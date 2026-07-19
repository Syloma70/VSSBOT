"""Volkan Arslan için 7/24 Telegram kontrol servisi ve PC karar API'si."""

from __future__ import annotations

from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
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
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('schedule_enabled', '1')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('schedule_hours', '')"
        )
        if not db.execute(
            "SELECT 1 FROM settings WHERE key='schedule_times'"
        ).fetchone():
            old_hours = str(db.execute(
                "SELECT value FROM settings WHERE key='schedule_hours'"
            ).fetchone()[0])
            migrated = ",".join(
                f"{int(value):02d}:00" for value in old_hours.split(",") if value != ""
            )
            db.execute(
                "INSERT INTO settings(key, value) VALUES('schedule_times', ?)",
                (migrated,),
            )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('event_enabled', '0')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('bot_enabled', '1')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('agent_status', '{}')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('viewer_password_hash', '')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('skip_requests', '[]')"
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('account_catalog', '[]')"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        session_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "username" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN username TEXT NOT NULL DEFAULT ''")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_user_accounts (
                username TEXT NOT NULL,
                account_name TEXT NOT NULL,
                PRIMARY KEY(username, account_name),
                FOREIGN KEY(username) REFERENCES app_users(username) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                details TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                account TEXT NOT NULL,
                message TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT,
                resolved_by TEXT NOT NULL DEFAULT '',
                last_event_key TEXT NOT NULL DEFAULT ''
            )
            """
        )
        incident_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(incidents)").fetchall()
        }
        if "last_event_key" not in incident_columns:
            db.execute("ALTER TABLE incidents ADD COLUMN last_event_key TEXT NOT NULL DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, last_seen)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS account_timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                account TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info'
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_timeline_account ON account_timeline(account, occurred_at)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS event_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                account TEXT NOT NULL,
                reward TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_rewards_date ON event_rewards(occurred_at)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL UNIQUE,
                occurred_at TEXT NOT NULL,
                account TEXT NOT NULL,
                operation TEXT NOT NULL,
                result TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings(occurred_at)")


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


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"{salt.hex()}:{digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = password_hash(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def normalize_app_username(username: str) -> str:
    username = username.strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
        raise ValueError("Kullanıcı adı 3-32 karakter olmalı; harf, rakam, nokta, alt çizgi ve tire kullanılabilir.")
    return username


def save_app_user(username: str, password: str, accounts: list[str]) -> dict:
    username = normalize_app_username(username)
    if not isinstance(accounts, list):
        raise ValueError("Hesap listesi geçersiz.")
    clean_accounts = []
    seen = set()
    for value in accounts:
        account = str(value).strip()
        key = account.casefold()
        if account and key not in seen:
            seen.add(key)
            clean_accounts.append(account[:200])
    with database() as db:
        row = db.execute("SELECT password_hash FROM app_users WHERE username=?", (username,)).fetchone()
        if not row and len(password) < 8:
            raise ValueError("Yeni kullanıcı şifresi en az 8 karakter olmalı.")
        if row and password and len(password) < 8:
            raise ValueError("Yeni şifre en az 8 karakter olmalı.")
        hashed = password_hash(password) if password else str(row[0])
        now = utc_now()
        db.execute(
            """INSERT INTO app_users(username, password_hash, active, created_at, updated_at)
               VALUES(?, ?, 1, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                   password_hash=excluded.password_hash, active=1, updated_at=excluded.updated_at""",
            (username, hashed, now, now),
        )
        db.execute("DELETE FROM app_user_accounts WHERE username=?", (username,))
        db.executemany(
            "INSERT INTO app_user_accounts(username, account_name) VALUES(?, ?)",
            [(username, account) for account in clean_accounts],
        )
        db.execute("DELETE FROM sessions WHERE username=?", (username,))
    return {"username": username, "accounts": clean_accounts, "active": True}


def list_app_users() -> list[dict]:
    with database() as db:
        rows = db.execute(
            "SELECT username, active, created_at, updated_at FROM app_users ORDER BY username"
        ).fetchall()
        assignments = db.execute(
            "SELECT username, account_name FROM app_user_accounts ORDER BY username, account_name"
        ).fetchall()
    by_user: dict[str, list[str]] = defaultdict(list)
    for row in assignments:
        by_user[str(row["username"])].append(str(row["account_name"]))
    return [
        {"username": str(row["username"]), "active": bool(row["active"]),
         "accounts": by_user[str(row["username"])], "created_at": row["created_at"],
         "updated_at": row["updated_at"]}
        for row in rows
    ]


def delete_app_user(username: str) -> dict:
    username = normalize_app_username(username)
    with database() as db:
        db.execute("DELETE FROM app_user_accounts WHERE username=?", (username,))
        deleted = db.execute("DELETE FROM app_users WHERE username=?", (username,)).rowcount > 0
        db.execute("DELETE FROM sessions WHERE username=?", (username,))
    return {"username": username, "deleted": deleted}


def assigned_accounts(username: str) -> list[str]:
    with database() as db:
        rows = db.execute(
            "SELECT account_name FROM app_user_accounts WHERE username=? ORDER BY account_name",
            (username,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def create_session(username: str, password: str) -> dict:
    supplied_username = username.strip().casefold()
    if CONTROL_SECRET and hmac.compare_digest(password, CONTROL_SECRET) and supplied_username in {"", "admin", "yonetici"}:
        role = "admin"
        supplied_username = "admin"
    else:
        supplied_username = normalize_app_username(supplied_username)
        with database() as db:
            row = db.execute(
                "SELECT password_hash, active FROM app_users WHERE username=?", (supplied_username,)
            ).fetchone()
        if not row or not bool(row["active"]) or not password_matches(password, str(row["password_hash"])):
            raise PermissionError("Kullanıcı adı veya şifre geçersiz.")
        role = "viewer"
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    with database() as db:
        db.execute(
            "INSERT INTO sessions(token_hash, role, username, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
            (hashlib.sha256(token.encode("utf-8")).hexdigest(), role, supplied_username,
             expires.isoformat(timespec="seconds"), utc_now()),
        )
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (utc_now(),))
    return {"token": token, "role": role, "username": supplied_username,
            "expires_at": expires.isoformat(timespec="seconds")}


def session_identity(token: str) -> dict | None:
    if CONTROL_SECRET and hmac.compare_digest(token, CONTROL_SECRET):
        return {"role": "admin", "username": "admin"}
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with database() as db:
        row = db.execute(
            "SELECT role, username, expires_at FROM sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
    if not row:
        return None
    try:
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    return {"role": str(row["role"]), "username": str(row["username"] or "")}


def session_role(token: str) -> str | None:
    identity = session_identity(token)
    return str(identity["role"]) if identity else None


def record_audit(actor: str, role: str, action: str, target: str = "", details: dict | None = None) -> None:
    with database() as db:
        db.execute(
            "INSERT INTO audit_log(created_at, actor, role, action, target, details) VALUES(?, ?, ?, ?, ?, ?)",
            (utc_now(), actor[:100], role[:20], action[:100], target[:200],
             json.dumps(details or {}, ensure_ascii=True, separators=(",", ":"))[:4000]),
        )


def audit_entries(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with database() as db:
        rows = db.execute(
            "SELECT id, created_at, actor, role, action, target, details FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(str(item["details"]))
        except json.JSONDecodeError:
            item["details"] = {}
        result.append(item)
    return result


def record_timeline(event_key: str, occurred_at: str, account: str, kind: str,
                    title: str, detail: str = "", severity: str = "info",
                    db: sqlite3.Connection | None = None) -> None:
    values = (
        str(event_key)[:500], str(occurred_at or utc_now())[:100],
        str(account or "Sistem").strip()[:200], str(kind)[:50],
        str(title).strip()[:500], str(detail).strip()[:2000], str(severity)[:20],
    )
    query = """INSERT OR IGNORE INTO account_timeline
               (event_key, occurred_at, account, kind, title, detail, severity)
               VALUES(?, ?, ?, ?, ?, ?, ?)"""
    if db is not None:
        db.execute(query, values)
    else:
        with database() as connection:
            connection.execute(query, values)


def record_timeline_from_agent(payload: dict) -> None:
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    account = str(progress.get("current_account") or "").strip()
    if not account:
        return
    tour_id = str(progress.get("tour_id") or "")
    updated = str(progress.get("updated_at") or payload.get("received_at") or utc_now())
    status = str(progress.get("status") or payload.get("task_state") or "").strip()
    action = str(progress.get("last_action") or "").strip()
    error = str(progress.get("last_error") or "").strip()
    signature = hashlib.sha256(f"{tour_id}|{account}|{status}|{action}|{error}".encode("utf-8")).hexdigest()
    if error:
        title, detail, severity, kind = "Hata oluştu", error, "warning", "error"
    elif action:
        title, detail, severity, kind = action, status, "info", "activity"
    else:
        title, detail, severity, kind = "Hesap işleniyor", status, "info", "activity"
    record_timeline(f"agent:{signature}", updated, account, kind, title, detail, severity)


def timeline_entries(allowed: list[str] | None = None, account: str = "", limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with database() as db:
        if account:
            rows = db.execute(
                """SELECT id, occurred_at, account, kind, title, detail, severity
                   FROM account_timeline WHERE account=? COLLATE NOCASE
                   ORDER BY occurred_at DESC, id DESC LIMIT ?""", (account, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT id, occurred_at, account, kind, title, detail, severity
                   FROM account_timeline ORDER BY occurred_at DESC, id DESC LIMIT ?""", (limit,),
            ).fetchall()
    allowed_keys = None if allowed is None else {str(value).casefold() for value in allowed}
    return [dict(row) for row in rows if allowed_keys is None or str(row["account"]).casefold() in allowed_keys]


