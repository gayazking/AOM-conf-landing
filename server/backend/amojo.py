"""amoJo (amoCRM Chats API) client — module used by app.py's route layer.

app.py owns the Flask routes (/amojo/outbound, /amo/chat-webhook) and the
loopback/auth/echo-filter logic. This module provides ONLY the amoJo protocol:
HMAC-SHA1 signing, chat create, contact link, message send, inbound signature
verification, delivery status, and a tiny JSON state cache.

Signing: HMAC-SHA1 over METHOD\\nContent-MD5\\nContent-Type\\nDate\\npath (hex),
key = channel secret. Same scheme amoJo uses to sign hooks it sends to us.
"""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

import requests

AMOJO_BASE = "https://amojo.amocrm.ru"
STATE_FILE = "/var/lib/sato/amojo_state.json"
HTTP_TIMEOUT = 20


def amojo_enabled(cfg):
    """Active only when the channel secret + a scope_id are available."""
    scope = cfg.get("AMOJO_SCOPE_ID") or (read_state() or {}).get("scope_id")
    return bool(cfg.get("AMOJO_CHANNEL_SECRET") and scope)


# --------------------------------------------------------------------------- #
# State (scope_id + per-conversation chat/contact cache)
# --------------------------------------------------------------------------- #
def read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def write_state(st):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        # State is a cache; never let a write failure break message flow.
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def _sign(secret, method, path, body_bytes, ctype="application/json"):
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    cmd5 = hashlib.md5(body_bytes).hexdigest().lower()
    sts = "\n".join([method.upper(), cmd5, ctype, date, path])
    sig = hmac.new(secret.encode(), sts.encode(), hashlib.sha1).hexdigest().lower()
    return {
        "Date": date,
        "Content-Type": ctype,
        "Content-MD5": cmd5,
        "X-Signature": sig,
    }


# --------------------------------------------------------------------------- #
# amoJo API
# --------------------------------------------------------------------------- #
def ensure_chat(cfg, scope_id, conversation_id, user_dict, logger=None):
    """Create the chat (idempotent via state cache). Returns chat id or None."""
    st = read_state()
    cached = (st.get("chats") or {}).get(conversation_id) or {}
    if cached.get("chat_id"):
        return cached["chat_id"]

    secret = cfg.get("AMOJO_CHANNEL_SECRET", "")
    path = "/v2/origin/custom/%s/chats" % scope_id
    body = json.dumps(
        {
            "conversation_id": conversation_id,
            "source": {"external_id": conversation_id},
            "user": user_dict,
        },
        ensure_ascii=False,
    ).encode()
    try:
        h = _sign(secret, "POST", path, body)
        r = requests.post(AMOJO_BASE + path, data=body, headers=h, timeout=HTTP_TIMEOUT)
        if r.status_code in (200, 201):
            chat_id = (r.json() or {}).get("id")
            if chat_id:
                ch = st.setdefault("chats", {}).setdefault(conversation_id, {})
                ch["chat_id"] = chat_id
                write_state(st)
            return chat_id
        if logger:
            logger.warning("amojo ensure_chat %s %s", r.status_code, r.text[:300])
    except Exception:
        if logger:
            logger.exception("amojo ensure_chat error")
    return None


def link_chat_to_contact(cfg, amo_request, contact_id, chat_id, logger=None):
    """Attach the amoJo chat to a CRM contact so it shows in the deal."""
    try:
        r = amo_request(
            cfg, "POST", "/api/v4/contacts/chats",
            [{"contact_id": int(contact_id), "chat_id": chat_id}],
        )
        if r.status_code in (200, 201):
            return True
        if logger:
            logger.warning("amojo link_chat_to_contact %s %s", r.status_code, r.text[:200])
    except Exception:
        if logger:
            logger.exception("amojo link_chat_to_contact error")
    return False


def send_message(cfg, scope_id, conversation_id, sender, text, msgid, logger=None):
    """Import a message (sender = client) into the amoCRM chat. Returns (ok, resp)."""
    secret = cfg.get("AMOJO_CHANNEL_SECRET", "")
    path = "/v2/origin/custom/%s" % scope_id
    now = int(time.time())
    payload = {
        "event_type": "new_message",
        "payload": {
            "timestamp": now,
            "msec_timestamp": now * 1000,
            "msgid": msgid,
            "conversation_id": conversation_id,
            "sender": sender,
            "message": {"type": "text", "text": text},
        },
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    try:
        h = _sign(secret, "POST", path, body)
        r = requests.post(AMOJO_BASE + path, data=body, headers=h, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300 and logger:
            logger.warning("amojo send_message %s %s", r.status_code, r.text[:300])
        return (r.status_code < 300, r)
    except Exception:
        if logger:
            logger.exception("amojo send_message error")
        return (False, None)


def verify_inbound(raw, sig, secret):
    """Verify amoJo inbound webhook X-Signature.

    amoJo signs the JSON body with HMAC-SHA1(secret) — but the body it signs has
    NO trailing newline, while the bytes we receive may have a trailing '\\n'
    appended. Accept the raw body or the newline-stripped body.
    """
    import hashlib as _h
    import hmac as _hm
    if not secret or not sig:
        return False
    sig = sig.strip().lower()
    s = secret.encode()
    body = raw or b""
    for msg in (body, body.rstrip(b"\n"), body.rstrip(b"\r\n")):
        calc = _hm.new(s, msg, _h.sha1).hexdigest().lower()
        if _hm.compare_digest(calc, sig):
            return True
    return False


def report_delivery(cfg, scope_id, msgid, delivered, logger=None):
    """Best-effort delivery receipt back to amoJo (never fatal)."""
    secret = cfg.get("AMOJO_CHANNEL_SECRET", "")
    path = "/v2/origin/custom/%s/%s/delivery_status" % (scope_id, msgid)
    body = json.dumps(
        {"msgid": msgid, "delivery_status": 1 if delivered else -1},
        ensure_ascii=False,
    ).encode()
    try:
        h = _sign(secret, "POST", path, body)
        requests.post(AMOJO_BASE + path, data=body, headers=h, timeout=HTTP_TIMEOUT)
    except Exception:
        if logger:
            logger.debug("amojo report_delivery failed (non-fatal)")
