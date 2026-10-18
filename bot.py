"""Personal Telegram Business assistant. Python 3.9+."""

import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from names import match_names
from styling import MODES, StyleError, model_ready, rewrite
from intelligence import brief, mission_reply, read_media


LOG = logging.getLogger("assistant")
GREETING_REPLIES = {
    "доброе утро": "Доброе утро",
    "сладких снов": "Сладких снов",
    "спокойной ночи": "Спокойной ночи",
}


def clean_greeting(text):
    normalized = re.sub(r"[^\w\s]", "", text.casefold(), flags=re.UNICODE)
    normalized = " ".join(normalized.split())
    return text.strip() if normalized in GREETING_REPLIES else None


def display_name(user):
    return " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username") or str(user.get("id", "?"))


def parse_command(text, reply_target=None):
    """Return (operation, target, body). The target can be a chat ID or name."""
    text = text.strip()
    if not text:
        return None
    m = re.match(r"^/(send|написать)\s+(\S+)\s+([\s\S]+)$", text, re.I)
    if m:
        return "send", m.group(2), m.group(3).strip()
    m = re.match(r"^(?:напиши|отправь)\s+(.+?)\s*[:：]\s*(.+)$", text, re.I | re.S)
    if m:
        return "send", m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(?:напиши|отправь)\s+(\S+)\s+([\s\S]+)$", text, re.I)
    if m:
        return "send", m.group(1), m.group(2).strip()
    m = re.match(r"^/(reply|ответить)\s+([\s\S]+)$", text, re.I)
    if m and reply_target:
        return "reply", reply_target, m.group(2).strip()
    m = re.match(r"^ответь\s*[:：]\s*(.+)$", text, re.I | re.S)
    if m and reply_target:
        return "reply", reply_target, m.group(1).strip()
    if reply_target and not text.startswith("/"):
        return "reply", reply_target, text
    return None


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot" + token + "/"
        self.file_base = "https://api.telegram.org/file/bot" + token + "/"

    def call(self, method, **params):
        payload = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc)
                raise TelegramError(detail.get("description", str(exc))) from exc
            except (ValueError, AttributeError):
                raise TelegramError(str(exc)) from exc
        if not result.get("ok"):
            raise TelegramError(result.get("description", "Unknown Telegram API error"))
        return result["result"]

    def download(self, file_id, destination):
        info = self.call("getFile", file_id=file_id)
        path = info["file_path"]
        with urllib.request.urlopen(self.file_base + urllib.parse.quote(path, safe="/"), timeout=45) as src:
            destination.write_bytes(src.read())


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(str(path))
        self.last_purge = 0
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS contacts (chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT, connection_id TEXT NOT NULL, seen_at INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS mirrors (message_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, connection_id TEXT NOT NULL, original_id INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS drafts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, connection_id TEXT NOT NULL, original_id INTEGER, source_text TEXT NOT NULL, incoming_text TEXT NOT NULL, text TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
        self.db.execute("CREATE TABLE IF NOT EXISTS media_drafts (draft_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, file_id TEXT NOT NULL, source_message_id INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS draft_previews (message_id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS style_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS incoming_texts (chat_id INTEGER NOT NULL, original_id INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(chat_id, original_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS aliases (chat_id INTEGER PRIMARY KEY, alias TEXT NOT NULL COLLATE NOCASE UNIQUE)")
        self.db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, source_message_id INTEGER, created_at INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS missions (chat_id INTEGER PRIMARY KEY, goal TEXT NOT NULL, limits TEXT NOT NULL, active INTEGER NOT NULL, created_at INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS archive (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(chat_id, message_id, direction))")
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(content, content='archive', content_rowid='id', tokenize='unicode61')")
        self.db.commit()

    def purge_archive(self, now=None):
        now = now or int(time.time())
        cutoff = now - 90 * 86400
        old = self.db.execute("SELECT id, content FROM archive WHERE created_at<?", (cutoff,)).fetchall()
        for archive_id, content in old:
            self.db.execute("INSERT INTO archive_fts(archive_fts, rowid, content) VALUES ('delete', ?, ?)", (archive_id, content))
        self.db.execute("DELETE FROM archive WHERE created_at<?", (cutoff,))
        self.db.commit()
        self.last_purge = now

    def save_archive(self, chat_id, message_id, direction, kind, content, created_at=None):
        if not content.strip():
            return
        timestamp = created_at or int(time.time())
        if timestamp < int(time.time()) - 90 * 86400:
            return
        if int(time.time()) - self.last_purge > 86400:
            self.purge_archive()
        row = self.db.execute("SELECT id FROM archive WHERE chat_id=? AND message_id=? AND direction=?", (chat_id, message_id, direction)).fetchone()
        if row:
            self.db.execute("INSERT INTO archive_fts(archive_fts, rowid, content) VALUES ('delete', ?, (SELECT content FROM archive WHERE id=?))", (row[0], row[0]))
            self.db.execute("UPDATE archive SET kind=?, content=? WHERE id=?", (kind, content[:10000], row[0]))
            archive_id = row[0]
        else:
            cursor = self.db.execute("INSERT INTO archive(chat_id, message_id, direction, kind, content, created_at) VALUES (?, ?, ?, ?, ?, ?)", (chat_id, message_id, direction, kind, content[:10000], timestamp))
            archive_id = cursor.lastrowid
        self.db.execute("INSERT INTO archive_fts(rowid, content) VALUES (?, ?)", (archive_id, content[:10000]))
        self.db.commit()

    def search_archive(self, query, limit=8):
        if int(time.time()) - self.last_purge > 86400:
            self.purge_archive()
        stopwords = {"кто", "что", "где", "когда", "какой", "какая", "какие", "какую", "мне", "меня", "было", "это", "про", "для", "наш", "мой", "моя"}
        words = [word for word in re.findall(r"[\w-]+", query.casefold().replace("ё", "е"), re.UNICODE) if len(word) >= 3 and word not in stopwords][:8]
        if not words:
            return []
        expression = " OR ".join('"{}"'.format(word.replace('"', '')) for word in words)
        rows = self.db.execute("SELECT a.chat_id, a.message_id, a.direction, a.kind, a.content, a.created_at FROM archive_fts f JOIN archive a ON a.id=f.rowid WHERE archive_fts MATCH ? ORDER BY bm25(archive_fts), a.created_at DESC LIMIT ?", (expression, limit)).fetchall()
        if rows:
            return rows
        clauses = ["content LIKE ?" for _ in words]
        stems = ["%" + word[:max(3, len(word) - 2)] + "%" for word in words]
        return self.db.execute("SELECT chat_id, message_id, direction, kind, content, created_at FROM archive WHERE {} ORDER BY created_at DESC LIMIT ?".format(" OR ".join(clauses)), (*stems, limit)).fetchall()

    def add_memory(self, chat_id, text, source_message_id=None):
        cursor = self.db.execute("INSERT INTO memories(chat_id, text, source_message_id, created_at) VALUES (?, ?, ?, ?)", (chat_id, text[:2000], source_message_id, int(time.time())))
        self.db.commit()
        return cursor.lastrowid

    def memories(self, chat_id, limit=10):
        return self.db.execute("SELECT id, text, source_message_id FROM memories WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()

    def search_memories(self, query, limit=5):
        words = [word for word in re.findall(r"[\w-]+", query.casefold(), re.UNICODE) if len(word) >= 4][:5]
        if not words:
            return []
        clauses = ["lower(text) LIKE ?" for _ in words]
        stems = ["%" + word[:max(3, len(word) - 2)] + "%" for word in words]
        return self.db.execute("SELECT id, chat_id, text FROM memories WHERE {} ORDER BY id DESC LIMIT ?".format(" OR ".join(clauses)), (*stems, limit)).fetchall()

    def memory_conflicts(self, chat_id, text):
        dates = re.findall(r"\b(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|понедельник|вторник|среду|среда|четверг|пятницу|пятница|субботу|суббота|воскресенье)\b", text.casefold())
        times = re.findall(r"\b\d{1,2}:\d{2}\b", text)
        if not dates or not times:
            return []
        rows = self.db.execute("SELECT chat_id, text FROM memories WHERE chat_id<>? ORDER BY id DESC LIMIT 200", (chat_id,)).fetchall()
        return [row for row in rows if any(date in row[1].casefold() for date in dates) and any(clock in row[1] for clock in times)][:3]

    def remove_memory(self, memory_id):
        cursor = self.db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.db.commit()
        return cursor.rowcount == 1

    def set_mission(self, chat_id, goal, limits=""):
        self.db.execute("INSERT INTO missions VALUES (?, ?, ?, 1, ?) ON CONFLICT(chat_id) DO UPDATE SET goal=excluded.goal, limits=excluded.limits, active=1, created_at=excluded.created_at", (chat_id, goal[:500], limits[:500], int(time.time())))
        self.db.commit()

    def mission(self, chat_id):
        return self.db.execute("SELECT goal, limits FROM missions WHERE chat_id=? AND active=1", (chat_id,)).fetchone()

    def stop_mission(self, chat_id):
        self.db.execute("UPDATE missions SET active=0 WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
        self.db.commit()

    def remember(self, chat_id, name, username, connection_id):
        self.db.execute("INSERT OR REPLACE INTO contacts VALUES (?, ?, ?, ?, ?)", (chat_id, name, username, connection_id, int(time.time())))
        self.db.commit()

    def contact(self, chat_id):
        return self.db.execute("SELECT chat_id, name, username, connection_id FROM contacts WHERE chat_id=?", (chat_id,)).fetchone()

    def resolve(self, query):
        if str(query).lstrip("-").isdigit():
            item = self.contact(int(query))
            return [item] if item else []
        candidates = self.db.execute("SELECT c.chat_id, c.name, c.username, c.connection_id, a.alias FROM contacts c LEFT JOIN aliases a ON a.chat_id=c.chat_id ORDER BY c.seen_at DESC").fetchall()
        scored = [(match_names(query, row[4], row[1], row[2]), row[:4]) for row in candidates]
        scored = [(score, row) for score, row in scored if score is not None]
        if not scored:
            return []
        best = min(score for score, _ in scored)
        return [row for score, row in scored if score == best]

    def recent(self, page=1):
        return self.db.execute("SELECT chat_id, name, username, connection_id FROM contacts ORDER BY seen_at DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)).fetchall()

    def mirror(self, message_id, chat_id, connection_id, original_id):
        self.db.execute("INSERT OR REPLACE INTO mirrors VALUES (?, ?, ?, ?)", (message_id, chat_id, connection_id, original_id))
        self.db.commit()

    def mirrored(self, message_id):
        return self.db.execute("SELECT chat_id, connection_id, original_id FROM mirrors WHERE message_id=?", (message_id,)).fetchone()

    def add_sample(self, text):
        if 4 <= len(text.strip()) <= 500:
            self.db.execute("INSERT INTO style_samples(text) VALUES (?)", (text.strip(),))
            self.db.execute("DELETE FROM style_samples WHERE id NOT IN (SELECT id FROM style_samples ORDER BY id DESC LIMIT 30)")
            self.db.commit()

    def samples(self):
        return [row[0] for row in self.db.execute("SELECT text FROM style_samples ORDER BY id DESC LIMIT 8")]

    def create_draft(self, chat_id, connection_id, original_id, source_text, incoming_text, text, mode):
        cursor = self.db.execute("INSERT INTO drafts(chat_id, connection_id, original_id, source_text, incoming_text, text, mode) VALUES (?, ?, ?, ?, ?, ?, ?)", (chat_id, connection_id, original_id, source_text, incoming_text, text, mode))
        self.db.commit()
        return cursor.lastrowid

    def draft(self, draft_id):
        return self.db.execute("SELECT id, chat_id, connection_id, original_id, source_text, incoming_text, text, mode, status FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def add_media_draft(self, draft_id, kind, file_id, source_message_id):
        self.db.execute("INSERT INTO media_drafts VALUES (?, ?, ?, ?)", (draft_id, kind, file_id, source_message_id))
        self.db.commit()

    def media_draft(self, draft_id):
        return self.db.execute("SELECT kind, file_id, source_message_id FROM media_drafts WHERE draft_id=?", (draft_id,)).fetchone()

    def update_draft(self, draft_id, text, mode, source_text=None):
        if source_text is None:
            self.db.execute("UPDATE drafts SET text=?, mode=? WHERE id=? AND status='pending'", (text, mode, draft_id))
        else:
            self.db.execute("UPDATE drafts SET source_text=?, text=?, mode=? WHERE id=? AND status='pending'", (source_text, text, mode, draft_id))
        self.db.commit()

    def change_status(self, draft_id, old, new):
        cursor = self.db.execute("UPDATE drafts SET status=? WHERE id=? AND status=?", (new, draft_id, old))
        self.db.commit()
        return cursor.rowcount == 1

    def add_preview(self, message_id, draft_id):
        self.db.execute("INSERT OR REPLACE INTO draft_previews VALUES (?, ?)", (message_id, draft_id))
        self.db.commit()

    def preview_draft(self, message_id):
        row = self.db.execute("SELECT draft_id FROM draft_previews WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def latest_preview(self, draft_id):
        row = self.db.execute("SELECT message_id FROM draft_previews WHERE draft_id=? ORDER BY message_id DESC LIMIT 1", (draft_id,)).fetchone()
        return row[0] if row else None

    def save_incoming(self, chat_id, original_id, text):
        self.db.execute("INSERT OR REPLACE INTO incoming_texts VALUES (?, ?, ?)", (chat_id, original_id, text[:2000]))
        self.db.commit()

    def incoming(self, chat_id, original_id):
        row = self.db.execute("SELECT text FROM incoming_texts WHERE chat_id=? AND original_id=?", (chat_id, original_id)).fetchone()
        return row[0] if row else ""

    def set_alias(self, chat_id, alias):
        if not self.contact(chat_id):
            raise ValueError("Этот чат ещё не появился в /chats.")
        if not re.fullmatch(r"[\w-]{1,32}", alias, re.UNICODE):
            raise ValueError("Имя должно быть одним словом до 32 символов; пробелы замените на _.")
        for existing_id, existing_alias in self.db.execute("SELECT chat_id, alias FROM aliases"):
            if existing_id != chat_id and existing_alias.casefold() == alias.casefold():
                raise ValueError("Это имя уже занято другим контактом.")
        try:
            self.db.execute("INSERT INTO aliases(chat_id, alias) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET alias=excluded.alias", (chat_id, alias))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Это имя уже занято другим контактом.") from exc

    def alias_for(self, chat_id):
        row = self.db.execute("SELECT alias FROM aliases WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None


class Assistant:
    def __init__(self, api, store, owner_id, styler=rewrite, executor=None):
        self.api, self.store, self.owner_id = api, store, owner_id
        self.styler = styler
        self.executor = executor
        self.connections = {}
        self.voice_model = None

    def tell(self, text, **kwargs):
        return self.api.call("sendMessage", chat_id=self.owner_id, text=text[:4096], **kwargs)

    def connection(self, connection_id):
        if connection_id not in self.connections:
            self.connections[connection_id] = self.api.call("getBusinessConnection", business_connection_id=connection_id)
        info = self.connections[connection_id]
        if info.get("user", {}).get("id") != self.owner_id or not info.get("is_enabled"):
            return None
        return info

    def can_reply(self, connection_id):
        info = self.connection(connection_id)
        return bool(info and info.get("rights", {}).get("can_reply"))

    def send_as_owner(self, chat_id, connection_id, text, original_id=None):
        if not self.can_reply(connection_id):
            raise TelegramError("Нет права can_reply для этого подключения. Проверьте настройки Business-бота.")
        if not text or len(text) > 4096:
            raise ValueError("Длина сообщения должна быть от 1 до 4096 символов.")
        params = {"business_connection_id": connection_id, "chat_id": chat_id, "text": text}
        if original_id:
            params["reply_parameters"] = {"message_id": original_id}
        return self.api.call("sendMessage", **params)

    def send_media_as_owner(self, chat_id, connection_id, kind, file_id, caption="", original_id=None):
        if not self.can_reply(connection_id):
            raise TelegramError("Нет права can_reply для этого подключения. Проверьте настройки Business-бота.")
        methods = {"photo": ("sendPhoto", "photo"), "video": ("sendVideo", "video"), "sticker": ("sendSticker", "sticker")}
        if kind not in methods or not file_id:
            raise ValueError("Неизвестный тип вложения.")
        if len(caption) > 1024:
            raise ValueError("Подпись к фото или видео должна быть не длиннее 1024 символов.")
        method, field = methods[kind]
        params = {"business_connection_id": connection_id, "chat_id": chat_id, field: file_id}
        if caption and kind != "sticker":
            params["caption"] = caption
        if original_id:
            params["reply_parameters"] = {"message_id": original_id}
        return self.api.call(method, **params)

    def mirror_content(self, message, chat_id):
        try:
            return self.api.call("copyMessage", chat_id=self.owner_id, from_chat_id=chat_id, message_id=message["message_id"])["message_id"]
        except TelegramError:
            pass
        media = [
            ("photo", "sendPhoto", "photo"), ("voice", "sendVoice", "voice"),
            ("video", "sendVideo", "video"), ("audio", "sendAudio", "audio"),
            ("document", "sendDocument", "document"), ("animation", "sendAnimation", "animation"),
            ("sticker", "sendSticker", "sticker"), ("video_note", "sendVideoNote", "video_note"),
        ]
        for kind, method, parameter in media:
            item = message.get(kind)
            if item:
                if kind == "photo":
                    item = item[-1]
                try:
                    params = {"chat_id": self.owner_id, parameter: item["file_id"]}
                    if message.get("caption") and kind not in ("sticker", "video_note"):
                        params["caption"] = message["caption"][:1024]
                    return self.api.call(method, **params)["message_id"]
                except TelegramError:
                    break
        if message.get("location"):
            point = message["location"]
            return self.api.call("sendLocation", chat_id=self.owner_id, latitude=point["latitude"], longitude=point["longitude"])["message_id"]
        if message.get("contact"):
            contact = message["contact"]
            return self.api.call("sendContact", chat_id=self.owner_id, phone_number=contact["phone_number"], first_name=contact["first_name"], last_name=contact.get("last_name", ""))["message_id"]
        body = message.get("text") or message.get("caption") or "[Медиа или служебное сообщение: Telegram не разрешил переслать этот тип]"
        return self.tell(body[:3500])["message_id"]

    def draft_keyboard(self, draft_id, media=False):
        rows = [
            [{"text": "✅ Отправить", "callback_data": "send:{}".format(draft_id)},
             {"text": "❌ Отмена", "callback_data": "cancel:{}".format(draft_id)}]
        ]
        if media:
            return {"inline_keyboard": rows}
        rows.append([{"text": "↩️ Исходный текст", "callback_data": "original:{}".format(draft_id)}])
        preferred = self.store.get("mode", "normal")
        if preferred != "normal" and preferred in MODES:
            rows.append([{"text": "✨ Мой режим: {}".format(MODES[preferred][0]), "callback_data": "mode:{}:{}".format(draft_id, preferred)}])
        items = list(MODES.items())
        for start in range(0, len(items), 4):
            rows.append([{"text": value[0], "callback_data": "mode:{}:{}".format(draft_id, key)} for key, value in items[start:start + 4]])
        return {"inline_keyboard": rows}

    def show_draft(self, draft_id, note=""):
        draft = self.store.draft(draft_id)
        if not draft or draft[8] != "pending":
            return
        _, chat_id, _, _, _, _, text, mode, _ = draft
        media = self.store.media_draft(draft_id)
        contact = self.store.contact(chat_id)
        name = self.store.alias_for(chat_id) or (contact[1] if contact else str(chat_id))
        if media:
            kind, _, source_message_id = media
            label = {"photo": "Фото", "video": "Видео", "sticker": "Стикер"}[kind]
            preview = "Черновик #{} → {} (ID {})\n{}{}{}\n\nВложение — в сообщении, на которое я отвечаю. Нажмите «Отправить» только после проверки получателя.".format(
                draft_id, name, chat_id, label, "\n" + note if note else "", "\nПодпись: " + text if text else ""
            )
            sent = self.tell(preview, reply_markup=self.draft_keyboard(draft_id, media=True), reply_parameters={"message_id": source_message_id})
        else:
            label = "Без изменений" if mode == "raw" else MODES[mode][0]
            preview = "Черновик #{} → {} (ID {})\nРежим: {}{}\n\n{}\n\nНажмите «Отправить» или ответьте на это сообщение своим исправленным текстом.".format(
                draft_id, name, chat_id, label, "\n" + note if note else "", text
            )
            sent = self.tell(preview, reply_markup=self.draft_keyboard(draft_id))
        self.store.add_preview(sent["message_id"], draft_id)

    def create_draft(self, chat_id, connection_id, original_id, source_text, incoming_text="", mode=None, verbatim=False):
        if not self.connection(connection_id):
            raise TelegramError("Business-подключение недоступно.")
        source_text = source_text.strip()
        if not source_text or len(source_text) > 3500:
            raise ValueError("Текст черновика должен быть от 1 до 3500 символов.")
        mode, text = "raw", source_text
        note = ("Предложенный ответ на приветствие — отправка только после подтверждения."
                if verbatim else "Исходный текст. Для переформулировки выберите стиль кнопкой ниже.")
        if len(text) > 3500:
            raise ValueError("Ответ слишком длинный для предпросмотра.")
        draft_id = self.store.create_draft(chat_id, connection_id, original_id, source_text, incoming_text, text, mode)
        self.show_draft(draft_id, note)
        return draft_id

    def create_media_draft(self, chat_id, connection_id, original_id, kind, file_id, source_message_id, caption=""):
        if not self.connection(connection_id):
            raise TelegramError("Business-подключение недоступно.")
        if kind not in ("photo", "video", "sticker") or not file_id:
            raise ValueError("Поддерживаются только фото, видео и стикеры.")
        if kind == "sticker":
            caption = ""
        if len(caption) > 1024:
            raise ValueError("Подпись должна быть не длиннее 1024 символов.")
        draft_id = self.store.create_draft(chat_id, connection_id, original_id, caption, "", caption, "raw")
        self.store.add_media_draft(draft_id, kind, file_id, source_message_id)
        self.show_draft(draft_id, "Отправка только после подтверждения.")
        return draft_id

    def on_callback(self, callback):
        if callback.get("from", {}).get("id") != self.owner_id:
            self.api.call("answerCallbackQuery", callback_query_id=callback["id"], text="Нет доступа.", show_alert=True)
            return
        self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
        data = callback.get("data", "")
        parts = data.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            return
        operation, draft_id = parts[0], int(parts[1])
        draft = self.store.draft(draft_id)
        if not draft or draft[8] != "pending":
            self.tell("Черновик #{} уже закрыт или не найден.".format(draft_id))
            return
        shown_message_id = callback.get("message", {}).get("message_id")
        if shown_message_id != self.store.latest_preview(draft_id):
            self.tell("Этот предпросмотр устарел. Используйте последнюю версию черновика #{}.".format(draft_id))
            return
        _, chat_id, connection_id, original_id, source, incoming, text, mode, _ = draft
        if operation == "cancel":
            self.store.change_status(draft_id, "pending", "canceled")
            self.tell("Черновик #{} отменён.".format(draft_id))
        elif operation == "original":
            self.store.update_draft(draft_id, source, "raw")
            self.show_draft(draft_id, "Восстановлен исходный текст.")
        elif operation == "mode" and len(parts) == 3 and parts[2] in MODES:
            new_mode = parts[2]
            self.tell("Переформулирую черновик #{} в режиме «{}»...".format(draft_id, MODES[new_mode][0]))
            try:
                new_text, used_ai = self.styler(source, new_mode, self.store.samples(), incoming)
                if len(new_text) > 3500:
                    raise StyleError("Получился слишком длинный текст.")
                self.store.update_draft(draft_id, new_text, new_mode)
                self.show_draft(draft_id, "Переформулировано." if used_ai else "Исходный текст без переработки.")
            except StyleError as exc:
                self.tell(str(exc))
        elif operation == "send":
            if not self.store.change_status(draft_id, "pending", "sending"):
                return
            try:
                media = self.store.media_draft(draft_id)
                if media:
                    kind, file_id, _ = media
                    self.send_media_as_owner(chat_id, connection_id, kind, file_id, text, original_id)
                else:
                    self.send_as_owner(chat_id, connection_id, text, original_id)
            except TelegramError as exc:
                self.store.change_status(draft_id, "sending", "pending")
                if "BUSINESS_PEER_USAGE_MISSING" in str(exc):
                    self.tell("Telegram пока не разрешает отправить черновик #{} этому контакту. Собеседник должен сначала написать вам в личный чат; после нового входящего сообщения откройте последний предпросмотр и нажмите «Отправить» ещё раз. Черновик сохранён.".format(draft_id))
                    return
                raise
            except Exception:
                self.store.change_status(draft_id, "sending", "pending")
                raise
            self.store.change_status(draft_id, "sending", "sent")
            self.tell("Черновик #{} отправлен в чат {}.".format(draft_id, chat_id))

    def on_business_message(self, message, edited=False):
        connection_id = message.get("business_connection_id")
        if not connection_id or not self.connection(connection_id):
            return
        sender = message.get("from", {})
        chat = message.get("chat", {})
        if message.get("sender_business_bot"):
            return
        if sender.get("id") == self.owner_id:
            if message.get("text"):
                self.store.add_sample(message["text"])
            outgoing = message.get("text") or message.get("caption") or ""
            if outgoing and chat.get("type") == "private":
                self.store.save_archive(chat["id"], message["message_id"], "out", "text", outgoing, message.get("date"))
            return
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        name = display_name(sender)
        self.store.remember(chat_id, name, sender.get("username"), connection_id)
        shown_name = self.store.alias_for(chat_id) or name
        incoming_text = message.get("text") or message.get("caption") or ""
        self.store.save_incoming(chat_id, message["message_id"], incoming_text)
        if incoming_text:
            self.store.save_archive(chat_id, message["message_id"], "in", "text", incoming_text, message.get("date"))
        header = self.tell("{} от {}{} · ID {}\nЧто ответить? Ответьте на копию ниже текстом или голосовым. Перед отправкой я покажу черновик.".format("Изменённое сообщение" if edited else "Сообщение", shown_name, " (@" + sender["username"] + ")" if sender.get("username") else "", chat_id))
        mirror_id = self.mirror_content(message, chat_id)
        self.store.mirror(header["message_id"], chat_id, connection_id, message["message_id"])
        self.store.mirror(mirror_id, chat_id, connection_id, message["message_id"])
        if not edited and self.store.get("auto", "on") == "on":
            reply = clean_greeting(message.get("text", ""))
            if reply:
                self.create_draft(chat_id, connection_id, message["message_id"], reply, incoming_text, "normal", verbatim=True)
        if self.executor and (self.store.mission(chat_id) or len(incoming_text) > 250 or any(message.get(key) for key in ("voice", "video", "photo"))):
            self.executor.submit(self.analyze_incoming, message)

    def analyze_incoming(self, message):
        """Run expensive reading away from the Telegram polling thread."""
        store = Store(self.store.path)
        try:
            worker = Assistant(self.api, store, self.owner_id)
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            content = message.get("text") or message.get("caption") or ""
            if any(message.get(key) for key in ("voice", "video", "photo")):
                try:
                    extracted = read_media(self.api, message)
                    if extracted:
                        content = (content + "\n" + extracted).strip()
                        store.save_archive(chat_id, message_id, "in", "extracted", content, message.get("date"))
                        store.save_incoming(chat_id, message_id, content)
                except Exception as exc:
                    LOG.exception("Media analysis failed for chat %s", chat_id)
                    worker.tell("Не удалось разобрать вложение из чата {}: {}. Исходный файл уже переслан вам выше.".format(chat_id, str(exc)[:200]))
            if content and (len(content) > 250 or any(message.get(key) for key in ("voice", "video", "photo"))):
                worker.tell("Разбор сообщения из чата {} (№{}):\n{}".format(chat_id, message_id, brief(content)))
            mission = store.mission(chat_id)
            if mission and content:
                notes = [item[1] for item in store.memories(chat_id, 5)]
                try:
                    proposed = mission_reply(mission[0], mission[1], notes, content)
                    worker.create_draft(chat_id, message["business_connection_id"], message_id, proposed, content)
                    worker.tell("Это предложение для миссии «{}». Проверьте смысл перед отправкой.".format(mission[0]))
                except Exception as exc:
                    LOG.exception("Mission suggestion failed for chat %s", chat_id)
                    worker.tell("Миссия для чата {} активна, но сейчас не удалось подготовить ответ: {}. Можно ответить вручную на сообщение выше.".format(chat_id, str(exc)[:160]))
        finally:
            store.db.close()

    def transcribe(self, file_id):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("Для голосовых команд установите зависимости: pip install -r requirements.txt") from exc
        if self.voice_model is None:
            threads = max(1, min(8, int(os.getenv("WHISPER_THREADS", "2"))))
            self.voice_model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8", cpu_threads=threads)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.ogg"
            self.api.download(file_id, path)
            segments, _ = self.voice_model.transcribe(str(path), language="ru", beam_size=5)
            return " ".join(segment.text.strip() for segment in segments).strip()

    def on_owner_media(self, message, target, preview_id):
        if message.get("photo"):
            kind, file_id = "photo", message["photo"][-1]["file_id"]
        elif message.get("video"):
            kind, file_id = "video", message["video"]["file_id"]
        else:
            kind, file_id = "sticker", message["sticker"]["file_id"]
        caption = message.get("caption", "").strip() if kind != "sticker" else ""
        if not target and preview_id:
            draft = self.store.draft(preview_id)
            if draft and draft[8] == "pending":
                target = draft[1:4]
        if not target:
            command = re.fullmatch(r"/send\s+(\S+)(?:\s+([\s\S]+))?", caption, re.I)
            if not command:
                self.tell("Ответьте фото, видео или стикером на копию сообщения контакта. Фото и видео можно также отправить с подписью /send Имя [текст].")
                return
            matches = self.store.resolve(command.group(1))
            if len(matches) != 1:
                self.tell("Контакт не найден или имя неоднозначно. Откройте /chats и уточните имя через /alias.")
                return
            chat_id, _, _, connection_id = matches[0]
            target = (chat_id, connection_id, None)
            caption = (command.group(2) or "").strip()
        chat_id, connection_id, original_id = target
        self.create_media_draft(chat_id, connection_id, original_id, kind, file_id, message["message_id"], caption)

    def on_owner_message(self, message):
        sender = message.get("from", {})
        chat = message.get("chat", {})
        if sender.get("id") != self.owner_id or chat.get("type") != "private":
            return
        replied = message.get("reply_to_message", {}).get("message_id")
        target = self.store.mirrored(replied) if replied else None
        preview_id = self.store.preview_draft(replied) if replied else None
        if message.get("photo") or message.get("video") or message.get("sticker"):
            self.on_owner_media(message, target, preview_id)
            return
        text = message.get("text", "")
        if message.get("voice"):
            voice = message["voice"]
            if voice.get("duration", 0) > 60:
                self.tell("Голосовая команда должна быть не длиннее 60 секунд.")
                return
            text = self.transcribe(voice["file_id"])
            self.tell("Распознано: " + (text or "[пусто]"))
            for spoken, command in (("найди ", "/find "), ("запомни ", "/remember "), ("миссия ", "/mission ")):
                if text.casefold().startswith(spoken):
                    text = command + text[len(spoken):]
                    break
        if text == "/myid":
            self.tell("Ваш Telegram ID: {}".format(self.owner_id))
        elif text in ("/start", "/help"):
            self.tell("Как отвечать: ответьте на копию входящего сообщения текстом, голосовым, фото, видео или стикером. Всё уходит человеку только после «Отправить». Текст показывается без изменений, стиль применяется кнопкой.\n\n/chats — контакты; /alias ID Имя — своё имя\n/send Имя текст — текстовый черновик\n/remember Имя | факт — память; можно ответить на входящее: /remember факт\n/memory Имя — память и миссия контакта; /forget номер — удалить запись\n/mission Имя | цель | ограничения — следить за разговором и предлагать черновики; /stopmission Имя — остановить\n/find слова — поиск по новым сообщениям за 90 дней\n/mode режим — быстрый выбор стиля; /example текст — образец манеры\n/autoon, /autooff — подсказки приветствий; /status — состояние.\nДлинные входящие, голосовые, видео и текст со скриншотов разбираются в фоне. Не отправляйте секреты в /example.")
        elif re.fullmatch(r"/chats(?:\s+\d+)?", text):
            page = int(text.split()[1]) if len(text.split()) > 1 else 1
            if page < 1:
                self.tell("Номер страницы должен быть от 1.")
                return
            rows = self.store.recent(page)
            self.tell("Контакты, страница {}:\n".format(page) + "\n".join("{} · {}{}{}".format(row[0], row[1], " @" + row[2] if row[2] else "", " → " + self.store.alias_for(row[0]) if self.store.alias_for(row[0]) else "") for row in rows) if rows else "На этой странице контактов нет.")
        elif text.startswith(("/remember ", "/запомни ")):
            body = text.split(maxsplit=1)[1].strip()
            if target:
                chat_id, _, source_id = target
                note = body
            elif "|" in body:
                query, note = [part.strip() for part in body.split("|", 1)]
                matches = self.store.resolve(query)
                if len(matches) != 1:
                    self.tell("Контакт не найден или имя неоднозначно. Укажите точное имя из /chats.")
                    return
                chat_id, source_id = matches[0][0], None
            else:
                self.tell("Ответьте на сообщение командой /remember текст или напишите /remember Имя | текст.")
                return
            if not note or len(note) > 2000:
                self.tell("Запись должна быть от 1 до 2000 символов.")
                return
            conflicts = self.store.memory_conflicts(chat_id, note)
            memory_id = self.store.add_memory(chat_id, note, source_id)
            self.tell("Сохранено в памяти контакта: #{} — {}".format(memory_id, note))
            if conflicts:
                other_id, other_text = conflicts[0]
                other = self.store.contact(other_id)
                name = self.store.alias_for(other_id) or (other[1] if other else str(other_id))
                self.tell("Проверьте возможное совпадение времени: у {} сохранено «{}».".format(name, other_text[:300]))
        elif text.startswith("/forget "):
            value = text.split(maxsplit=1)[1].strip()
            if not value.isdigit():
                self.tell("Укажите номер записи: /forget 12")
                return
            self.tell("Запись удалена." if self.store.remove_memory(int(value)) else "Запись не найдена.")
        elif text.startswith(("/memory ", "/память ")) or text in ("/memory", "/память"):
            query = text.split(maxsplit=1)[1].strip() if " " in text else ""
            matches = self.store.resolve(query) if query else ([self.store.contact(target[0])] if target else [])
            if len(matches) != 1 or not matches[0]:
                self.tell("Укажите контакт: /memory Имя или ответьте командой на его сообщение.")
                return
            chat_id = matches[0][0]
            notes = self.store.memories(chat_id)
            mission = self.store.mission(chat_id)
            lines = ["Память: {}".format(self.store.alias_for(chat_id) or matches[0][1])]
            if mission:
                lines.append("Активная миссия: {}. Ограничения: {}".format(mission[0], mission[1] or "нет"))
            lines.extend("#{} {}".format(item[0], item[1]) for item in notes)
            self.tell("\n".join(lines) if len(lines) > 1 else "Для этого контакта пока нет записей.")
        elif text.startswith(("/mission ", "/миссия ")):
            body = text.split(maxsplit=1)[1].strip()
            if target:
                chat_id, _, _ = target
                parts = [part.strip() for part in body.split("|", 1)]
            else:
                parts = [part.strip() for part in body.split("|", 2)]
                if len(parts) < 2:
                    self.tell("Формат: /mission Имя | цель | ограничения. Можно ответить на сообщение: /mission цель | ограничения.")
                    return
                matches = self.store.resolve(parts[0])
                if len(matches) != 1:
                    self.tell("Контакт не найден или имя неоднозначно.")
                    return
                chat_id = matches[0][0]
                parts = parts[1:]
            goal = parts[0] if parts else ""
            limits = parts[1] if len(parts) > 1 else ""
            if len(goal) < 4 or len(goal) > 500 or len(limits) > 500:
                self.tell("Цель должна быть от 4 до 500 символов, ограничения — до 500.")
                return
            self.store.set_mission(chat_id, goal, limits)
            self.tell("Миссия для {} запущена. После его следующего сообщения я предложу черновик; отправить его сможете только вы. Цель: {}".format(self.store.alias_for(chat_id) or str(chat_id), goal))
        elif text.startswith(("/stopmission ", "/стопмиссия ")):
            query = text.split(maxsplit=1)[1].strip()
            matches = self.store.resolve(query)
            if len(matches) != 1:
                self.tell("Укажите точное имя контакта из /chats.")
                return
            self.store.stop_mission(matches[0][0])
            self.tell("Миссия остановлена.")
        elif text.startswith(("/find ", "/найди ")):
            query = text.split(maxsplit=1)[1].strip()
            rows = self.store.search_archive(query)
            lines = []
            for memory_id, chat_id, content in self.store.search_memories(query):
                contact = self.store.contact(chat_id)
                name = self.store.alias_for(chat_id) or (contact[1] if contact else str(chat_id))
                lines.append("Память #{} · {}\n{}".format(memory_id, name, content[:300]))
            for chat_id, message_id, direction, kind, content, created_at in rows:
                contact = self.store.contact(chat_id)
                name = self.store.alias_for(chat_id) or (contact[1] if contact else str(chat_id))
                when = datetime.fromtimestamp(created_at).strftime("%d.%m.%Y %H:%M")
                lines.append("{} · {} · {} · сообщение {}\n{}".format(name, when, "от него" if direction == "in" else "от вас", message_id, content[:250]))
            self.tell("Найдено:\n\n" + "\n\n".join(lines) if lines else "Пока ничего не найдено. Поиск охватывает новые сообщения с момента обновления бота.")
        elif text.startswith("/alias "):
            match = re.fullmatch(r"/alias\s+(\d+)\s+(\S+)", text)
            if match:
                alias_chat_id, alias = int(match.group(1)), match.group(2)
            elif target and re.fullmatch(r"/alias\s+\S+", text):
                alias_chat_id, alias = target[0], text.split(maxsplit=1)[1]
            else:
                self.tell("Формат: /alias ID Имя или ответьте на входящее сообщением /alias Имя")
                return
            self.store.set_alias(alias_chat_id, alias)
            self.tell("Теперь чат {} — {}.".format(alias_chat_id, alias))
        elif text.startswith("/mode "):
            requested = text[6:].strip().casefold()
            matches = [key for key, value in MODES.items() if requested in (key.casefold(), value[0].casefold())]
            if not matches:
                self.tell("Неизвестный режим. Варианты: " + ", ".join(value[0] for value in MODES.values()))
                return
            self.store.set("mode", matches[0])
            self.tell("Ваш быстрый режим: {}. Черновики сначала показываются без изменений; примените режим кнопкой под черновиком.".format(MODES[matches[0]][0]))
        elif text.startswith("/example "):
            example = text[9:].strip()
            if len(example) < 4 or len(example) > 500:
                self.tell("Образец должен быть от 4 до 500 символов.")
                return
            self.store.add_sample(example)
            self.tell("Образец вашей манеры сохранён локально.")
        elif text == "/autoon":
            self.store.set("auto", "on")
            self.tell("Подсказки ответов на приветствия включены. Перед отправкой потребуется подтверждение.")
        elif text == "/autooff":
            self.store.set("auto", "off")
            self.tell("Подсказки ответов на приветствия выключены.")
        elif text == "/status":
            self.tell("Подсказки приветствий: {}. Режим: {}. Локальная модель: {}. Известных чатов: {}.".format(self.store.get("auto", "on"), MODES.get(self.store.get("mode", "normal"), MODES["normal"])[0], "готова" if model_ready() else "не запущена", self.store.db.execute("SELECT count(*) FROM contacts").fetchone()[0]))
        elif preview_id and text and not text.startswith("/"):
            draft = self.store.draft(preview_id)
            if not draft or draft[8] != "pending":
                self.tell("Этот черновик уже закрыт.")
                return
            media = self.store.media_draft(preview_id)
            if media and media[0] == "sticker":
                self.tell("У стикера нет подписи. Можно отправить его отдельно или создать текстовый ответ на входящее сообщение.")
                return
            if len(text) > (1024 if media else 3500):
                self.tell("Слишком длинный текст для предпросмотра.")
                return
            self.store.update_draft(preview_id, text, "raw", source_text=text)
            self.show_draft(preview_id, "Исправленный текст сохранён без переработки.")
        else:
            command = parse_command(text, target)
            if not command:
                self.tell("Не понял команду. Откройте /help.")
                return
            operation, query, body = command
            if operation == "reply":
                chat_id, connection_id, original_id = query
                incoming = self.store.incoming(chat_id, original_id)
            else:
                matches = self.store.resolve(query)
                if not matches:
                    self.tell("Контакт не найден. Откройте /chats и назначьте ему имя командой /alias.")
                    return
                if len(matches) > 1:
                    names = ["{} ({})".format(self.store.alias_for(row[0]) or row[1], row[0]) for row in matches[:8]]
                    self.tell("Имя неоднозначно: {}. Назначьте этим чатам разные имена через /alias и повторите команду.".format(", ".join(names)))
                    return
                chat_id, _, _, connection_id = matches[0]
                original_id = None
                incoming = ""
            self.create_draft(chat_id, connection_id, original_id, body, incoming)

    def on_update(self, update):
        if "business_connection" in update:
            info = update["business_connection"]
            if info.get("user", {}).get("id") == self.owner_id:
                self.connections[info["id"]] = info
                self.tell("Business-подключение {}. Право отвечать: {}.".format("активно" if info.get("is_enabled") else "отключено", "да" if info.get("rights", {}).get("can_reply") else "нет"))
        elif "business_message" in update:
            self.on_business_message(update["business_message"])
        elif "edited_business_message" in update:
            self.on_business_message(update["edited_business_message"], edited=True)
        elif "deleted_business_messages" in update:
            deleted = update["deleted_business_messages"]
            if self.connection(deleted["business_connection_id"]):
                self.tell("В чате {} удалены сообщения: {}".format(deleted.get("chat", {}).get("id", "?"), ", ".join(map(str, deleted.get("message_ids", [])))))
        elif "callback_query" in update:
            self.on_callback(update["callback_query"])
        elif "message" in update:
            self.on_owner_message(update["message"])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    owner = os.getenv("TELEGRAM_OWNER_ID")
    if not token or not owner or not owner.isdigit():
        sys.exit("Укажите TELEGRAM_BOT_TOKEN и числовой TELEGRAM_OWNER_ID в переменных окружения.")
    data_dir = Path(os.getenv("BOT_DATA_DIR", str(Path(__file__).parent / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    api = Telegram(token)
    store = Store(data_dir / "bot.sqlite3")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="incoming-analysis")
    assistant = Assistant(api, store, int(owner), executor=executor)
    me = api.call("getMe")
    LOG.info("Started @%s", me.get("username", "unknown"))
    api.call("deleteWebhook", drop_pending_updates=False)
    while True:
        try:
            if int(time.time()) - store.last_purge > 86400:
                store.purge_archive()
            params = {"timeout": 25, "allowed_updates": ["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages", "callback_query"]}
            offset = store.get("offset")
            if offset is not None:
                params["offset"] = int(offset)
            for update in api.call("getUpdates", **params):
                try:
                    assistant.on_update(update)
                except Exception as exc:
                    LOG.exception("Update %s failed", update.get("update_id"))
                    try:
                        assistant.tell("Ошибка обработки сообщения: {}".format(str(exc)[:300]))
                    except Exception:
                        LOG.exception("Could not notify owner")
                store.set("offset", update["update_id"] + 1)
        except KeyboardInterrupt:
            break
        except Exception:
            LOG.exception("Polling failed; retrying")
            time.sleep(5)


if __name__ == "__main__":
    main()
