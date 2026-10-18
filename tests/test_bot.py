import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import Assistant, Store, TelegramError, clean_greeting, parse_command
from intelligence import brief


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.next_id = 1

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getBusinessConnection":
            return {"id": "conn", "user": {"id": 7}, "is_enabled": True, "rights": {"can_reply": True}}
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "copyMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "answerCallbackQuery":
            return True
        if method in ("sendPhoto", "sendVideo", "sendSticker"):
            return {"message_id": 100}
        raise AssertionError(method)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.bot = Assistant(self.api, self.store, 7)
        self.bot.styler = lambda source, mode, examples, incoming: (source, True)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_greetings_are_exact(self):
        self.assertEqual(clean_greeting("Доброе утро!"), "Доброе утро!")
        self.assertIsNone(clean_greeting("Не говори доброе утро"))

    def test_command_parsing(self):
        self.assertEqual(parse_command("/send 123 Привет"), ("send", "123", "Привет"))
        self.assertEqual(parse_command("Напиши Аня: Привет"), ("send", "Аня", "Привет"))
        self.assertEqual(parse_command("Напиши Маше привет"), ("send", "Маше", "привет"))
        self.assertEqual(parse_command("Ответь: Хорошо", (123, "conn", 10)), ("reply", (123, "conn", 10), "Хорошо"))

    def test_business_message_creates_unconfirmed_greeting_draft(self):
        self.bot.styler = lambda *args: self.fail("Greeting should not call the style API")
        self.bot.on_business_message({"business_connection_id": "conn", "message_id": 10, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Аня"}, "text": "Сладких снов!"})
        self.assertEqual(self.store.contact(42)[1], "Аня")
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))
        self.assertEqual(self.store.mirrored(3), (42, "conn", 10))
        self.assertEqual(self.store.draft(1)[6], "Сладких снов!")
        self.assertEqual(self.store.draft(1)[8], "pending")

    def test_ignores_other_business_account(self):
        self.api.call = lambda method, **params: {"id": "conn", "user": {"id": 999}, "is_enabled": True, "rights": {"can_reply": True}}
        self.bot.on_business_message({"business_connection_id": "conn", "message_id": 10, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "Доброе утро"})
        self.assertIsNone(self.store.contact(42))

    def test_ignores_non_owner_command(self):
        self.bot.on_owner_message({"from": {"id": 99}, "chat": {"id": 99, "type": "private"}, "text": "/chats"})
        self.assertEqual(self.api.calls, [])

    def test_reply_to_mirror_requires_confirmation(self):
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "text": "Привет"})
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))
        self.bot.on_callback({"id": "cb", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendMessage" and params.get("business_connection_id")]
        self.assertEqual(sent[0]["chat_id"], 42)
        self.assertEqual(sent[0]["reply_parameters"], {"message_id": 10})
        self.assertEqual(self.store.draft(1)[8], "sent")

    def test_reply_starts_with_unmodified_text(self):
        self.bot.styler = lambda *args: self.fail("Styling must be optional")
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "text": "ну я приду завтра"})
        self.assertEqual(self.store.draft(1)[6], "ну я приду завтра")
        self.assertEqual(self.store.draft(1)[7], "raw")

    def test_original_button_restores_text_after_styling(self):
        self.bot.styler = lambda *args: ("Другой смысл", True)
        self.bot.create_draft(42, "conn", 10, "ну я приду завтра")
        self.bot.on_callback({"id": "mode", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "mode:1:kind"})
        self.assertEqual(self.store.draft(1)[6], "Другой смысл")
        self.bot.on_callback({"id": "original", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "original:1"})
        self.assertEqual(self.store.draft(1)[6], "ну я приду завтра")
        self.bot.on_callback({"id": "send", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendMessage" and params.get("business_connection_id")]
        self.assertEqual(sent[0]["text"], "ну я приду завтра")

    def test_business_recent_contact_error_keeps_draft(self):
        self.bot.create_draft(42, "conn", None, "Привет")
        preview_id = self.store.latest_preview(1)
        original_call = self.api.call

        def denied_call(method, **params):
            if method == "sendMessage" and params.get("business_connection_id"):
                raise TelegramError("Bad Request: BUSINESS_PEER_USAGE_MISSING")
            return original_call(method, **params)

        self.api.call = denied_call
        self.bot.on_callback({"id": "send", "from": {"id": 7}, "message": {"message_id": preview_id}, "data": "send:1"})
        self.assertEqual(self.store.draft(1)[8], "pending")
        self.assertEqual(self.store.latest_preview(1), preview_id)
        self.assertTrue(any("сначала написать" in params.get("text", "") for method, params in self.api.calls if method == "sendMessage"))

    def test_photo_reply_is_previewed_then_sent_with_caption(self):
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"message_id": 60, "from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "photo": [{"file_id": "small"}, {"file_id": "large"}], "caption": "Это фото"})
        self.assertEqual(self.store.media_draft(1), ("photo", "large", 60))
        self.assertFalse(any(method == "sendPhoto" for method, _ in self.api.calls))
        preview = self.store.latest_preview(1)
        self.bot.on_callback({"id": "send", "from": {"id": 7}, "message": {"message_id": preview}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendPhoto"]
        self.assertEqual(sent, [{"business_connection_id": "conn", "chat_id": 42, "photo": "large", "caption": "Это фото", "reply_parameters": {"message_id": 10}}])

    def test_sticker_reply_is_previewed_then_sent(self):
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"message_id": 61, "from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "sticker": {"file_id": "sticker123"}})
        self.bot.on_callback({"id": "send", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendSticker"]
        self.assertEqual(sent, [{"business_connection_id": "conn", "chat_id": 42, "sticker": "sticker123", "reply_parameters": {"message_id": 10}}])

    def test_video_with_contact_caption_and_caption_edit(self):
        self.store.remember(42, "Маша", None, "conn")
        self.bot.on_owner_message({"message_id": 62, "from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "video": {"file_id": "video123"}, "caption": "/send Маше Старый текст"})
        self.assertEqual(self.store.draft(1)[6], "Старый текст")
        preview = self.store.latest_preview(1)
        self.bot.on_owner_message({"message_id": 63, "from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": preview}, "text": "Новая подпись"})
        self.bot.on_callback({"id": "send", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendVideo"]
        self.assertEqual(sent, [{"business_connection_id": "conn", "chat_id": 42, "video": "video123", "caption": "Новая подпись"}])

    def test_memory_and_mission_commands_use_replied_contact(self):
        self.store.remember(42, "Маша", None, "conn")
        self.store.mirror(50, 42, "conn", 10)
        base = {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}}
        self.bot.on_owner_message(dict(base, text="/remember Встреча в пятницу в 19:00"))
        self.assertEqual(self.store.memories(42)[0][1], "Встреча в пятницу в 19:00")
        self.bot.on_owner_message(dict(base, text="/mission Договориться о встрече | Только после 18:00"))
        self.assertEqual(self.store.mission(42), ("Договориться о встрече", "Только после 18:00"))
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/stopmission Маша"})
        self.assertIsNone(self.store.mission(42))

    def test_archive_search_and_90_day_retention(self):
        import time
        now = int(time.time())
        self.store.save_archive(42, 10, "in", "text", "Встреча в Казани завтра", now)
        self.store.save_archive(42, 11, "in", "text", "Старое сообщение о билете", now - 91 * 86400)
        self.assertEqual(self.store.search_archive("Казани")[0][1], 10)
        self.store.purge_archive(now)
        self.assertEqual(self.store.search_archive("билете"), [])
        self.store.save_archive(42, 10, "in", "text", "Встреча перенесена", now)
        self.assertEqual(self.store.search_archive("Казани"), [])
        self.assertEqual(self.store.search_archive("перенесена")[0][1], 10)

    def test_brief_quotes_source_questions_and_times(self):
        card = brief("Встречаемся в 19:30. Адрес пришлёшь?")
        self.assertIn("Адрес пришлёшь?", card)
        self.assertIn("19:30", card)

    def test_mission_prepares_reviewable_draft_in_background(self):
        from unittest.mock import patch

        class InlineExecutor:
            def submit(self, function, *args):
                function(*args)

        self.bot.executor = InlineExecutor()
        self.store.set_mission(42, "Уточнить время встречи", "После 18:00")
        incoming = {"business_connection_id": "conn", "message_id": 10, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Маша"}, "text": "Во сколько увидимся?"}
        with patch("bot.mission_reply", return_value="Давай встретимся после 18:00. Во сколько тебе удобно?"):
            self.bot.on_business_message(incoming)
        draft = self.store.draft(1)
        self.assertEqual(draft[6], "Давай встретимся после 18:00. Во сколько тебе удобно?")
        self.assertEqual(draft[8], "pending")
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))

    def test_old_preview_cannot_confirm_changed_text(self):
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "text": "Первый"})
        old_preview = self.store.latest_preview(1)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": old_preview}, "text": "Второй"})
        self.bot.on_callback({"id": "cb", "from": {"id": 7}, "message": {"message_id": old_preview}, "data": "send:1"})
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))
        self.assertEqual(self.store.draft(1)[6], "Второй")

    def test_alias_is_used_for_draft(self):
        self.store.remember(42, "Анна", "anna", "conn")
        self.store.set_alias(42, "Маша")
        self.assertEqual(self.store.resolve("Маша")[0][0], 42)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/send Маша Привет"})
        self.assertEqual(self.store.draft(1)[1], 42)
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))

    def test_inflected_alias_resolves_in_voice_like_command(self):
        self.store.remember(42, "Мария", None, "conn")
        self.store.set_alias(42, "Маша")
        self.assertEqual(self.store.resolve("Маше")[0][0], 42)
        self.assertEqual(self.store.resolve("Маше,")[0][0], 42)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "Напиши Маше: привет"})
        self.assertEqual(self.store.draft(1)[1], 42)

    def test_inflected_first_name_requires_unique_match(self):
        self.store.remember(42, "Сергей Иванов", None, "conn")
        self.store.remember(43, "Сергей Петров", None, "conn")
        self.assertEqual(len(self.store.resolve("Сергею")), 2)

    def test_yo_matches_e(self):
        self.store.remember(42, "Артём", None, "conn")
        self.assertEqual(self.store.resolve("Артему")[0][0], 42)

    def test_alias_casefold_collision(self):
        self.store.remember(42, "Анна", None, "conn")
        self.store.remember(43, "Мария", None, "conn")
        self.store.set_alias(42, "Маша")
        with self.assertRaises(ValueError):
            self.store.set_alias(43, "маша")

    def test_alias_can_be_set_by_replying_to_incoming(self):
        self.store.remember(42, "Анна", None, "conn")
        self.store.mirror(50, 42, "conn", 10)
        self.bot.on_owner_message({"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "reply_to_message": {"message_id": 50}, "text": "/alias Маша"})
        self.assertEqual(self.store.alias_for(42), "Маша")

    def test_mode_button_rewrites_then_latest_preview_sends(self):
        self.bot.styler = lambda source, mode, examples, incoming: (mode + ":" + source, True)
        self.bot.create_draft(42, "conn", 10, "Привет")
        first_preview = self.store.latest_preview(1)
        self.bot.on_callback({"id": "m", "from": {"id": 7}, "message": {"message_id": first_preview}, "data": "mode:1:kind"})
        self.assertEqual(self.store.draft(1)[6], "kind:Привет")
        self.bot.on_callback({"id": "s", "from": {"id": 7}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        sent = [params for method, params in self.api.calls if method == "sendMessage" and params.get("business_connection_id")]
        self.assertEqual(sent[0]["text"], "kind:Привет")

    def test_non_owner_cannot_confirm(self):
        self.store.create_draft(42, "conn", 10, "Привет", "", "Привет", "normal")
        self.bot.show_draft(1)
        self.bot.on_callback({"id": "cb", "from": {"id": 99}, "message": {"message_id": self.store.latest_preview(1)}, "data": "send:1"})
        self.assertEqual(self.store.draft(1)[8], "pending")

    def test_auto_off_prevents_reply(self):
        self.store.set("auto", "off")
        self.bot.on_business_message({"business_connection_id": "conn", "message_id": 10, "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "first_name": "Аня"}, "text": "Доброе утро"})
        self.assertFalse(any(method == "sendMessage" and params.get("business_connection_id") for method, params in self.api.calls))


if __name__ == "__main__":
    unittest.main()
