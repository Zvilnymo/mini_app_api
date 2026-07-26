"""
Офлайн-тести AI-чату: без БД, без OpenAI, без Бітрікса — усе на фейках.

Головне, що вони доводять: заміна «мозку» чату на support_agent НЕ зламала
обвʼязку, яка вже працювала в проді (вимога ТЗ — «щоб AI це не зламав»):
кожне повідомлення пишеться в БД, ескалація створює задачу в Бітріксі й не
спамить, історія та summary читаються як раніше.

Запуск:  python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mini_app_api import chat, support_agent  # noqa: E402


CLIENT = {"id": 7, "full_name": "Тестовий Клієнт", "phone": "+380000000000"}
CASE = {"step_label": "Збір документів", "step": 2, "steps": [1, 2, 3, 4], "current_stage_name": "C1:PREPARATION"}


class FakeDB:
    """Записує всі виклики, щоб перевірити, що конвеєр не втратив жодного."""

    def __init__(self, recent_escalation=None, history=None, summary=None, count=4):
        self.messages = []          # (client_id, role, content, category)
        self.escalations = []       # (client_id, category, task_id)
        self._recent = recent_escalation
        self._history = history or []
        self._summary = summary
        self._count = count
        self.escalation_windows = []

    def add_chat_message(self, conn, client_id, role, content, category=None):
        self.messages.append((client_id, role, content, category))

    def get_chat_history(self, conn, client_id, limit=20):
        # конвеєр відкидає останній елемент (щойно доданий user) — імітуємо
        return self._history + [{"role": "user", "content": "останнє"}]

    def get_chat_summary(self, conn, client_id):
        return self._summary

    def count_chat_messages(self, conn, client_id):
        return self._count

    def get_chat_messages_range(self, conn, client_id, offset, limit):
        return []

    def set_chat_summary(self, conn, client_id, summary):
        self._summary = summary

    def get_recent_escalation(self, conn, client_id, category, within_minutes=120):
        self.escalation_windows.append((category, within_minutes))
        return self._recent

    def log_chat_escalation(self, conn, client_id, category, task_id):
        self.escalations.append((client_id, category, task_id))


class FakeBitrix:
    def __init__(self, fail=False):
        self.tasks = []
        self.fail = fail

    def create_complaint_task(self, *, title, description, responsible_id, auditors=None, deal_id=None):
        if self.fail:
            raise RuntimeError("Bitrix down")
        self.tasks.append({"title": title, "description": description,
                           "responsible_id": responsible_id, "auditors": auditors})
        return 12345


def run(user_message, *, llm_json=None, llm_none=False, db=None, bitrix=None, case=CASE):
    """Проганяє handle_message з підміненими залежностями."""
    db = db or FakeDB()
    bitrix = bitrix or FakeBitrix()
    patch_openai = mock.patch.object(chat, "_call_openai", return_value=(None if llm_none else llm_json))
    with mock.patch.object(chat, "db", db), mock.patch.object(chat, "bitrix", bitrix), \
            mock.patch.object(chat, "_client", None), patch_openai:
        result = chat.handle_message(
            conn=None, client=CLIENT, case=case, payments=None, days_active=10,
            user_message=user_message,
        )
    return result, db, bitrix


# ---------------------------------------------------------------------------
# 1. Обвʼязка, що вже працювала, лишилась цілою
# ---------------------------------------------------------------------------
class TestPlumbingPreserved(unittest.TestCase):
    def test_both_messages_stored_in_order(self):
        _, db, _ = run("Як зробити виписку з Монобанку?",
                       llm_json='{"category":"faq","reply":"Ось покроково: відкрийте застосунок, розділ Виписки, оберіть період і надішліть файл сюди 👍"}')
        self.assertEqual(len(db.messages), 2)
        self.assertEqual(db.messages[0][1], "user")
        self.assertEqual(db.messages[1][1], "assistant")
        self.assertEqual(db.messages[1][3], "faq")   # категорія збережена

    def test_plain_faq_does_not_escalate(self):
        _, db, bx = run("Скільки триває процедура банкрутства?",
                        llm_json='{"category":"faq","reply":"Зазвичай від кількох місяців до року — залежить від складності Вашої справи. Розкажу орієнтир саме по Вашому етапу 🙂"}')
        self.assertEqual(bx.tasks, [])
        self.assertEqual(db.escalations, [])

    def test_uncertain_escalates_and_logs(self):
        _, db, bx = run("Дуже специфічне питання без відповіді в базі",
                        llm_json='{"category":"uncertain","reply":"Передам юристу, він уточнить це питання і повернеться до Вас із конкретикою найближчим часом."}')
        self.assertEqual(len(bx.tasks), 1)
        self.assertEqual(db.escalations[0][1], "uncertain")
        self.assertEqual(db.escalations[0][2], 12345)

    def test_escalation_rate_limited(self):
        db = FakeDB(recent_escalation={"id": 1})   # вже ескалювали нещодавно
        _, db, bx = run("Ще одне емоційне повідомлення",
                        llm_json='{"category":"emotional","reply":"Розумію Вас, це справді непросто — ми поруч і Вашу справу тримаємо в роботі 💛"}',
                        db=db)
        self.assertEqual(bx.tasks, [], "не має створювати другу задачу в межах вікна")

    def test_bitrix_failure_does_not_break_reply(self):
        result, db, bx = run("Питання поза темою — де знайти роботу?",
                             llm_json='{"category":"off_topic","reply":"ігнорується"}',
                             bitrix=FakeBitrix(fail=True))
        self.assertTrue(result["reply"])                    # клієнт усе одно отримав відповідь
        self.assertEqual(db.escalations[0][2], None)        # задача не створилась, але факт залогований


# ---------------------------------------------------------------------------
# 2. Нове: детермінований запобіжник (не залежить від LLM)
# ---------------------------------------------------------------------------
class TestSafetyNet(unittest.TestCase):
    def test_distress_gives_psychologist_without_llm(self):
        # LLM свідомо «мертва» — відповідь усе одно має бути правильною
        result, db, bx = run("у мене немає сил, не бачу виходу з цього всього", llm_none=True)
        self.assertEqual(result["category"], support_agent.CATEGORY_DISTRESS)
        self.assertIn("380500360991", result["reply"])
        self.assertEqual(len(bx.tasks), 1)
        self.assertIn("КРИЗОВИЙ", bx.tasks[0]["title"])

    def test_distress_uses_shorter_escalation_window(self):
        _, db, _ = run("не хочу жити, все марно", llm_none=True)
        self.assertIn((support_agent.CATEGORY_DISTRESS, 30), db.escalation_windows)

    def test_complaint_escalates_and_does_not_argue(self):
        result, db, bx = run("Що це за розвод? два місяці нічого не робите, поверніть мої гроші!",
                             llm_none=True)
        self.assertEqual(result["category"], support_agent.CATEGORY_COMPLAINT)
        self.assertEqual(len(bx.tasks), 1)
        self.assertIn("Претензія", bx.tasks[0]["title"])

    def test_safety_net_skips_llm_entirely(self):
        with mock.patch.object(chat, "_call_openai") as spy, \
                mock.patch.object(chat, "db", FakeDB()), \
                mock.patch.object(chat, "bitrix", FakeBitrix()), \
                mock.patch.object(chat, "_client", None):
            chat.handle_message(conn=None, client=CLIENT, case=CASE, payments=None,
                                days_active=1, user_message="не хочу жити")
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Нове: анти-«закрита відповідь» (пряма вимога ТЗ)
# ---------------------------------------------------------------------------
class TestClosedReplies(unittest.TestCase):
    def test_bare_yes_is_replaced(self):
        result, _, _ = run("То ви подали заяву?", llm_json='{"category":"case_status","reply":"Так"}')
        self.assertNotEqual(result["reply"].strip().lower(), "так")
        self.assertGreater(len(result["reply"]), 40)

    def test_empty_promise_is_replaced(self):
        result, _, _ = run("А коли буде суд?",
                           llm_json='{"category":"case_status","reply":"Дайте мені трохи часу — уточню це і повернуся з відповіддю до Вас."}')
        self.assertNotIn("Дайте мені трохи часу", result["reply"])

    def test_dot_reply_is_replaced(self):
        result, _, _ = run("Дякую", llm_json='{"category":"faq","reply":"."}')
        self.assertGreater(len(result["reply"]), 40)

    def test_good_reply_is_left_alone(self):
        good = ("Розумію Ваше хвилювання 🌿 Зараз Ваша справа на етапі збору документів. "
                "Від Вас потрібні виписки за 3 періоди — і ми одразу рушаємо далі. Підказати по Вашому банку?")
        result, _, _ = run("Що по моїй справі?", llm_json='{"category":"case_status","reply":"%s"}' % good)
        self.assertEqual(result["reply"], good)

    def test_off_topic_uses_fixed_reply(self):
        result, _, _ = run("Порадьте, де шукати роботу",
                           llm_json='{"category":"off_topic","reply":"Раджу подивитись на work.ua"}')
        self.assertNotIn("work.ua", result["reply"])


# ---------------------------------------------------------------------------
# 3b. Дефекти, спіймані живим прогоном 26.07 на реальній моделі
# ---------------------------------------------------------------------------
class TestLiveRunRegressions(unittest.TestCase):
    def test_thanks_does_not_create_bitrix_task(self):
        # модель реально віддавала "emotional" на подяку → задача підтримці
        result, db, bx = run("Дякую вам велике за допомогу!",
                             llm_json='{"category":"emotional","reply":"Дуже приємно це чути! Ваша справа рухається, і я поруч, якщо будуть питання 🌿"}')
        self.assertEqual(result["category"], "faq")
        self.assertEqual(bx.tasks, [], "подяка не має смикати живу людину")

    def test_real_emotional_still_escalates(self):
        # контроль, що фікс не вимкнув справжню емоційну підтримку
        _, _, bx = run("Мені дуже соромно перед дітьми через ці борги, не знаю як їм пояснити",
                       llm_json='{"category":"emotional","reply":"Розумію Вас, це болісно. Ви не погана людина через борги — Ви людина, яка вирішує проблему 💛"}')
        self.assertEqual(len(bx.tasks), 1)

    def test_promised_handoff_actually_escalates(self):
        # модель обіцяє передати юристу, але категорія не ескалюється
        _, db, bx = run("Колектори дзвонять щодня, що робити?",
                        llm_json='{"category":"faq","reply":"Розумію, це виснажує. Я вже передаю Ваше звернення юристу, він підкаже дії саме у Вашій ситуації, і колега звʼяжеться з Вами найближчим часом 🤝"}')
        self.assertEqual(len(bx.tasks), 1, "обіцянка передати юристу має створити задачу")
        self.assertEqual(db.escalations[0][1], "promised_handoff")

    def test_no_promise_no_extra_task(self):
        _, _, bx = run("Скільки триває процедура?",
                       llm_json='{"category":"faq","reply":"Зазвичай від кількох місяців до року — залежить від складності справи. Розкажу орієнтир по Вашому етапу 🙂"}')
        self.assertEqual(bx.tasks, [])


# ---------------------------------------------------------------------------
# 4. Деградація: LLM недоступна
# ---------------------------------------------------------------------------
class TestOfflineDegradation(unittest.TestCase):
    def test_no_llm_still_gives_useful_answer(self):
        result, db, _ = run("Скільки коштує арбітражний керуючий?", llm_none=True)
        self.assertGreater(len(result["reply"]), 60)
        self.assertIn("22 710", result["reply"])
        self.assertEqual(len(db.messages), 2)   # усе одно збережено в БД

    def test_invalid_json_from_model_is_survived(self):
        result, _, _ = run("Питання", llm_json="це не json")
        self.assertTrue(result["reply"])
        self.assertEqual(result["category"], "uncertain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
