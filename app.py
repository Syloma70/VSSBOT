"""Volkan Arslan için 7/24 Telegram kontrol servisi ve PC karar API'si."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CONTROL_SECRET = os.getenv("CONTROL_SECRET", "").strip()
VOLKAN_USERNAME = os.getenv("VOLKAN_USERNAME", "vlknarslan").lstrip("@").casefold()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "jacktheripppper").lstrip("@").casefold()
PORT = int(os.getenv("PORT", "8080"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "data"))
DB_PATH = DATA_DIR / "vssbot.sqlite3"
STOP_EVENT = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database():
    connection = db_connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def prepare_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                telegram_user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                command TEXT NOT NULL,
                resulting_mode TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('mode', 'off')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('telegram_offset', '0')"
        )


def get_setting(key: str, default: str = "") -> str:
    with database() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_setting(key: str, value: str, db: sqlite3.Connection | None = None) -> None:
    query = (
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    if db is not None:
        db.execute(query, (key, value))
        return
    with database() as connection:
        connection.execute(query, (key, value))


def telegram_request(method: str, fields: dict, timeout: int = 40) -> dict:
    payload = json.dumps(fields, ensure_ascii=True).encode("ascii")
    request = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description") or "Telegram API error"))
    return result


HELP_TEXT = (
    "🚀 <b>TICARION OTOMASYON</b>\n"
    "<b>Volkan Arslan Hesap Kontrol Paneli</b>\n\n"
    "⚡ <b>Tek seferlik çalışma</b>\n"
    "<code>/calistir</code> — Hesabın yalnız sıradaki otomatik tura katılır.\n\n"
    "🔄 <b>Sürekli çalışma</b>\n"
    "<code>/surekli</code> — Hesabın sen kapatana kadar her otomatik tura katılır.\n\n"
    "🛑 <b>Tümünü durdur</b>\n"
    "<code>/iptal</code> — Tek seferlik isteği ve sürekli modu kapatır.\n\n"
    "📊 <b>Durumu öğren</b>\n"
    "<code>/durum</code> — Kayıtlı çalışma tercihini gösterir.\n\n"
    "⚠️ Oyuna kendin gireceğin zaman çift giriş yaşamamak için önce "
    "<code>/iptal</code> gönder."
)


def send_message(chat_id: int | str, text: str, parse_mode: str = "HTML") -> None:
    fields = {"chat_id": chat_id, "text": text}
    if parse_mode:
        fields["parse_mode"] = parse_mode
    telegram_request("sendMessage", fields)


def authorized_role(message: dict) -> str:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    username = str(sender.get("username") or chat.get("username") or "").casefold()
    user_id = str(sender.get("id") or "")
    if not user_id or chat.get("type") != "private":
        return ""
    for role, allowed_username, setting_key in (
        ("admin", ADMIN_USERNAME, "admin_user_id"),
        ("volkan", VOLKAN_USERNAME, "volkan_user_id"),
    ):
        saved_user_id = get_setting(setting_key)
        if saved_user_id and user_id == saved_user_id:
            return role
        if not saved_user_id and username == allowed_username:
            set_setting(setting_key, user_id)
            return role
    return ""


def normalize_command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].casefold()


def mode_text(mode: str) -> str:
    return {
        "once": "⚡ Tek seferlik çalışma bekliyor.",
        "always": "🔄 Sürekli çalışma açık.",
        "off": "⛔ Otomatik çalışma kapalı.",
    }.get(mode, "⛔ Otomatik çalışma kapalı.")


def apply_command(message: dict) -> None:
    role = authorized_role(message)
    if not role:
        return
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    command = normalize_command(str(message.get("text") or ""))
    current_mode = get_setting("mode", "off")
    response = ""
    new_mode = current_mode
    if command in {"/start", "/yardim", "/help"}:
        response = HELP_TEXT
    elif command in {"/calistir", "/volkan", "/aktif"}:
        new_mode = "once"
        response = (
            "✅ <b>Tek seferlik çalışma kaydedildi.</b>\n\n"
            "Volkan Arslan hesabı sıradaki PC otomasyon turuna dahil edilecek. "
            "Karar uygulandıktan sonra hesap yeniden pasif olacak."
        )
    elif command in {"/surekli", "/surekli_ac"}:
        new_mode = "always"
        response = (
            "🔄 <b>Sürekli çalışma açıldı.</b>\n\n"
            "Volkan Arslan hesabı <b>/iptal</b> komutu gönderilene kadar her otomatik tura katılacak."
        )
    elif command in {"/iptal", "/pasif", "/surekli_kapat", "/surekli_pasif"}:
        new_mode = "off"
        response = (
            "🛑 <b>Tüm çalışma istekleri iptal edildi.</b>\n\n"
            "Volkan Arslan hesabı yeni bir komut verilene kadar otomasyona alınmayacak."
        )
    elif command == "/durum":
        response = f"📊 <b>Volkan Arslan otomasyon durumu</b>\n\n{mode_text(current_mode)}"
    elif command.startswith("/"):
        response = "❓ Bu komutu tanımıyorum. Kullanılabilir komutlar için /yardim yaz."
    if not response:
        return
    if new_mode != current_mode:
        with database() as db:
            set_setting("mode", new_mode, db)
            db.execute(
                """
                INSERT INTO command_log(created_at, telegram_user_id, username, command, resulting_mode)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    str(sender.get("id") or ""),
                    str(sender.get("username") or ""),
                    f"{role}:{command}",
                    new_mode,
                ),
            )
    send_message(chat_id, response)