def record_incident(kind: str, severity: str, account: str, message: str, event_key: str) -> None:
    message = " ".join(str(message).split()).strip()
    if not message:
        return
    account = str(account or "Sistem").strip()[:200]
    fingerprint = hashlib.sha256(
        f"{kind.casefold()}|{account.casefold()}|{message.casefold()}".encode("utf-8")
    ).hexdigest()
    now = utc_now()
    with database() as db:
        row = db.execute(
            "SELECT occurrence_count, last_event_key FROM incidents WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row:
            count = int(row["occurrence_count"]) + (str(row["last_event_key"]) != event_key)
            db.execute(
                """UPDATE incidents SET severity=?, occurrence_count=?, last_seen=?, status='open',
                   resolved_at=NULL, resolved_by='', last_event_key=? WHERE fingerprint=?""",
                (severity, count, now, event_key, fingerprint),
            )
        else:
            db.execute(
                """INSERT INTO incidents(fingerprint, kind, severity, account, message,
                   occurrence_count, first_seen, last_seen, status, last_event_key)
                   VALUES(?, ?, ?, ?, ?, 1, ?, ?, 'open', ?)""",
                (fingerprint, kind, severity, account, message[:2000], now, now, event_key),
            )


def record_incidents_from_agent(payload: dict) -> None:
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    tour_id = str(progress.get("tour_id") or "")
    updated = str(progress.get("updated_at") or payload.get("received_at") or utc_now())
    last_error = str(progress.get("last_error") or "").strip()
    if last_error:
        record_incident(
            "account_error", "warning", str(progress.get("current_account") or "Bilinmeyen hesap"),
            last_error, f"{tour_id}|{updated}|account_error",
        )
    if str(payload.get("health") or "") == "error":
        message = str(payload.get("message") or "Bot hata durumuna geçti.")
        record_incident("bot_error", "critical", "Sistem", message, f"{payload.get('last_run')}|{message}")


def incident_entries(allowed: list[str] | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with database() as db:
        rows = db.execute(
            """SELECT id, kind, severity, account, message, occurrence_count, first_seen,
               last_seen, status, resolved_at, resolved_by
               FROM incidents ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    allowed_keys = None if allowed is None else {value.casefold() for value in allowed}
    result = []
    for row in rows:
        if allowed_keys is not None and str(row["account"]).casefold() not in allowed_keys:
            continue
        item = dict(row)
        count = int(item["occurrence_count"])
        item["alert_level"] = "critical" if count >= 3 or item["severity"] == "critical" else "warning"
        milestone = 3 if count >= 3 else 1 if count == 1 else 0
        item["alert_milestone"] = milestone
        item["should_notify"] = item["status"] == "open" and milestone > 0
        item["notification_reason"] = (
            "İlk hata" if count == 1 else "Hata 3 kez tekrarlandı" if count >= 3 else "Tekrar alarmı susturuldu"
        )
        result.append(item)
    return result


def resolve_incident(incident_id: int, actor: str) -> dict:
    with database() as db:
        row = db.execute("SELECT account FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not row:
            raise ValueError("Olay bulunamadı.")
        db.execute(
            "UPDATE incidents SET status='resolved', resolved_at=?, resolved_by=? WHERE id=?",
            (utc_now(), actor[:100], incident_id),
        )
    return {"id": incident_id, "status": "resolved", "account": str(row["account"])}


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
    "🕐 <b>Otomatik tur saatleri (yalnız yönetici)</b>\n"
    "<code>/saat 04.16 08:30 14</code> — Seçilen saat ve dakikalarda çalıştırır.\n"
    "<code>/saat her</code> — Her saat çalıştırır.\n"
    "<code>/saat kapat</code> — Otomatik turları kapatır.\n"
    "<code>/saatler</code> — Geçerli saat planını gösterir.\n\n"
    "🎁 <b>Etkinlik raporları</b>\n"
    "<code>/etkinlik [gün]</code> — Çıkan ödüllerin toplam listesi.\n"
    "<code>/etkinlikhesap [gün]</code> — Hesap bazlı ödül listesi.\n\n"
    "🎛 <b>Etkinlik kontrolü</b>\n"
    "<code>/etkinlikac</code> — Etkinlik kontrollerini açar.\n"
    "<code>/etkinlikkapat</code> — Kodu silmeden etkinliği kapatır.\n"
    "<code>/etkinlikdurum</code> — Etkinlik anahtarını gösterir.\n\n"
    "⚠️ Oyuna kendin gireceğin zaman çift giriş yaşamamak için önce "
    "<code>/iptal</code> gönder."
)


def split_text(text: str, limit: int = 4000) -> list[str]:
    parts = []
    remaining = text or ""
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining or not parts:
        parts.append(remaining)
    return parts


def send_message(chat_id: int | str, text: str, parse_mode: str = "HTML") -> None:
    for part in split_text(text):
        fields = {"chat_id": chat_id, "text": part}
        if parse_mode:
            fields["parse_mode"] = parse_mode
        telegram_request("sendMessage", fields)


def event_rows(days: int | None = None) -> list[sqlite3.Row]:
    query = "SELECT occurred_at, account, reward FROM event_rewards"
    params: tuple = ()
    if days is not None:
        query += " WHERE datetime(occurred_at) >= datetime('now', ?)"
        params = (f"-{days} days",)
    query += " ORDER BY occurred_at, id"
    with database() as db:
        return db.execute(query, params).fetchall()


def event_report(days: int | None = None, by_account: bool = False) -> str:
    rows = event_rows(days)
    if not rows:
        return "📭 Bu dönem için etkinlik kutusu kaydı bulunamadı."
    period = f"Son {days} gün" if days is not None else "Tüm zamanlar"
    if not by_account:
        rewards: dict[str, int] = defaultdict(int)
        for row in rows:
            rewards[str(row["reward"])] += 1
        lines = [
            "🎁 ETKİNLİK KUTUSU LİSTESİ", period,
            f"📦 {len(rows)} kutu • 👥 {len({row['account'] for row in rows})} hesap",
            "", "🏆 ÇIKAN ÖDÜLLER",
        ]
        for reward, count in sorted(rewards.items(), key=lambda item: (-item[1], item[0].casefold())):
            lines.append(f"• {reward}: {count} kez")
        lines.extend(["", "Hesap dökümü: /etkinlikhesap"])
        return "\n".join(lines)

    accounts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        accounts[str(row["account"])][str(row["reward"])] += 1
    lines = [
        "👤 HESAP BAZLI ETKİNLİK LİSTESİ", period,
        f"📦 {len(rows)} kutu • 👥 {len(accounts)} hesap",
    ]
    ordered_accounts = sorted(accounts.items(), key=lambda item: item[0].casefold())
    for index, (account, rewards) in enumerate(ordered_accounts, start=1):
        lines.append(f"\n{index}. {account} — {sum(rewards.values())} kutu")
        for reward, count in sorted(
            rewards.items(), key=lambda item: (-item[1], item[0].casefold())
        ):
            lines.append(f"   • {reward}" + (f" ×{count}" if count > 1 else ""))
    return "\n".join(lines)


def command_days(text: str) -> int | None:
    parts = (text or "").strip().split()
    if len(parts) < 2 or not parts[-1].isdigit():
        return None
    return max(1, min(int(parts[-1]), 3650))


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


def schedule_state() -> dict:
    enabled = get_setting("schedule_enabled", "1") == "1"
    raw_times = get_setting("schedule_times", "")
    times = []
    for value in raw_times.split(","):
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                times.append(f"{hour:02d}:{minute:02d}")
        except (ValueError, TypeError):
            continue
    times = sorted(set(times))
    hours = [int(value[:2]) for value in times if value.endswith(":00")]
    return {"enabled": enabled, "times": times, "hours": hours, "timezone": "Europe/Istanbul"}


def schedule_text() -> str:
    state = schedule_state()
    if not state["enabled"]:
        plan = "KAPALI"
    elif not state["times"]:
        plan = "Her saat başı"
    else:
        plan = ", ".join(state["times"])
    return f"🕐 <b>Otomatik tur planı</b>\n\n{plan}\nSaat dilimi: Türkiye"


def event_control_state() -> dict:
    return {"enabled": get_setting("event_enabled", "0") == "1"}


def save_event_control(enabled: bool) -> dict:
    set_setting("event_enabled", "1" if enabled else "0")
    return event_control_state()


def dashboard_state() -> dict:
    mode = get_setting("mode", "off")
    with database() as db:
        event_count = int(db.execute("SELECT COUNT(*) FROM event_rewards").fetchone()[0])
        account_count = int(
            db.execute("SELECT COUNT(DISTINCT account) FROM event_rewards").fetchone()[0]
        )
        last_event = db.execute(
            "SELECT occurred_at, account, reward FROM event_rewards ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return {
        "mode": mode,
        "active": mode != "off",
        "bot": bot_status_state(),
        "schedule": schedule_state(),
        "earnings": earnings_dashboard(),
        "event": {
            **event_control_state(),
            "reward_count": event_count,
            "account_count": account_count,
            "last_reward": dict(last_event) if last_event else None,
        },
        "server_time": utc_now(),
    }


def bot_status_state() -> dict:
    enabled = get_setting("bot_enabled", "1") == "1"
    try:
        agent = json.loads(get_setting("agent_status", "{}"))
        if not isinstance(agent, dict):
            agent = {}
    except (ValueError, TypeError, json.JSONDecodeError):
        agent = {}
    received_at = str(agent.get("received_at") or "")
    age_seconds = None
    if received_at:
        try:
            received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            age_seconds = max(0, int((datetime.now(timezone.utc) - received).total_seconds()))
        except ValueError:
            pass
    if not enabled:
        health = "off"
    elif age_seconds is None or age_seconds > 45:
        health = "offline"
    else:
        health = str(agent.get("health") or "idle")
    progress = agent.get("progress") if isinstance(agent.get("progress"), dict) else {}
    return {
        "enabled": enabled,
        "health": health,
        "heartbeat_age_seconds": age_seconds,
        "task_state": str(agent.get("task_state") or "unknown"),
        "last_run": agent.get("last_run"),
        "last_result": agent.get("last_result"),
        "next_run": agent.get("next_run"),
        "message": str(agent.get("message") or ""),
        "progress": progress,
    }


def save_bot_control(enabled: bool) -> dict:
    set_setting("bot_enabled", "1" if enabled else "0")
    return bot_status_state()


def save_agent_status(payload: dict) -> dict:
    allowed = {
        "health", "task_state", "last_run", "last_result", "next_run", "message",
        "progress",
    }
    clean = {key: payload.get(key) for key in allowed if key in payload}
    clean["received_at"] = utc_now()
    set_setting("agent_status", json.dumps(clean, ensure_ascii=True, separators=(",", ":")))
    record_incidents_from_agent(clean)
    record_timeline_from_agent(clean)
    progress = clean.get("progress") if isinstance(clean.get("progress"), dict) else {}
    names = progress.get("account_names") if isinstance(progress.get("account_names"), list) else []
    if names:
        existing = account_catalog()
        by_key = {value.casefold(): value for value in existing}
        for value in names:
            name = str(value).strip()
            if name:
                by_key.setdefault(name.casefold(), name[:200])
        set_setting("account_catalog", json.dumps(list(by_key.values()), ensure_ascii=True))
    return bot_status_state()


def account_catalog() -> list[str]:
    try:
        stored = json.loads(get_setting("account_catalog", "[]"))
    except json.JSONDecodeError:
        stored = []
    values = [str(value).strip() for value in stored if str(value).strip()] if isinstance(stored, list) else []
    with database() as db:
        rows = db.execute(
            "SELECT DISTINCT account FROM earnings UNION SELECT DISTINCT account FROM event_rewards"
        ).fetchall()
    by_key = {value.casefold(): value for value in values}
    for row in rows:
        value = str(row[0] or "").strip()
        if value:
            by_key.setdefault(value.casefold(), value)
    return sorted(by_key.values(), key=str.casefold)


def account_status_state(allowed_accounts: list[str] | None = None) -> dict:
    bot = bot_status_state()
    progress = bot.get("progress") or {}
    all_names = progress.get("account_names") if isinstance(progress.get("account_names"), list) else []
    allowed_by_key = None if allowed_accounts is None else {
        str(value).casefold(): str(value) for value in allowed_accounts
    }
    indexed_names = [
        (position, value) for position, value in enumerate(all_names, start=1)
        if allowed_by_key is None or str(value).casefold() in allowed_by_key
    ]
    skipped = {
        str(value).casefold() for value in progress.get("skipped_accounts", [])
        if isinstance(value, str)
    }
    index = int(progress.get("account_index") or 0)
    status = str(progress.get("status") or "")
    accounts = []
    for original_position, value in indexed_names:
        name = str(value)
        if name.casefold() in skipped:
            state = "skipped"
        elif status == "completed" or (index > 0 and original_position < index):
            state = "completed"
        elif status == "running" and original_position == index:
            state = "running"
        else:
            state = "pending"
        accounts.append({"name": name, "position": len(accounts) + 1, "status": state})
    if allowed_by_key is not None:
        present = {str(item["name"]).casefold() for item in accounts}
        for account in allowed_accounts or []:
            if str(account).casefold() not in present:
                accounts.append({
                    "name": str(account), "position": len(accounts) + 1,
                    "status": "not_scheduled",
                })
    public_progress = dict(progress)
    public_progress.pop("completed_accounts", None)
    public_progress["account_names"] = [str(value) for _, value in indexed_names]
    public_progress["skipped_accounts"] = [
        str(item["name"]) for item in accounts if item["status"] == "skipped"
    ]
    public_progress["completed_count"] = sum(
        1 for item in accounts if item["status"] in {"completed", "skipped"}
    )
    current_account = str(public_progress.get("current_account") or "")
    if allowed_by_key is not None:
        public_progress["total_accounts"] = len(accounts)
        visible_index = next(
            (position for position, item in enumerate(accounts, start=1)
             if str(item["name"]).casefold() == current_account.casefold()),
            0,
        )
        public_progress["account_index"] = visible_index
        if not visible_index:
            public_progress["current_account"] = ""
            public_progress["last_action"] = ""
            public_progress["last_error"] = ""
    public_bot = dict(bot)
    public_bot["progress"] = public_progress
    return {
        "role": None,
        "bot": public_bot,
        "tour_id": str(progress.get("tour_id") or ""),
        "phase": str(progress.get("phase") or ""),
        "accounts": accounts,
        "server_time": utc_now(),
    }


def account_detail(account: str) -> dict:
    account = account.strip()
    if not account:
        raise ValueError("Hesap adı gerekli.")
    today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
    with database() as db:
        recent = db.execute(
            """SELECT occurred_at, operation, result FROM earnings
               WHERE account=? COLLATE NOCASE ORDER BY id DESC LIMIT 20""",
            (account,),
        ).fetchall()
        daily_count = int(db.execute(
            "SELECT COUNT(*) FROM earnings WHERE account=? COLLATE NOCASE AND substr(occurred_at,1,10)=?",
            (account, today),
        ).fetchone()[0])
        all_count = int(db.execute(
            "SELECT COUNT(*) FROM earnings WHERE account=? COLLATE NOCASE", (account,)
        ).fetchone()[0])
        rewards = db.execute(
            """SELECT occurred_at, reward FROM event_rewards
               WHERE account=? COLLATE NOCASE ORDER BY id DESC LIMIT 10""",
            (account,),
        ).fetchall()
    status_items = account_status_state()["accounts"]
    live = next(
        (item for item in status_items if str(item["name"]).casefold() == account.casefold()),
        {"name": account, "status": "not_scheduled", "position": 0},
    )
    return {
        "account": account,
        "live": live,
        "daily_operations": daily_count,
        "all_time_operations": all_count,
        "recent": [dict(row) for row in recent],
        "event_rewards": [dict(row) for row in rewards],
        "incidents": incident_entries([account], 20),
        "health": account_health(account),
        "timeline": timeline_entries([account], account, 30),
        "server_time": utc_now(),
    }


def account_health(account: str) -> dict:
    account = account.strip()
    score = 100
    reasons = []
    with database() as db:
        open_rows = db.execute(
            """SELECT severity, occurrence_count FROM incidents
               WHERE account=? COLLATE NOCASE AND status='open'""", (account,),
        ).fetchall()
        last_row = db.execute(
            "SELECT occurred_at FROM earnings WHERE account=? COLLATE NOCASE ORDER BY id DESC LIMIT 1",
            (account,),
        ).fetchone()
    for row in open_rows:
        count = int(row["occurrence_count"])
        score -= 30 if str(row["severity"]) == "critical" or count >= 3 else 15
    if open_rows:
        reasons.append(f"{len(open_rows)} açık hata")
    last_operation = str(last_row[0]) if last_row else ""
    if last_operation:
        try:
            parsed = datetime.fromisoformat(last_operation.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=3)))
            age_hours = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 3600
            if age_hours > 48:
                score -= 25
                reasons.append("48 saattir başarılı işlem yok")
            elif age_hours > 24:
                score -= 15
                reasons.append("24 saattir başarılı işlem yok")
        except ValueError:
            pass
    else:
        score -= 10
        reasons.append("Henüz işlem kaydı yok")
    state = next((item["status"] for item in account_status_state()["accounts"]
                  if str(item["name"]).casefold() == account.casefold()), "not_scheduled")
    if state == "skipped":
        score -= 10
        reasons.append("Bu tur atlandı")
    if bot_status_state()["health"] in {"offline", "error"}:
        score -= 25
        reasons.append("Bot bağlantısı sağlıksız")
    score = max(0, min(100, score))
    level = "healthy" if score >= 80 else "risk" if score >= 50 else "critical"
    return {"score": score, "level": level, "reasons": reasons or ["Sorun görünmüyor"],
            "last_operation": last_operation, "open_incidents": len(open_rows)}


def operations_state(allowed: list[str] | None = None) -> dict:
    names = account_catalog() if allowed is None else allowed
    health = [{"account": name, **account_health(name)} for name in names]
    health.sort(key=lambda item: (int(item["score"]), str(item["account"]).casefold()))
    incidents = incident_entries(allowed, 100)
    return {
        "health": health,
        "timeline": timeline_entries(allowed, limit=100),
        "alarms": {
            "open": sum(item["status"] == "open" for item in incidents),
            "critical": sum(item["status"] == "open" and item["alert_level"] == "critical" for item in incidents),
            "notify_now": sum(bool(item["should_notify"]) for item in incidents),
        },
        "alert_incidents": [item for item in incidents if item["should_notify"]][:20],
        "server_time": utc_now(),
    }


def save_skip_request(account: str, tour_id: str = "") -> dict:
    account = account.strip()
    if not account:
        raise ValueError("Hesap adı gerekli.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key='skip_requests'").fetchone()
        try:
            requests = json.loads(str(row[0]) if row else "[]")
        except json.JSONDecodeError:
            requests = []
        requests = [item for item in requests if isinstance(item, dict)]
        if not any(
            str(item.get("account", "")).casefold() == account.casefold()
            and str(item.get("tour_id", "")) == tour_id for item in requests
        ):
            requests.append({"account": account, "tour_id": tour_id, "created_at": utc_now()})
        set_setting("skip_requests", json.dumps(requests[-100:], ensure_ascii=True), db)
        db.commit()
    return {"queued": True, "account": account, "tour_id": tour_id}


def claim_skip_request(account: str, tour_id: str = "") -> dict:
    account = account.strip()
    matched = False
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM settings WHERE key='skip_requests'").fetchone()
        try:
            requests = json.loads(str(row[0]) if row else "[]")
        except json.JSONDecodeError:
            requests = []
        remaining = []
        for item in requests if isinstance(requests, list) else []:
            same_account = str(item.get("account", "")).casefold() == account.casefold()
            requested_tour = str(item.get("tour_id", ""))
            same_tour = not requested_tour or requested_tour == tour_id
            if not matched and same_account and same_tour:
                matched = True
            else:
                remaining.append(item)
        set_setting("skip_requests", json.dumps(remaining, ensure_ascii=True), db)
        db.commit()
    return {"skip": matched, "account": account, "tour_id": tour_id}


def save_earnings(entries: list[dict]) -> int:
    saved = 0
    with database() as db:
        for entry in entries[:1000]:
            external_id = str(entry.get("external_id") or "").strip()
            occurred_at = str(entry.get("occurred_at") or "").strip()
            account = str(entry.get("account") or "").strip()
            operation = str(entry.get("operation") or "").strip()
            result = str(entry.get("result") or "").strip()
            if not all((external_id, occurred_at, account, operation, result)):
                continue
            cursor = db.execute(
                """INSERT OR IGNORE INTO earnings
                   (external_id, occurred_at, account, operation, result, received_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    external_id[:200], occurred_at[:40], account[:200],
                    operation[:200], result[:2000], utc_now(),
                ),
            )
            inserted = cursor.rowcount > 0
            saved += int(inserted)
            if inserted:
                record_timeline(
                    f"earning:{external_id}", occurred_at, account, "success",
                    operation, result, "success", db,
                )
    return saved


def earnings_summary(today_only: bool) -> dict:
    where = ""
    params: tuple = ()
    if today_only:
        today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
        where = " WHERE substr(occurred_at, 1, 10)=?"
        params = (today,)
    with database() as db:
        total = int(db.execute("SELECT COUNT(*) FROM earnings" + where, params).fetchone()[0])
        accounts = int(db.execute(
            "SELECT COUNT(DISTINCT account) FROM earnings" + where, params
        ).fetchone()[0])
        operations = db.execute(
            "SELECT operation, COUNT(*) count FROM earnings" + where
            + " GROUP BY operation ORDER BY count DESC, operation LIMIT 12",
            params,
        ).fetchall()
    return {
        "total": total,
        "accounts": accounts,
        "operations": [
            {"operation": str(row["operation"]), "count": int(row["count"])}
            for row in operations
        ],
    }


MATERIAL_NAMES = {
    "xp": "XP", "kereste": "Kereste", "demir": "Demir", "celik": "Çelik",
    "plastik": "Plastik", "petrol": "Petrol", "tas": "Taş", "cam": "Cam",
    "anakart": "Anakart", "motor": "Motor", "enerji": "Enerji",
    "sanayi parcasi": "Sanayi Parçası", "mermi": "Mermi", "belge": "Belge",
    "uranyum": "Uranyum", "aurorium": "Aurorium", "photonium": "Photonium",
    "voidium": "Voidium", "carbon": "Carbon",
}


def material_key(value: str) -> str:
    value = str(value or "").replace("ı", "i").replace("İ", "I")
    value = "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    ).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[_-]+", " ", value)).strip()


