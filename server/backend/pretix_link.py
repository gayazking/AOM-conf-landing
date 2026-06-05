"""pretix integration — push a PAID registration into self-hosted pretix as a
paid order, so registrars check attendees in with pretixSCAN (or the web
/checkin backup). All calls are loopback HTTP to the pretix container; the
Host header carries the public vhost so pretix ALLOWED_HOSTS accepts them.

Env (in /etc/sato/amo.env): PRETIX_TOKEN, PRETIX_ORG, PRETIX_EVENT,
PRETIX_HOST, PRETIX_HTTP_HOST, PRETIX_HTTP_PORT, PRETIX_CHECKINLIST.
"""
import http.client
import json
import logging
from datetime import datetime, timezone

import reg

log = logging.getLogger("sato")

_ITEM_CACHE = {}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _cfg():
    import app
    return app.load_env()


def _conf():
    c = _cfg()
    return {
        "host_tcp": c.get("PRETIX_HTTP_HOST", "127.0.0.1"),
        "port": int(c.get("PRETIX_HTTP_PORT", "8345")),
        "vhost": c.get("PRETIX_HOST", "sadaosato.pro"),
        "org": c.get("PRETIX_ORG", "sato"),
        "event": c.get("PRETIX_EVENT", "ktk2026"),
        "token": c.get("PRETIX_TOKEN", ""),
        "checkinlist": str(c.get("PRETIX_CHECKINLIST", "1")),
    }


def _req(method, path, payload=None, conf=None):
    conf = conf or _conf()
    conn = http.client.HTTPConnection(conf["host_tcp"], conf["port"], timeout=25)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": "Token " + conf["token"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Host": conf["vhost"],
        "X-Forwarded-Proto": "https",
    }
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        status = resp.status
    except Exception as exc:
        log.error("pretix req error %s %s: %s", method, path, exc)
        return 0, {"detail": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        j = json.loads(data) if data else {}
    except Exception:
        j = {"detail": data[:400]}
    return status, j


def _evbase(conf):
    return "/api/v1/organizers/%s/events/%s" % (conf["org"], conf["event"])


def _item_map(conf):
    if _ITEM_CACHE.get(conf["event"]):
        return _ITEM_CACHE[conf["event"]]
    st, j = _req("GET", _evbase(conf) + "/items/?page_size=100", conf=conf)
    m = {}
    if st == 200:
        for it in j.get("results", []):
            key = (it.get("internal_name") or "").strip()
            if key:
                m[key] = it["id"]
    if m:
        _ITEM_CACHE[conf["event"]] = m
    return m


def available():
    conf = _conf()
    return bool(conf["token"]) and bool(_item_map(conf))


def _email_for(row):
    e = (row.get("email_lc") or "").strip()
    if e and "@" in e:
        return e
    tg = row.get("telegram_user_id")
    if tg:
        return "tg%s@noemail.sadaosato.pro" % tg
    return "reg%s@noemail.sadaosato.pro" % row.get("id")


def init():
    """Add pretix bookkeeping columns to registrations (idempotent)."""
    c = reg._conn()
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(registrations)").fetchall()}
        adds = [("pretix_order_code", "TEXT"), ("pretix_secret", "TEXT"),
                ("pretix_position_id", "INTEGER")]
        for col, typ in adds:
            if col not in cols:
                try:
                    c.execute("ALTER TABLE registrations ADD COLUMN %s %s" % (col, typ))
                    c.commit()
                except Exception:
                    # concurrent worker may have added it first; ignore duplicate
                    c.rollback()
    finally:
        c.close()


def create_paid_order(row):
    """Idempotent. Returns dict(code, secret, position_id, item, price, status)
    or None. Reuses pretix order if row already has pretix_order_code."""
    conf = _conf()
    if not conf["token"]:
        return None
    base = _evbase(conf)
    existing = (row.get("pretix_order_code") or "").strip()
    if existing:
        st, j = _req("GET", "%s/orders/%s/" % (base, existing), conf=conf)
        if st == 200 and j.get("positions"):
            p = j["positions"][0]
            return {"code": j["code"], "secret": p["secret"], "position_id": p["id"],
                    "item": p["item"], "price": p["price"], "status": j.get("status")}
    pkg = str(row.get("package") or "").strip()
    imap = _item_map(conf)
    item_id = imap.get(pkg)
    if not item_id:
        log.error("pretix: no item for package %r (map=%s)", pkg, imap)
        return None
    try:
        price = "%.2f" % float(row.get("price_eur") or pkg or 0)
    except Exception:
        price = "%.2f" % float(pkg or 0)
    payload = {
        "email": _email_for(row),
        "locale": "ru",
        "sales_channel": "web",
        "send_email": False,
        "payment_provider": "manual",
        "positions": [{
            "item": item_id,
            "price": price,
            "attendee_name_parts": {"_scheme": "full", "full_name": row.get("full_name") or "Участник"},
        }],
    }
    st, j = _req("POST", base + "/orders/", payload, conf=conf)
    if st not in (200, 201):
        log.error("pretix order create failed %s: %s", st, j)
        return None
    code = j["code"]
    st2, j2 = _req("POST", "%s/orders/%s/mark_paid/" % (base, code), {"send_email": False}, conf=conf)
    pos = (j.get("positions") or [{}])[0]
    secret = pos.get("secret")
    position_id = pos.get("id")
    try:
        c = reg._conn()
        c.execute("UPDATE registrations SET pretix_order_code=?, pretix_secret=?, pretix_position_id=?, updated_at=? WHERE id=?",
                  (code, secret, position_id, _now(), row.get("id")))
        c.commit()
        c.close()
    except Exception as exc:
        log.error("pretix persist failed: %s", exc)
    status = j2.get("status") if st2 in (200, 201) else j.get("status")
    return {"code": code, "secret": secret, "position_id": position_id,
            "item": item_id, "price": price, "status": status}


def redeem(secret):
    """Check in by QR secret on the configured list. Returns (result, info):
    result in ok|already|unpaid|invalid|error."""
    secret = (secret or "").strip()
    if not secret:
        return "invalid", {}
    conf = _conf()
    path = "%s/checkinlists/%s/positions/%s/redeem/" % (_evbase(conf), conf["checkinlist"], secret)
    st, j = _req("POST", path, {}, conf=conf)
    if st == 404:
        return "invalid", {"raw": j}
    status = j.get("status")
    reason = j.get("reason")
    pos = j.get("position") or {}
    info = {"name": pos.get("attendee_name"), "item": pos.get("item"),
            "order": pos.get("order"), "reason": reason, "raw": j}
    if status == "ok":
        return "ok", info
    if reason == "already_redeemed":
        return "already", info
    if reason == "unpaid":
        return "unpaid", info
    if reason in ("invalid", "blocked", "invalid_time", "product", "rules", "ambiguous", "revoked", "canceled"):
        return "invalid", info
    return "error", info