def telegram_loop() -> None:
    while not STOP_EVENT.is_set():
        try:
            offset = int(get_setting("telegram_offset", "0"))
            updates = telegram_request(
                "getUpdates",
                {"offset": offset, "limit": 50, "timeout": 30, "allowed_updates": ["message"]},
                timeout=40,
            ).get("result", [])
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message") or {}
                if message:
                    apply_command(message)
            if updates:
                set_setting("telegram_offset", str(offset))
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as error:
            print(f"Telegram polling error: {type(error).__name__}: {error}", flush=True)
            STOP_EVENT.wait(5)
        except Exception as error:
            print(f"Unexpected polling error: {type(error).__name__}: {error}", flush=True)
            STOP_EVENT.wait(5)


def claim_decision() -> dict:
    """PC turu için kararı atomik oku; tek seferlik izni aynı işlemde tüket."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
        mode = str(row[0]) if row else "off"
        active = mode in {"once", "always"}
        if mode == "once":
            set_setting("mode", "off", db)
        db.commit()
    return {
        "active": active,
        "mode": mode,
        "consumed": mode == "once",
        "claimed_at": utc_now(),
    }


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "VSSBOT/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"HTTP {self.address_string()} - {fmt % args}", flush=True)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized_request(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = self.headers.get("X-Control-Secret", "")
        return bool(CONTROL_SECRET) and supplied == CONTROL_SECRET

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True, "service": "vssbot", "time": utc_now()})
            return
        if self.path == "/api/status":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            mode = get_setting("mode", "off")
            self.send_json(200, {"ok": True, "mode": mode, "active": mode != "off"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/api/claim":
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.authorized_request():
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        self.send_json(200, {"ok": True, **claim_decision()})


def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CONTROL_SECRET:
        missing.append("CONTROL_SECRET")
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))


def stop_service(*_args) -> None:
    STOP_EVENT.set()


def main() -> None:
    validate_config()
    prepare_database()
    signal.signal(signal.SIGTERM, stop_service)
    signal.signal(signal.SIGINT, stop_service)
    polling = threading.Thread(target=telegram_loop, name="telegram-polling", daemon=True)
    polling.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ApiHandler)
    print(
        f"VSSBOT listening on port {PORT}; users: "
        f"@{VOLKAN_USERNAME} (owner), @{ADMIN_USERNAME} (admin)",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOP_EVENT.set()
        server.server_close()


if __name__ == "__main__":
    main()