def integer_value(value: str) -> int:
    return int(re.sub(r"\D", "", str(value or "")) or "0")


def earning_materials(operation: str, result: str) -> dict[str, int]:
    materials: dict[str, int] = defaultdict(int)
    for amount, raw_name in re.findall(
        r"([\d.,]+)\s+adet\s+([\wÇĞİÖŞÜçğıöşü_-]+)", result or "", re.IGNORECASE
    ):
        key = material_key(raw_name)
        if key == "urun":
            operation_key = material_key(operation)
            if operation_key == "eyalet fabrikasi":
                key = "enerji"
            elif operation_key == "eyalet sanayisi":
                key = "sanayi parcasi"
        materials[key] += integer_value(amount)
    for raw_name, amount in re.findall(
        r"Kazanılan\s+([^:!-]+?)\s*:\s*([\d.,]+)", result or "", re.IGNORECASE
    ):
        key = material_key(raw_name)
        if key not in {"bonus exp", "exp", "xp"}:
            materials[key] += integer_value(amount)
    for after, before in re.findall(
        r"(?:Bonus\s+)?(?:Exp|XP)\s*:\s*([\d.,]+)|([\d.,]+)\s*(?:Exp|XP)\b",
        result or "", re.IGNORECASE,
    ):
        materials["xp"] += integer_value(after or before)
    return {key: amount for key, amount in materials.items() if key and amount > 0}


