#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sato conference landing — lead-capture backend.

Design goal: NEVER lose a lead, even if amoCRM is down, misconfigured, or
returns errors. Every lead is written to disk (append-only JSONL) BEFORE any
network call. amoCRM delivery is best-effort; failures are queued to
pending.jsonl for later backfill, and the client always gets ok:true.

Endpoints:
  POST /api/lead         -> capture lead, push to amoCRM (best effort)
  POST /api/event        -> lightweight analytics event capture
  GET  /amo/callback     -> OAuth2 authorization-code exchange
  POST /amo/chat-webhook -> amoJo inbound (manager replies) -> bot loopback
  POST /amojo/outbound   -> LOOPBACK: bot pushes client messages into amoJo
  GET  /api/health       -> health check

Run behind nginx via gunicorn:
  gunicorn -w 2 -b 127.0.0.1:8081 app:app
"""

import json
import os
import threading
import time
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, Response, make_response

import amojo

# --------------------------------------------------------------------------- #
# Paths / configuration
# --------------------------------------------------------------------------- #

DATA_DIR = os.environ.get("SATO_DATA_DIR", "/var/lib/sato")
ENV_FILE = os.environ.get("SATO_ENV_FILE", "/etc/sato/amo.env")

LEADS_FILE = os.path.join(DATA_DIR, "leads.jsonl")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
PENDING_FILE = os.path.join(DATA_DIR, "pending.jsonl")
TOKENS_FILE = os.path.join(DATA_DIR, "amo_tokens.json")
LEAD_MAP_FILE = os.path.join(DATA_DIR, "lead_map.jsonl")
LOG_FILE = os.path.join(DATA_DIR, "api.log")

# CORS: explicit allow-list. The landing page lives on sadaosato.pro.
ALLOWED_ORIGINS = {
    "https://sadaosato.pro",
    "https://www.sadaosato.pro",
}

# Price (integer EUR) derived from the "format" field of the form.
FORMAT_PRICE_EUR = {
    "500": 500,
    "800": 800,
    "1000": 1000,
    "1800": 1800,
}

HTTP_TIMEOUT = 12  # seconds for any amoCRM call
TOKEN_SKEW = 600   # refresh proactively when within 600s of expiry

# Loopback hosts allowed to call the bot-facing /amojo/* endpoints.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# --------------------------------------------------------------------------- #
# Bootstrapping: ensure data dir exists, configure logging
# --------------------------------------------------------------------------- #

os.makedirs(DATA_DIR, exist_ok=True)

logger = logging.getLogger("sato")
logger.setLevel(logging.INFO)
if not logger.handlers:
    try:
        _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    except Exception:
        # Fall back to stderr if the log file is not writable; we must never
        # let logging setup take down the process.
        _fh = logging.StreamHandler()
    _fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(_fh)
    # Also echo to stderr so journalctl captures it.
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_sh)

app = Flask(__name__)

# Serialize token refreshes: amoCRM refresh_token is single-use, so two
# concurrent refreshes would invalidate each other. gunicorn -w 2 uses
# separate processes, so we also guard with an OS file lock below.
_token_lock = threading.Lock()
# Serialize appends within a process (file appends are atomic on POSIX for
# small writes, but we keep a lock to be safe under threaded workers).
_file_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Helpers: config loading
# --------------------------------------------------------------------------- #

def load_env():
    """Parse /etc/sato/amo.env (KEY=VALUE per line). Tolerant of missing file
    and blank values. Returns a dict (possibly with empty strings)."""
    cfg = {
        "SUBDOMAIN": "gayazking",
        "BASE_URL": "https://gayazking.amocrm.ru",
        "CLIENT_ID": "",
        "CLIENT_SECRET": "",
        "REDIRECT_URI": "https://sadaosato.pro/amo/callback",
        "PIPELINE_ID": "",
        "STATUS_ID": "",
        # amoJo two-way chat (server-side only; secret never logged).
        "AMOJO_CHANNEL_ID": "",
        "AMOJO_CHANNEL_SECRET": "",
        "AMOJO_TITLE": "SatoChat",
        "AMOJO_SCOPE_ID": "",
        "INTERNAL_TOKEN": "",
        "BOT_INTERNAL_URL": "http://127.0.0.1:8082/internal/deliver",
    }
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                cfg[key] = val
    except FileNotFoundError:
        logger.warning("env file %s not found; amoCRM disabled", ENV_FILE)
    except Exception as exc:  # never fail the request because of config IO
        logger.error("failed reading env file %s: %s", ENV_FILE, exc)
    return cfg


def amo_enabled(cfg):
    """amoCRM is considered enabled only if a client id is configured AND a
    tokens file exists (i.e. OAuth has been completed)."""
    return bool(cfg.get("CLIENT_ID")) and os.path.exists(TOKENS_FILE)


# --------------------------------------------------------------------------- #
# Helpers: durable disk writes
# --------------------------------------------------------------------------- #

def append_jsonl(path, obj):
    """Append one JSON object as a line. ensure_ascii=False to keep Cyrillic
    readable. Best-effort durability via flush+fsync. Raises on hard failure
    only for the caller to decide (lead capture treats this as critical)."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _file_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass  # fsync may not be supported on all filesystems


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def client_ip():
    """Resolve the real client IP. nginx sets X-Forwarded-For / X-Real-IP."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or ""


# --------------------------------------------------------------------------- #
# Helpers: token persistence + refresh
# --------------------------------------------------------------------------- #

def read_tokens():
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error("failed reading tokens file: %s", exc)
        return None


def write_tokens(tokens):
    """Persist tokens atomically with mode 0600."""
    tmp = TOKENS_FILE + ".tmp"
    data = json.dumps(tokens, ensure_ascii=False, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, TOKENS_FILE)
        os.chmod(TOKENS_FILE, 0o600)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _token_request(cfg, payload):
    """Low-level POST to the OAuth token endpoint."""
    url = cfg["BASE_URL"].rstrip("/") + "/oauth2/access_token"
    resp = requests.post(
        url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    return resp


def exchange_code(cfg, code):
    """§1 authorization_code -> tokens. Returns saved token dict, raises on
    failure."""
    payload = {
        "client_id": cfg["CLIENT_ID"],
        "client_secret": cfg["CLIENT_SECRET"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["REDIRECT_URI"],
    }
    resp = _token_request(cfg, payload)
    if resp.status_code != 200:
        raise RuntimeError(
            "token exchange failed: %s %s" % (resp.status_code, resp.text[:500])
        )
    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at_epoch": int(time.time()) + int(data.get("expires_in", 86400)),
    }
    write_tokens(tokens)
    logger.info("OAuth code exchanged; tokens persisted")
    return tokens


def refresh_tokens(cfg, failed_access_token=None):
    """§2 refresh flow. Serialized via _token_lock. Re-reads stored tokens
    after acquiring the lock so we never reuse a rotated (single-use)
    refresh_token. Returns the (possibly already-refreshed) token dict.

    Two callers:
      * proactive (failed_access_token=None): refresh only if the stored token
        is within the skew window of expiry; otherwise reuse it (another worker
        may have just refreshed while we waited on the lock).
      * reactive 401 (failed_access_token set): we got a 401 on a specific
        access token. Force a refresh — UNLESS the token now on disk already
        differs from the one that failed, which means a concurrent worker
        already rotated it; in that case reuse the fresh on-disk token.
    """
    with _token_lock:
        current = read_tokens()
        if current is None:
            raise RuntimeError("no tokens on disk; re-authorize required")
        if failed_access_token is not None:
            # Reactive path: skip the network refresh only if someone else
            # already replaced the token we failed on.
            if current.get("access_token") != failed_access_token:
                return current
        else:
            # Proactive path: if another worker refreshed while we waited for
            # the lock, the token is valid again — bail out and reuse it.
            if not _needs_refresh(current):
                return current
        payload = {
            "client_id": cfg["CLIENT_ID"],
            "client_secret": cfg["CLIENT_SECRET"],
            "grant_type": "refresh_token",
            "refresh_token": current["refresh_token"],
            "redirect_uri": cfg["REDIRECT_URI"],
        }
        resp = _token_request(cfg, payload)
        if resp.status_code != 200:
            raise RuntimeError(
                "token refresh failed: %s %s"
                % (resp.status_code, resp.text[:500])
            )
        data = resp.json()
        tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],  # MUST overwrite (rotated)
            "expires_at_epoch": int(time.time())
            + int(data.get("expires_in", 86400)),
        }
        write_tokens(tokens)
        logger.info("access_token refreshed; new refresh_token persisted")
        return tokens


def _needs_refresh(tokens):
    return time.time() >= int(tokens.get("expires_at_epoch", 0)) - TOKEN_SKEW


def get_valid_access_token(cfg):
    """Return a non-expired access token, refreshing proactively if needed."""
    tokens = read_tokens()
    if tokens is None:
        raise RuntimeError("no tokens on disk; re-authorize required")
    if _needs_refresh(tokens):
        tokens = refresh_tokens(cfg)
    return tokens["access_token"]


# --------------------------------------------------------------------------- #
# Helpers: amoCRM API calls with one-shot 401 refresh+retry
# --------------------------------------------------------------------------- #

def amo_request(cfg, method, path, json_body):
    """Perform an authenticated amoCRM API call. On 401, refresh once and
    retry once (§6). Returns the requests.Response. Raises on transport error
    (caller handles by queueing to pending)."""
    url = cfg["BASE_URL"].rstrip("/") + path
    token = get_valid_access_token(cfg)

    def _do(tok):
        return requests.request(
            method,
            url,
            json=json_body,
            headers={
                "Authorization": "Bearer " + tok,
                "Content-Type": "application/json",
            },
            timeout=HTTP_TIMEOUT,
        )

    resp = _do(token)
    if resp.status_code == 401:
        logger.warning("amoCRM 401 on %s; refreshing token and retrying once", path)
        token = refresh_tokens(cfg, failed_access_token=token)["access_token"]
        resp = _do(token)
    return resp


def build_complex_lead(cfg, lead):
    """Build the §3 /leads/complex array body from a normalized lead dict."""
    # "format" is a human string like "Полный пакет (4 дня) — 1800 €";
    # scan for a known price token (1800 before 800 — "1800" contains "800").
    _fmt = str(lead.get("format") or "")
    price = 0
    for _tok in ("1800", "1000", "800", "500"):
        if _tok in _fmt:
            price = int(_tok)
            break

    utm_source = (lead.get("utm_source") or "").strip()
    tags = [
        {"name": (lead.get("format") or "no-format").strip()},
        {"name": utm_source if utm_source else "direct"},
    ]

    contact_fields = []
    phone = (lead.get("phone") or "").strip()
    email = (lead.get("email") or "").strip()
    if phone:
        contact_fields.append({
            "field_code": "PHONE",
            "values": [{"value": phone, "enum_code": "WORK"}],
        })
    if email:
        contact_fields.append({
            "field_code": "EMAIL",
            "values": [{"value": email, "enum_code": "WORK"}],
        })

    contact = {"name": (lead.get("name") or "Без имени").strip()}
    if contact_fields:
        contact["custom_fields_values"] = contact_fields

    obj = {
        "name": "Заявка с сайта — Лендинг конференции Казань-Токио 2026",
        "price": price,
        "_embedded": {
            "tags": tags,
            "contacts": [contact],
        },
    }
    if cfg.get("PIPELINE_ID"):
        try:
            obj["pipeline_id"] = int(cfg["PIPELINE_ID"])
        except ValueError:
            logger.error("invalid PIPELINE_ID in env: %r", cfg["PIPELINE_ID"])
    if cfg.get("STATUS_ID"):
        try:
            obj["status_id"] = int(cfg["STATUS_ID"])
        except ValueError:
            logger.error("invalid STATUS_ID in env: %r", cfg["STATUS_ID"])

    return [obj]


def build_note_text(lead):
    """§4 — dump UTM / marketing / city / channel / message into a note."""
    def g(k):
        v = lead.get(k)
        return "" if v is None else str(v)

    lines = []
    lines.append("Источник заявки: лендинг sadaosato.pro")
    if g("city"):
        lines.append("Город: " + g("city"))
    if g("format"):
        lines.append("Формат участия: " + g("format"))
    if g("channel"):
        lines.append("Удобный канал связи: " + g("channel"))
    if g("phone"):
        lines.append("Телефон: " + g("phone"))
    if g("email"):
        lines.append("Email: " + g("email"))
    lines.append("Согласие на обработку ПДн: " + ("да" if lead.get("consent") else "нет"))

    utm_keys = [
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    ]
    utm_present = [k for k in utm_keys if g(k)]
    if utm_present:
        lines.append("UTM:")
        for k in utm_present:
            lines.append("  %s=%s" % (k, g(k)))

    for k, label in (
        ("gclid", "gclid"),
        ("yclid", "yclid"),
        ("fbclid", "fbclid"),
    ):
        if g(k):
            lines.append("%s: %s" % (label, g(k)))

    if g("page_url"):
        lines.append("Страница: " + g("page_url"))
    if g("referrer"):
        lines.append("Referrer: " + g("referrer"))
    if g("first_visit_ts"):
        lines.append("Первый визит (ts): " + g("first_visit_ts"))
    if g("submit_ts"):
        lines.append("Отправка формы (ts): " + g("submit_ts"))
    if g("screen"):
        lines.append("Экран: " + g("screen"))
    if g("user_agent"):
        lines.append("User-Agent: " + g("user_agent"))
    if g("ip"):
        lines.append("IP: " + g("ip"))
    if g("server_ts"):
        lines.append("Получено сервером: " + g("server_ts"))

    if g("message"):
        lines.append("")
        lines.append("Комментарий клиента:")
        lines.append(g("message"))

    return "\n".join(lines)


def push_to_amo(cfg, lead):
    """Create lead+contact (§3), then attach a note (§4). Returns lead id on
    success. Raises on any failure so the caller can queue to pending.

    BONUS (amoJo gap-fix): when the /leads/complex response embeds the created
    contact id, persist a {tg_id, contact_id, lead_id} row to lead_map.jsonl so
    /amojo/outbound can link the chat to the deal without a phone query.
    """
    # (A) anti-duplicate: if this phone already has an ACTIVE lead, reuse it
    # instead of creating a second deal. Attach a note so the new touch's detail
    # is preserved; later forward('registered') updates fields on the same lead.
    try:
        import amo_sync
        existing = amo_sync.find_lead_by_phone(lead.get("phone")) if lead.get("phone") else None
    except Exception as exc:
        logger.error("amo dedup lookup failed (will create): %s", exc)
        existing = None
    if existing:
        try:
            amo_request(cfg, "POST", "/api/v4/leads/%d/notes" % int(existing),
                        [{"note_type": "common",
                          "params": {"text": "Повторное касание (антидубль):\n" + build_note_text(lead)}}])
        except Exception as exc:
            logger.error("amo reuse-note failed for lead %s: %s", existing, exc)
        logger.info("amo dedup: reused existing lead id=%s (no new deal)", existing)
        return existing

    body = build_complex_lead(cfg, lead)
    resp = amo_request(cfg, "POST", "/api/v4/leads/complex", body)
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            "leads/complex failed: %s %s" % (resp.status_code, resp.text[:800])
        )
    data = resp.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("leads/complex unexpected response: %s" % resp.text[:500])
    lead_id = data[0].get("id")
    if not lead_id:
        raise RuntimeError("leads/complex returned no lead id: %s" % resp.text[:500])

    # Best-effort contact_id capture for amoJo deal-linking (never fatal).
    try:
        contacts = ((data[0].get("_embedded") or {}).get("contacts") or [])
        contact_id = contacts[0].get("id") if contacts else None
        tg_id = lead.get("tg_id")
        if contact_id and tg_id:
            append_jsonl(LEAD_MAP_FILE, {
                "ts": now_iso(),
                "tg_id": tg_id,
                "contact_id": contact_id,
                "lead_id": lead_id,
            })
    except Exception as exc:
        logger.error("contact_id capture failed for lead %s: %s", lead_id, exc)

    # Attach note (best effort — if note fails, the lead still exists in amo,
    # so we treat note failure as non-fatal but log it).
    try:
        note_body = [{
            "note_type": "common",
            "params": {"text": build_note_text(lead)},
        }]
        nresp = amo_request(
            cfg, "POST", "/api/v4/leads/%d/notes" % int(lead_id), note_body
        )
        if nresp.status_code not in (200, 201):
            logger.error(
                "note attach failed for lead %s: %s %s",
                lead_id, nresp.status_code, nresp.text[:500],
            )
    except Exception as exc:
        logger.error("note attach raised for lead %s: %s", lead_id, exc)

    return lead_id


# --------------------------------------------------------------------------- #
# Helpers: amoJo contact resolution
# --------------------------------------------------------------------------- #

def _lead_map_contact(tg_id):
    """Look up a contact_id captured at lead-creation time (cheap, no network)."""
    if not tg_id or not os.path.exists(LEAD_MAP_FILE):
        return None
    found = None
    try:
        with open(LEAD_MAP_FILE, "r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                if str(row.get("tg_id")) == str(tg_id) and row.get("contact_id"):
                    found = row["contact_id"]  # last wins
    except Exception as exc:
        logger.error("lead_map read failed: %s", exc)
    return found


def _resolve_contact_id(cfg, tg_id, phone):
    """Resolve a CRM contact_id: lead_map first, then GET /contacts?query=phone."""
    cid = _lead_map_contact(tg_id)
    if cid:
        return cid
    phone = (phone or "").strip()
    if not phone or not amo_enabled(cfg):
        return None
    try:
        from urllib.parse import quote

        resp = amo_request(
            cfg, "GET", "/api/v4/contacts?query=%s" % quote(phone), None
        )
    except Exception as exc:
        logger.error("contacts query transport error: %s", exc)
        return None
    if resp.status_code == 204:
        return None
    if resp.status_code != 200:
        logger.error("contacts?query -> %s", resp.status_code)
        return None
    try:
        hits = (resp.json().get("_embedded") or {}).get("contacts") or []
        return hits[0].get("id") if hits else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #

def _cors_origin():
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        return origin
    return None


@app.after_request
def add_cors_headers(resp):
    origin = _cors_origin()
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def handle_preflight():
    """Respond to a CORS preflight OPTIONS request."""
    resp = make_response("", 204)
    return resp


def _is_loopback_request():
    """True only when the request originates from loopback (defence-in-depth).

    nginx does not proxy /amojo/, so in production these only ever arrive from
    the bot on 127.0.0.1. We still verify remote_addr to refuse any spoofed
    forward.
    """
    return (request.remote_addr or "") in _LOOPBACK_HOSTS


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/api/lead", methods=["POST", "OPTIONS"])
def api_lead():
    if request.method == "OPTIONS":
        return handle_preflight()

    # ---- parse input (never crash on bad JSON) ----
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    name = (payload.get("name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    email = (payload.get("email") or "").strip()

    # ---- validation: name required, and phone OR email required ----
    errors = []
    if not name:
        errors.append("name")
    if not phone and not email:
        errors.append("phone_or_email")
    if errors:
        return jsonify({"ok": False, "error": "validation", "fields": errors}), 400

    # ---- build the full durable record ----
    fields = [
        "name", "phone", "email", "city", "format", "channel", "message",
        "consent", "consent_oferta", "consent_pdn", "page_url", "referrer",
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "gclid", "yclid", "fbclid",
        "first_visit_ts", "submit_ts", "user_agent", "screen",
        "tg_id", "external_id",
    ]
    record = {k: payload.get(k) for k in fields}
    record["server_ts"] = now_iso()
    record["ip"] = client_ip()

    # ---- (2) ALWAYS persist to disk FIRST, before any network call ----
    # If even this fails, we still try amoCRM and never 500 the client, but a
    # disk-write failure here is logged loudly.
    try:
        append_jsonl(LEADS_FILE, record)
    except Exception as exc:
        logger.error("CRITICAL: failed to persist lead to %s: %s", LEADS_FILE, exc)

    # ---- (3) best-effort amoCRM delivery ----
    cfg = load_env()
    lead_id = None
    if amo_enabled(cfg):
        try:
            lead_id = push_to_amo(cfg, record)
            logger.info("lead delivered to amoCRM: id=%s", lead_id)
        except Exception as exc:
            logger.error("amoCRM delivery failed; queuing to pending: %s", exc)
            try:
                append_jsonl(PENDING_FILE, {
                    "queued_ts": now_iso(),
                    "error": str(exc)[:500],
                    "lead": record,
                })
            except Exception as exc2:
                logger.error("CRITICAL: failed to queue pending lead: %s", exc2)
    else:
        # amoCRM not configured -> queue so backfill can deliver once enabled.
        logger.info("amoCRM not configured; lead captured to disk + pending queue")
        try:
            append_jsonl(PENDING_FILE, {
                "queued_ts": now_iso(),
                "error": "amocrm_not_configured",
                "lead": record,
            })
        except Exception as exc2:
            logger.error("CRITICAL: failed to queue pending lead: %s", exc2)

    # ---- (4) always respond ok ----
    # Mirror into the local operational registrations store + consent audit (never fatal).
    try:
        import reg
        _tg = payload.get("tg_id") or payload.get("telegram_user_id")
        _rid = reg.upsert_registration(dict(
            record, amocrm_lead_id=lead_id,
            source=("bot" if _tg else "web"),
            telegram_user_id=_tg,
            telegram_username=payload.get("username") or payload.get("telegram_username"),
        ))
        _VER = "Редакция №1 от 01.06.2026"
        if record.get("consent_oferta"):
            reg.log_consent(_rid, "oferta", _VER, subject_phone=record.get("phone"),
                            subject_email=record.get("email"), ip=record.get("ip"),
                            user_agent=record.get("user_agent"), channel="web")
        if record.get("consent_pdn") or record.get("consent"):
            reg.log_consent(_rid, "pdn", _VER, subject_phone=record.get("phone"),
                            subject_email=record.get("email"), ip=record.get("ip"),
                            user_agent=record.get("user_agent"), channel="web")
        # push initial funnel state to amoCRM (stage + fields/tags/task)
        try:
            import amo_sync
            amo_sync.forward(_rid, "registered")
        except Exception as _e:
            logger.error("amo forward(registered) failed: %s", _e)
    except Exception as _exc:
        logger.error("reg mirror failed: %s", _exc)

    return jsonify({"ok": True, "id": lead_id})


@app.route("/api/event", methods=["POST", "OPTIONS"])
def api_event():
    if request.method == "OPTIONS":
        return handle_preflight()

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    fields = [
        "type", "name", "target", "page_url",
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "ts",
    ]
    record = {k: payload.get(k) for k in fields}
    record["server_ts"] = now_iso()
    record["ip"] = client_ip()

    try:
        append_jsonl(EVENTS_FILE, record)
    except Exception as exc:
        logger.error("failed to persist event: %s", exc)

    return jsonify({"ok": True})


@app.route("/amo/callback", methods=["GET"])
def amo_callback():
    """OAuth2 redirect target. amoCRM appends ?code=...&referer=...&client_id=..."""
    cfg = load_env()
    code = request.args.get("code", "")
    error = request.args.get("error", "")

    if error:
        logger.error("OAuth callback error param: %s", error)
        return _render_callback(False, "amoCRM вернул ошибку авторизации: %s" % error)

    if not code:
        return _render_callback(False, "В запросе отсутствует параметр code.")

    if not cfg.get("CLIENT_ID") or not cfg.get("CLIENT_SECRET"):
        return _render_callback(
            False,
            "Интеграция не настроена: CLIENT_ID / CLIENT_SECRET не заданы в /etc/sato/amo.env."
        )

    try:
        exchange_code(cfg, code)
    except Exception as exc:
        logger.error("OAuth code exchange failed: %s", exc)
        return _render_callback(False, "Не удалось обменять код на токен: %s" % str(exc)[:300])

    return _render_callback(True, None)


# --------------------------------------------------------------------------- #
# amoJo: outbound (bot -> amoCRM) and inbound webhook (manager -> bot)
# --------------------------------------------------------------------------- #

@app.route("/amojo/outbound", methods=["POST"])
def amojo_outbound():
    """LOOPBACK only. The bot posts a client message to import into amoJo.

    Body: {tg_id, conversation_id, name, username, phone, text, msgid}.
    Steps: ensure scope_id; ensure_chat; lazily resolve+link contact; send.
    """
    cfg = load_env()
    if not _is_loopback_request():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    token = cfg.get("INTERNAL_TOKEN") or ""
    if not token or request.headers.get("X-Internal-Token", "") != token:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if not amojo.amojo_enabled(cfg):
        # Dormant — accept and no-op so the bot never errors.
        return jsonify({"ok": True, "amojo": "disabled"})

    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    tg_id = body.get("tg_id")
    conversation_id = body.get("conversation_id") or ("tgbot-%s" % tg_id)
    text = (body.get("text") or "").strip()
    if not tg_id or not text:
        return jsonify({"ok": False, "error": "missing_fields"}), 400

    state = amojo.read_state()
    scope_id = state.get("scope_id") or cfg.get("AMOJO_SCOPE_ID")
    if not scope_id:
        logger.error("amojo outbound: no scope_id (run amojo_connect.py)")
        return jsonify({"ok": False, "error": "no_scope_id"}), 503

    phone = (body.get("phone") or "").strip()
    name = body.get("name") or "Клиент"
    user_dict = {
        "id": conversation_id,  # stable unique client id
        "name": name,
    }
    profile = {}
    if phone:
        profile["phone"] = phone
    if profile:
        user_dict["profile"] = profile

    chat_id = amojo.ensure_chat(cfg, scope_id, conversation_id, user_dict, logger=logger)
    if not chat_id:
        return jsonify({"ok": False, "error": "ensure_chat_failed"}), 502

    # Lazily link the chat to the CRM contact (cached in state once linked).
    try:
        cached = (amojo.read_state().get("chats") or {}).get(conversation_id) or {}
        if not cached.get("linked"):
            contact_id = cached.get("contact_id") or _resolve_contact_id(cfg, tg_id, phone)
            if contact_id and amojo.link_chat_to_contact(
                cfg, amo_request, contact_id, chat_id, logger=logger
            ):
                st = amojo.read_state()
                ch = st.setdefault("chats", {}).setdefault(conversation_id, {})
                ch["chat_id"] = chat_id
                ch["contact_id"] = contact_id
                ch["linked"] = True
                amojo.write_state(st)
    except Exception as exc:
        logger.error("amojo link step failed (non-fatal): %s", exc)

    sender = {"id": conversation_id, "name": name}
    if phone:
        sender["profile"] = {"phone": phone}
    msgid = body.get("msgid") or ("tg-%s-%s" % (tg_id, int(time.time())))
    ok, _ = amojo.send_message(
        cfg, scope_id, conversation_id, sender, text, msgid, logger=logger
    )
    if not ok:
        return jsonify({"ok": False, "error": "send_failed", "chat_id": chat_id}), 502
    return jsonify({"ok": True, "chat_id": chat_id})


@app.route("/amo/chat-webhook", methods=["POST"])
@app.route("/amo/chat-webhook/<path:scope_id>", methods=["POST"])
def amo_chat_webhook(scope_id=None):
    """PUBLIC (via nginx /amo/). amoJo POSTs manager replies here.

    Verify X-Signature over the RAW body, forward manager messages to the bot's
    loopback /internal/deliver. Returns 200 fast (amojo retries on non-2xx).
    """
    cfg = load_env()
    raw = request.get_data()  # RAW bytes BEFORE parsing — required for signature
    secret = cfg.get("AMOJO_CHANNEL_SECRET") or ""
    sig = request.headers.get("X-Signature", "")
    if not amojo.amojo_enabled(cfg):
        # Dormant: acknowledge so amojo (if somehow pointed here) stops retrying.
        return jsonify({"ok": True, "amojo": "disabled"}), 200
    if not amojo.verify_inbound(raw, sig, secret):
        logger.warning("amo chat-webhook: bad signature")
        return jsonify({"ok": False, "error": "bad_signature"}), 403

    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        payload = {}

    msg = payload.get("message") or {}
    sender = msg.get("sender") or {}
    receiver = msg.get("receiver") or {}
    conversation = msg.get("conversation") or {}
    inner = msg.get("message") or {}
    text = (inner.get("text") or "").strip()

    # Filter self-echo: amojo echoes the inbound (client) messages we import.
    # Only forward when the message is FROM a manager (sender present) and the
    # sender is NOT our own imported client id. The client's id equals the
    # conversation_id we used on import (tgbot-{tg_id}).
    conv_id = conversation.get("id") or conversation.get("client_id") or ""
    source_ext = (msg.get("source") or {}).get("external_id") or ""
    receiver_client_id = receiver.get("client_id") or receiver.get("id") or ""
    sender_id = sender.get("id") or ""

    # Map back to tg_id from the conversation external id (tgbot-{tg_id}).
    tg_id = _extract_tg_id(conv_id) or _extract_tg_id(source_ext) or _extract_tg_id(receiver_client_id)

    # Skip echoes of our imported client messages (sender == our client id).
    if sender_id and tg_id and sender_id == ("tgbot-%s" % tg_id):
        return jsonify({"ok": True, "skipped": "self_echo"}), 200

    if not text or not tg_id:
        # Nothing actionable (e.g. status event); ack so amojo doesn't retry.
        return jsonify({"ok": True, "skipped": "no_text_or_target"}), 200

    delivered = _deliver_to_bot(cfg, tg_id, text)

    # Best-effort delivery status back to amojo.
    try:
        state = amojo.read_state()
        scope_id = state.get("scope_id") or cfg.get("AMOJO_SCOPE_ID")
        msgid = inner.get("id")
        if scope_id and msgid:
            amojo.report_delivery(cfg, scope_id, msgid, delivered, logger=logger)
    except Exception:
        pass

    return jsonify({"ok": True}), 200


def _extract_tg_id(value):
    """Pull the numeric tg_id out of a 'tgbot-{tg_id}' style identifier."""
    s = str(value or "")
    if s.startswith("tgbot-"):
        tail = s[len("tgbot-"):]
        if tail.lstrip("-").isdigit():
            return int(tail)
    return None


def _deliver_to_bot(cfg, tg_id, text):
    """POST the manager reply to the bot's loopback /internal/deliver."""
    url = cfg.get("BOT_INTERNAL_URL") or "http://127.0.0.1:8082/internal/deliver"
    token = cfg.get("INTERNAL_TOKEN") or ""
    try:
        resp = requests.post(
            url,
            json={"tg_id": tg_id, "text": text},
            headers={"X-Internal-Token": token, "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 300:
            logger.error("bot deliver -> %s", resp.status_code)
            return False
        return True
    except Exception as exc:
        logger.error("bot deliver transport error: %s", exc)
        return False


def _render_callback(ok, error_msg):
    if ok:
        title = "amoCRM подключён ✓"
        body = ("Токены сохранены. Интеграция активна — заявки с сайта "
                "теперь попадают в amoCRM автоматически.")
        color = "#1a7f37"
        status = 200
    else:
        title = "Ошибка подключения amoCRM"
        body = error_msg or "Неизвестная ошибка."
        color = "#b42318"
        status = 400

    html = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       background:#0f1115;color:#e7e9ee;display:flex;min-height:100vh;
       align-items:center;justify-content:center;padding:24px;}
  .card{max-width:520px;background:#171a21;border:1px solid #232733;
        border-radius:16px;padding:36px;text-align:center;
        box-shadow:0 20px 60px rgba(0,0,0,.45);}
  h1{margin:0 0 14px;font-size:26px;color:%(color)s;}
  p{margin:0;line-height:1.6;color:#aeb4c0;font-size:16px;}
</style>
</head>
<body>
  <div class="card">
    <h1>%(title)s</h1>
    <p>%(body)s</p>
  </div>
</body>
</html>""" % {"title": title, "body": body, "color": color}

    return Response(html, status=status, mimetype="text/html; charset=utf-8")


@app.route("/api/amo/event", methods=["POST"])
def api_amo_event():
    """Bot -> backend: emit a funnel event (sbp_shown, i_paid, ...) for amoCRM."""
    import amo_sync
    cfg = load_env()
    tok = cfg.get("INTERNAL_TOKEN") or ""
    if not tok or request.headers.get("X-Internal-Token", "") != tok:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = request.get_json(force=True, silent=True) or {}
    event = d.get("event")
    reg_id = d.get("reg_id")
    if not reg_id and d.get("tg_id"):
        try:
            import reg as _reg
            c = _reg._conn()
            r = c.execute("SELECT id FROM registrations WHERE telegram_user_id=? ORDER BY created_at DESC LIMIT 1",
                          (int(d["tg_id"]),)).fetchone()
            c.close()
            reg_id = r["id"] if r else None
        except Exception:
            reg_id = None
    if not reg_id or not event:
        return jsonify({"ok": False, "error": "bad_request"}), 400
    # carry the tariff the user chose in the bot onto the registration, so a later
    # paid-confirmation can auto-issue the ticket without a "no tariff" task.
    package = str(d.get("package") or "").strip()
    if package in ("500", "800", "1000", "1800"):
        try:
            import reg as _reg
            c = _reg._conn()
            c.execute("UPDATE registrations SET package=?, price_eur=COALESCE(price_eur,?), updated_at=? WHERE id=?",
                      (package, int(package), now_iso(), reg_id))
            c.commit()
            c.close()
        except Exception as exc:
            logger.error("amo/event package set failed: %s", exc)
    amo_sync.forward(reg_id, event)
    return jsonify({"ok": True})


@app.route("/amo/lead-webhook", methods=["POST"])
@app.route("/amo/lead-webhook/<path_key>", methods=["POST"])
def amo_lead_webhook(path_key=None):
    """amoCRM -> backend: react to a manager-driven stage change (reverse).
    Secret accepted in the PATH (amoCRM may drop query strings) or ?key=."""
    import amo_sync
    cfg = load_env()
    secret = cfg.get("AMO_WEBHOOK_SECRET") or ""
    provided = path_key or request.args.get("key", "")
    if not secret or provided != secret:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    form = request.form
    acc = form.get("account[id]") or ""
    if acc and str(acc) != str(amo_sync.ACCOUNT_ID):
        return jsonify({"ok": False, "error": "account_mismatch"}), 403
    # 1) stage change (manager dragged the card)
    sid = form.get("leads[status][0][id]")
    if sid:
        try:
            new_status = int(form.get("leads[status][0][status_id]"))
        except Exception:
            return jsonify({"ok": True, "ignored": "no_status"})
        res = amo_sync.reverse_status_change(int(sid), new_status)
        logger.info("amo reverse status lead=%s -> %s -> %s", sid, new_status, res)
        return jsonify({"ok": True, **res})
    # 2) field update (e.g. «Статус оплаты»=Оплачено) -> automation issues + moves card
    uid = form.get("leads[update][0][id]")
    if uid:
        res = amo_sync.reverse_field_paid(int(uid))
        if res.get("action") not in ("pay_field_not_set", "already_done"):
            logger.info("amo reverse update lead=%s -> %s", uid, res)
        return jsonify({"ok": True, **res})
    return jsonify({"ok": True, "ignored": "no_event"})


try:
    import desk
    desk.register(app)
except Exception:
    logger.exception("desk reg failed")

try:
    import qrtrack
    qrtrack.register(app)
except Exception:
    logger.exception("qrtrack reg failed")

try:
    import tickets
    tickets.register(app)
except Exception:
    logger.exception("tickets blueprint registration failed")

try:
    import amo_sync
    amo_sync.init()
    logger.info("amo_sync initialized")
except Exception:
    logger.exception("amo_sync init failed")


if __name__ == "__main__":
    # Dev convenience only; production uses gunicorn.
    app.run(host="127.0.0.1", port=8081, debug=False)
