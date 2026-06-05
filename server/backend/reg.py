"""Local operational registrations store (SQLite, shared by web backend + bot).

amoCRM = sales funnel. THIS db = operational source of truth for
registration / payment / ticket / check-in + an append-only consent audit
trail (152-FZ / GDPR Art.7). Lives at REG_DB, WAL, group-writable by `sato`.

Defensive: every public call wraps its own short-lived connection and never
raises into the request path on a benign error (caller wraps in try/except).
"""
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

REG_DB = os.environ.get("REG_DB", "/var/lib/sato/registrations.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  full_name TEXT, phone_e164 TEXT, email_lc TEXT,
  city TEXT, clinic TEXT, specialty TEXT, badge_name TEXT,
  source TEXT, telegram_user_id INTEGER, telegram_username TEXT,
  utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
  format TEXT, channel TEXT, message TEXT,
  package TEXT, price_eur INTEGER, currency TEXT DEFAULT 'EUR',
  amocrm_lead_id INTEGER, amocrm_contact_id INTEGER,
  payment_provider TEXT, payment_id TEXT UNIQUE,
  payment_link TEXT, amount_paid INTEGER, paid_at TEXT,
  ticket_id TEXT UNIQUE, ticket_issued_at TEXT, qr_delivered_channels TEXT,
  status TEXT NOT NULL DEFAULT 'lead',
  checked_in_at TEXT, checked_in_by TEXT, check_in_gate TEXT,
  reminders_sent TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_reg_phone ON registrations(phone_e164) WHERE phone_e164 IS NOT NULL AND phone_e164<>'';
CREATE INDEX IF NOT EXISTS ix_reg_email ON registrations(email_lc);
CREATE INDEX IF NOT EXISTS ix_reg_status ON registrations(status);
CREATE INDEX IF NOT EXISTS ix_reg_tg ON registrations(telegram_user_id);

CREATE TABLE IF NOT EXISTS status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registration_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
  ts TEXT NOT NULL, actor TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS consent_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registration_id TEXT, subject_phone TEXT, subject_email TEXT, subject_tg INTEGER,
  doc_type TEXT NOT NULL, doc_version TEXT, action TEXT NOT NULL,
  ts TEXT NOT NULL, ip TEXT, user_agent TEXT, channel TEXT
);
CREATE TABLE IF NOT EXISTS scan_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT, ts TEXT, result TEXT, gate TEXT, staff TEXT
);
"""

_PRICE = {"500": 500, "800": 800, "1000": 1000, "1800": 1800}
_ALLOWED = {
    "lead": {"registered", "paid", "cancelled"},
    "registered": {"paid", "cancelled"},
    "paid": {"ticket_issued", "refunded", "cancelled"},
    "ticket_issued": {"checked_in", "no_show", "refunded", "cancelled"},
    "checked_in": set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    os.makedirs(os.path.dirname(REG_DB), exist_ok=True)
    c = sqlite3.connect(REG_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init():
    c = _conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def normalize_phone(p):
    if not p:
        return ""
    d = re.sub(r"\D", "", str(p))
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return ("+" + d) if d else ""


def price_from_format(fmt):
    s = str(fmt or "")
    for tok in ("1800", "1000", "800", "500"):
        if tok in s:
            return int(tok)
    return None


def upsert_registration(rec):
    """Upsert by phone (then email). rec: dict of fields. Returns reg id."""
    phone = normalize_phone(rec.get("phone"))
    email = (rec.get("email") or "").strip().lower()
    price = price_from_format(rec.get("format")) or price_from_format(rec.get("package"))
    fields = {
        "full_name": (rec.get("name") or rec.get("full_name") or "").strip() or None,
        "phone_e164": phone or None,
        "email_lc": email or None,
        "city": rec.get("city") or None,
        "clinic": rec.get("clinic") or None,
        "specialty": rec.get("specialty") or None,
        "source": rec.get("source") or "web",
        "telegram_user_id": rec.get("telegram_user_id"),
        "telegram_username": rec.get("telegram_username"),
        "utm_source": rec.get("utm_source"), "utm_medium": rec.get("utm_medium"),
        "utm_campaign": rec.get("utm_campaign"), "utm_content": rec.get("utm_content"),
        "utm_term": rec.get("utm_term"),
        "format": rec.get("format"), "channel": rec.get("channel"), "message": rec.get("message"),
        "package": rec.get("package") or (str(price) if price else None),
        "price_eur": price,
        "amocrm_lead_id": rec.get("amocrm_lead_id"),
        "amocrm_contact_id": rec.get("amocrm_contact_id"),
    }
    c = _conn()
    try:
        row = None
        if phone:
            row = c.execute("SELECT * FROM registrations WHERE phone_e164=?", (phone,)).fetchone()
        if row is None and email:
            row = c.execute("SELECT * FROM registrations WHERE email_lc=?", (email,)).fetchone()
        if row:
            rid = row["id"]
            sets, vals = [], []
            for k, v in fields.items():
                if v not in (None, ""):
                    sets.append("%s=?" % k)
                    vals.append(v)
            sets.append("updated_at=?")
            vals.append(_now())
            vals.append(rid)
            c.execute("UPDATE registrations SET %s WHERE id=?" % ",".join(sets), vals)
        else:
            rid = uuid.uuid4().hex
            cols = ["id", "created_at", "updated_at", "status"] + list(fields.keys())
            status = "registered" if fields["package"] else "lead"
            vals = [rid, _now(), _now(), status] + [fields[k] for k in fields]
            c.execute(
                "INSERT INTO registrations (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                vals,
            )
            c.execute(
                "INSERT INTO status_history (registration_id, from_status, to_status, ts, actor, reason) VALUES (?,?,?,?,?,?)",
                (rid, None, status, _now(), fields["source"], "created"),
            )
        c.commit()
        return rid
    finally:
        c.close()


def log_consent(registration_id, doc_type, doc_version, action="granted",
                subject_phone=None, subject_email=None, subject_tg=None,
                ip=None, user_agent=None, channel="web"):
    c = _conn()
    try:
        c.execute(
            "INSERT INTO consent_log (registration_id, subject_phone, subject_email, subject_tg,"
            " doc_type, doc_version, action, ts, ip, user_agent, channel)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (registration_id, normalize_phone(subject_phone) or None,
             (subject_email or "").strip().lower() or None, subject_tg,
             doc_type, doc_version, action, _now(), ip, user_agent, channel),
        )
        c.commit()
    finally:
        c.close()


def set_status(registration_id, new_status, actor="system", reason=None, force=False):
    c = _conn()
    try:
        row = c.execute("SELECT status FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if not row:
            return False
        cur = row["status"]
        if not force and new_status not in _ALLOWED.get(cur, set()):
            return False
        c.execute("UPDATE registrations SET status=?, updated_at=? WHERE id=?",
                  (new_status, _now(), registration_id))
        c.execute(
            "INSERT INTO status_history (registration_id, from_status, to_status, ts, actor, reason) VALUES (?,?,?,?,?,?)",
            (registration_id, cur, new_status, _now(), actor, reason),
        )
        c.commit()
        return True
    finally:
        c.close()


if __name__ == "__main__":
    init()
    print("registrations db initialized at", REG_DB)