def material_reports() -> dict:
    turkey_now = datetime.now(timezone(timedelta(hours=3)))
    today = turkey_now.date().isoformat()
    yesterday = (turkey_now.date() - timedelta(days=1)).isoformat()
    periods: dict[str, dict[str, int]] = {
        "today": defaultdict(int), "yesterday": defaultdict(int), "all_time": defaultdict(int)
    }
    with database() as db:
        rows = db.execute("SELECT occurred_at, operation, result FROM earnings").fetchall()
    for row in rows:
        date = str(row["occurred_at"])[:10]
        for key, amount in earning_materials(row["operation"], row["result"]).items():
            periods["all_time"][key] += amount
            if date == today:
                periods["today"][key] += amount
            elif date == yesterday:
                periods["yesterday"][key] += amount
    comparison = []
    keys = set(periods["today"]) | set(periods["yesterday"])
    for key in sorted(
        keys, key=lambda item: max(periods["today"][item], periods["yesterday"][item]),
        reverse=True,
    ):
        current, previous = periods["today"][key], periods["yesterday"][key]
        percent = None if previous == 0 else round(((current - previous) / previous) * 100, 1)
        comparison.append({
            "key": key, "name": MATERIAL_NAMES.get(key, key.title()),
            "today": current, "yesterday": previous, "difference": current - previous,
            "percent": percent,
        })
    all_time = [
        {"key": key, "name": MATERIAL_NAMES.get(key, key.title()), "amount": amount}
        for key, amount in sorted(periods["all_time"].items(), key=lambda item: item[1], reverse=True)
    ]
    return {"today": today, "yesterday": yesterday, "comparison": comparison, "all_time": all_time}


