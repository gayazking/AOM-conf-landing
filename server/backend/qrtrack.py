"""QR scan tracking — poster QR codes redirect through /r/<code>.

Each poster variant (1..5) has 3 codes (tg/site/max). The QR encodes
https://sadaosato.pro/r/<code>; this endpoint logs the scan (ip, ua, referer,
lang, device, variant, channel, ts) into qr_scans, then 302-redirects to the
real destination. Analytics live in registrations.sqlite3 (Datasette/GUI).
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, redirect

import reg

log = logging.getLogger("sato")
bp = Blueprint("qrtrack", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS qr_codes(
  code TEXT PRIMARY KEY, variant TEXT, channel TEXT, dest_url TEXT, label TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS qr_scans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, variant TEXT, channel TEXT, ts TEXT,
  ip TEXT, ua TEXT, referer TEXT, lang TEXT, device TEXT
);
CREATE INDEX IF NOT EXISTS ix_scan_code ON qr_scans(code);
CREATE INDEX IF NOT EXISTS ix_scan_ts ON qr_scans(ts);
"""

SITE = "https://sadaosato.pro/"
TG = "https://t.me/sadaosato"
MAX = "https://max.ru/join/LVco0qmc-vwqhbquj4nl7-746A1ieXudlmyr5QGbUf8"


def _now():
    return datetime.now(timezone.utc).isoformat()


def init():
    c = reg._conn()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def seed():
    """Create 5 variants x 3 channels = 15 codes. Idempotent."""
    init()
    c = reg._conn()
    try:
        for v in range(1, 6):
            site_dest = (SITE + "?utm_source=poster&utm_medium=qr&utm_campaign=afisha-v%d&utm_content=site" % v)
            rows = [
                ("p%dt" % v, str(v), "tg", TG, "Telegram-канал @sadaosato"),
                ("p%ds" % v, str(v), "site", site_dest, "Сайт sadaosato.pro"),
                ("p%dm" % v, str(v), "max", MAX, "MAX-канал"),
            ]
            for code, variant, channel, dest, label in rows:
                c.execute(
                    "INSERT INTO qr_codes (code,variant,channel,dest_url,label,created_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET dest_url=excluded.dest_url, label=excluded.label",
                    (code, variant, channel, dest, label, _now()),
                )
        c.commit()
    finally:
        c.close()


def _device(ua):
    u = (ua or "").lower()
    if any(b in u for b in ("bot", "crawler", "spider", "preview", "facebookexternalhit", "telegrambot", "whatsapp")):
        return "bot"
    if any(m in u for m in ("iphone", "android", "ipad", "mobile")):
        return "mobile"
    return "desktop"


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


@bp.route("/r/<code>", methods=["GET"])
def track(code):
    code = (code or "").strip()[:32]
    dest = SITE
    variant = channel = None
    try:
        c = reg._conn()
        row = c.execute("SELECT variant,channel,dest_url FROM qr_codes WHERE code=?", (code,)).fetchone()
        if row:
            variant, channel, dest = row["variant"], row["channel"], row["dest_url"]
        ua = request.headers.get("User-Agent", "")
        c.execute(
            "INSERT INTO qr_scans (code,variant,channel,ts,ip,ua,referer,lang,device) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, variant, channel, _now(), _client_ip(), ua[:400],
             request.headers.get("Referer", "")[:300],
             request.headers.get("Accept-Language", "")[:80], _device(ua)),
        )
        c.commit()
        c.close()
    except Exception:
        log.exception("qr track failed for %s", code)
    return redirect(dest, code=302)


def register(flask_app):
    flask_app.register_blueprint(bp)
    log.info("qrtrack blueprint registered")