def earnings_dashboard() -> dict:
    with database() as db:
        recent = db.execute(
            "SELECT occurred_at, account, operation, result FROM earnings ORDER BY id DESC LIMIT 8"
        ).fetchall()
    return {
        "daily": earnings_summary(True),
        "all_time": earnings_summary(False),
        "materials": material_reports(),
        "recent": [dict(row) for row in recent],
    }


def save_mode(mode: str) -> dict:
    if mode not in {"off", "once", "always"}:
        raise ValueError("mode must be off, once or always")
    set_setting("mode", mode)
    return {"mode": mode, "active": mode != "off"}


def normalize_schedule_time(value: str) -> str:
    value = value.strip().replace(".", ":")
    if not value:
        raise ValueError("Boş saat yazılamaz.")
    parts = value.split(":", 1)
    if not parts[0].isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        raise ValueError(f"Geçersiz saat: {value}")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Geçersiz saat: {value}. Saat 00:00 ile 23:59 arasında olmalı.")
    return f"{hour:02d}:{minute:02d}"


def parse_schedule_command(command_text: str) -> tuple[bool, list[str]]:
    args = command_text.strip().split()[1:]
    if not args:
        raise ValueError("Kullanım: /saat 02 08 14 20, /saat her veya /saat kapat")
    normalized = " ".join(args).casefold()
    if normalized in {"kapat", "kapalı", "kapali", "off"}:
        return False, []
    if normalized in {"her", "hepsi", "saatlik", "ac", "aç", "on"}:
        return True, []
    values = [part for part in normalized.replace(",", " ").split() if part]
    times: set[str] = set()
    for value in values:
        times.add(normalize_schedule_time(value))
    if not times:
        raise ValueError("En az bir saat yazmalısın.")
    return True, sorted(times)


def save_schedule(enabled: bool, times: list[str | int]) -> dict:
    clean_times = sorted({normalize_schedule_time(str(value)) for value in times})
    with database() as db:
        set_setting("schedule_enabled", "1" if enabled else "0", db)
        set_setting("schedule_times", ",".join(clean_times), db)
        set_setting(
            "schedule_hours",
            ",".join(value[:2].lstrip("0") or "0" for value in clean_times if value.endswith(":00")),
            db,
        )
    return schedule_state()


def apply_command(message: dict) -> None:
    role = authorized_role(message)
    if not role:
        return
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    command_text = str(message.get("text") or "")
    command = normalize_command(command_text)
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
    elif command == "/saatler":
        response = schedule_text()
    elif command == "/saat":
        if role != "admin":
            response = "⛔ Otomatik tur saatlerini yalnız yönetici değiştirebilir."
        else:
            try:
                enabled, times = parse_schedule_command(command_text)
                save_schedule(enabled, times)
                response = "✅ Saat planı kaydedildi.\n\n" + schedule_text()
            except ValueError as error:
                response = f"❌ {error}"
    elif command == "/etkinlik":
        response = event_report(command_days(command_text))
    elif command == "/etkinlikhesap":
        response = event_report(command_days(command_text), by_account=True)
    elif command in {"/etkinlikac", "/etkinlikaç"}:
        save_event_control(True)
        response = "🎁 <b>Etkinlik modülü açıldı.</b>\n\nSonraki turda etkinlik kutuları kontrol edilecek."
    elif command == "/etkinlikkapat":
        save_event_control(False)
        response = "⏸ <b>Etkinlik modülü kapatıldı.</b>\n\nKod ve geçmiş kayıtlar korunuyor."
    elif command == "/etkinlikdurum":
        durum = "AÇIK" if event_control_state()["enabled"] else "KAPALI"
        response = f"🎁 <b>Etkinlik modülü:</b> {durum}"
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
    send_message(
        chat_id,
        response,
        parse_mode="" if command in {"/etkinlik", "/etkinlikhesap"} else "HTML",
    )


def save_events(events: list[dict]) -> int:
    saved = 0
    with database() as db:
        for event in events[:1000]:
            external_id = str(event.get("external_id") or "").strip()
            occurred_at = str(event.get("occurred_at") or "").strip()
            account = str(event.get("account") or "").strip()
            reward = str(event.get("reward") or "").strip()
            if not all((external_id, occurred_at, account, reward)):
                continue
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO event_rewards
                    (external_id, occurred_at, account, reward, received_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (external_id[:200], occurred_at[:40], account[:200], reward[:1000], utc_now()),
            )
            saved += int(cursor.rowcount > 0)
    return saved


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

    def authorized_identity(self) -> dict | None:
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = self.headers.get("X-Control-Secret", "")
        return session_identity(supplied) if supplied else None

    def authorized_role(self) -> str | None:
        identity = self.authorized_identity()
        return str(identity["role"]) if identity else None

    def authorized_request(self, admin: bool = False) -> bool:
        role = self.authorized_role()
        return role == "admin" if admin else role in {"admin", "viewer"}

    def audit_action(self, action: str, target: str = "", details: dict | None = None) -> None:
        identity = self.authorized_identity() or {"username": "unknown", "role": "unknown"}
        record_audit(
            str(identity.get("username") or "unknown"), str(identity.get("role") or "unknown"),
            action, target, details,
        )

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/health":
            self.send_json(200, {"ok": True, "service": "vssbot", "time": utc_now()})
            return
        if path == "/api/status":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            mode = get_setting("mode", "off")
            self.send_json(200, {"ok": True, "mode": mode, "active": mode != "off"})
            return
        if path == "/api/dashboard":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            role = self.authorized_role()
            if role == "admin":
                self.send_json(200, {"ok": True, "role": role, **dashboard_state()})
            else:
                identity = self.authorized_identity() or {}
                username = str(identity.get("username") or "")
                accounts = account_status_state(assigned_accounts(username))
                self.send_json(200, {
                    "ok": True, "role": role, "username": username, "bot": accounts["bot"],
                    "accounts": accounts["accounts"], "tour_id": accounts["tour_id"],
                    "phase": accounts["phase"], "server_time": accounts["server_time"],
                })
            return
        if path == "/api/account-status":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            identity = self.authorized_identity() or {}
            role = str(identity.get("role") or "")
            username = str(identity.get("username") or "")
            state = account_status_state(None if role == "admin" else assigned_accounts(username))
            state["role"] = role
            state["username"] = username
            self.send_json(200, {"ok": True, **state})
            return
        if path == "/api/users":
            if not self.authorized_request(admin=True):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, "users": list_app_users()})
            return
        if path == "/api/account-catalog":
            if not self.authorized_request(admin=True):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, "accounts": account_catalog()})
            return
        if path == "/api/account-detail":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            account = str((query.get("name") or [""])[0]).strip()
            identity = self.authorized_identity() or {}
            if identity.get("role") != "admin" and account.casefold() not in {
                value.casefold() for value in assigned_accounts(str(identity.get("username") or ""))
            }:
                self.send_json(403, {"ok": False, "error": "Bu hesap kullanıcıya atanmamış."})
                return
            try:
                self.send_json(200, {"ok": True, **account_detail(account)})
            except ValueError as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if path == "/api/operations":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            identity = self.authorized_identity() or {}
            allowed = None if identity.get("role") == "admin" else assigned_accounts(str(identity.get("username") or ""))
            self.send_json(200, {"ok": True, **operations_state(allowed)})
            return
        if path == "/api/incidents":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            identity = self.authorized_identity() or {}
            allowed = None if identity.get("role") == "admin" else assigned_accounts(str(identity.get("username") or ""))
            self.send_json(200, {"ok": True, "incidents": incident_entries(allowed)})
            return
        if path == "/api/audit":
            if not self.authorized_request(admin=True):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, "entries": audit_entries()})
            return
        if path == "/api/event-control":
            if not self.authorized_request(admin=True):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, **event_control_state()})
            return
        if path == "/api/bot-control":
            if not self.authorized_request(admin=True):
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, **bot_status_state()})
            return
        if path == "/api/schedule":
            if not self.authorized_request():
                self.send_json(401, {"ok": False, "error": "unauthorized"})
                return
            self.send_json(200, {"ok": True, **schedule_state()})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/api/session":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 10000)
                payload = json.loads(self.rfile.read(length) or b"{}")
                result = create_session(
                    str(payload.get("username") or ""), str(payload.get("password") or "")
                )
                record_audit(result["username"], result["role"], "login", "android")
                self.send_json(200, {"ok": True, **result})
            except PermissionError as error:
                self.send_json(401, {"ok": False, "error": str(error)})
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self.send_json(400, {"ok": False, "error": str(error)})
            return
        if self.path not in {
            "/api/claim", "/api/events", "/api/schedule", "/api/mode",
            "/api/event-control",
            "/api/bot-control", "/api/agent-status", "/api/earnings",
            "/api/users", "/api/users/delete", "/api/account-skip", "/api/account-skip/claim",
            "/api/incidents/resolve",
        }:
            self.send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self.authorized_request(admin=True):
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path == "/api/claim":
            self.send_json(200, {"ok": True, **claim_decision()})
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/users":
                accounts = payload.get("accounts") or []
                result = save_app_user(
                    str(payload.get("username") or ""),
                    str(payload.get("password") or ""), accounts,
                )
                self.audit_action("user_saved", result["username"], {"accounts": result["accounts"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/users/delete":
                result = delete_app_user(
                    str(payload.get("username") or "")
                )
                self.audit_action("user_deleted", result["username"], {"deleted": result["deleted"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/incidents/resolve":
                result = resolve_incident(int(payload.get("id") or 0), str((self.authorized_identity() or {}).get("username") or "admin"))
                self.audit_action("incident_resolved", result["account"], {"incident_id": result["id"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/account-skip":
                result = save_skip_request(
                    str(payload.get("account") or ""), str(payload.get("tour_id") or "")
                )
                self.audit_action("account_skip_queued", result["account"], {"tour_id": result["tour_id"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/account-skip/claim":
                self.send_json(200, {"ok": True, **claim_skip_request(
                    str(payload.get("account") or ""), str(payload.get("tour_id") or "")
                )})
                return
            if self.path == "/api/mode":
                result = save_mode(str(payload.get("mode") or ""))
                self.audit_action("mode_changed", "bot", {"mode": result["mode"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/event-control":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled must be boolean")
                result = save_event_control(payload["enabled"])
                self.audit_action("event_control_changed", "event", {"enabled": result["enabled"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/bot-control":
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled must be boolean")
                result = save_bot_control(payload["enabled"])
                self.audit_action("bot_control_changed", "bot", {"enabled": result["enabled"]})
                self.send_json(200, {"ok": True, **result})
                return
            if self.path == "/api/agent-status":
                if not isinstance(payload, dict):
                    raise ValueError("status must be an object")
                self.send_json(200, {"ok": True, **save_agent_status(payload)})
                return
            if self.path == "/api/earnings":
                entries = payload.get("entries") or []
                if not isinstance(entries, list):
                    raise ValueError("entries must be a list")
                saved = save_earnings(entries)
                self.send_json(200, {"ok": True, "received": len(entries), "saved": saved})
                return
            if self.path == "/api/schedule":
                enabled = bool(payload.get("enabled", True))
                if "times" in payload:
                    times = payload.get("times")
                    if not isinstance(times, list) or any(not isinstance(value, str) for value in times):
                        raise ValueError("times must be a list of HH:MM strings")
                else:
                    hours = payload.get("hours", [])
                    if not isinstance(hours, list) or any(
                        isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23
                        for hour in hours
                    ):
                        raise ValueError("hours must be a list of integers from 0 to 23")
                    times = [f"{hour:02d}:00" for hour in hours]
                result = save_schedule(enabled, times)
                self.audit_action("schedule_changed", "schedule", {"enabled": enabled, "times": result["times"]})
                self.send_json(200, {"ok": True, **result})
                return
            events = payload.get("events") or []
            if not isinstance(events, list):
                raise ValueError("events must be a list")
            saved = save_events(events)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.send_json(400, {"ok": False, "error": str(error)})
            return
        self.send_json(200, {"ok": True, "received": len(events), "saved": saved})


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
