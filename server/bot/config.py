"""Configuration loader for the SATO summit bot.

All values are read from the process environment (populated by the systemd
EnvironmentFile at /etc/sato-bot/bot.env). Nothing is read from disk here so the
module stays import-safe and testable.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Required environment variable {name!r} is missing")
    return value if value is not None else ""


def _get_float(name: str, default: float) -> float:
    """Parse a float env var, falling back (with a warning) on bad input.

    Never raises at import time — a typo in bot.env must not crash-loop the
    service and lose leads while it's down.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        log.warning(
            "Env %s=%r is not a valid number, using default %s", name, raw, default
        )
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.warning(
            "Env %s=%r is not a valid integer, using default %s", name, raw, default
        )
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            log.warning("Ignoring non-integer admin id %r", chunk)
    return ids


def _parse_payment_urls(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("PAYMENT_URLS is not valid JSON, ignoring: %s", exc)
        return {}
    if not isinstance(data, dict):
        log.error("PAYMENT_URLS must be a JSON object, got %s", type(data).__name__)
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


BOT_TOKEN: str = _get("BOT_TOKEN", required=True)

# Channel that gates access. Stored WITH leading @ for display, normalised for API.
CHANNEL_USERNAME: str = _get("CHANNEL_USERNAME", "@sadaosato").strip()
if CHANNEL_USERNAME and not CHANNEL_USERNAME.startswith("@"):
    CHANNEL_USERNAME = "@" + CHANNEL_USERNAME
# chat_id form accepted by get_chat_member (@username works for public channels).
CHANNEL_CHAT_ID: str = CHANNEL_USERNAME
CHANNEL_URL: str = "https://t.me/" + CHANNEL_USERNAME.lstrip("@")

ADMIN_IDS: list[int] = _parse_admin_ids(_get("ADMIN_IDS", ""))

LEAD_API_URL: str = _get("LEAD_API_URL", "http://127.0.0.1:8081/api/lead").strip()

PAYMENT_URLS: dict[str, str] = _parse_payment_urls(_get("PAYMENT_URLS", ""))

DB_PATH: str = _get("DB_PATH", "/var/lib/sato-bot/bot.sqlite3").strip()

# Optional: event start date for the generic pre-event reminder (ISO 8601).
# Empty disables the reminder. Example: 2026-10-01T10:00:00+03:00
EVENT_DATE: str = _get("EVENT_DATE", "").strip()

# Optional: human-readable early-bird cutoff shown in the last-call drip.
# Empty omits the dated line entirely (no fake deadline). Example: 1 сентября 2026
EARLY_BIRD_DEADLINE: str = _get("EARLY_BIRD_DEADLINE", "").strip()

# HTTP timeout (seconds) for the lead backend call.
LEAD_TIMEOUT: float = _get_float("LEAD_TIMEOUT", 12.0)

# Broadcast / drip send rate (messages per second).
SEND_RATE: float = _get_float("SEND_RATE", 25.0)

# --------------------------------------------------------------------------- #
# AI sales agent (gated on OPENAI_API_KEY presence).
# --------------------------------------------------------------------------- #
OPENAI_API_KEY: str = _get("OPENAI_API_KEY", "").strip()
# Master switch: AI is live only when explicitly enabled AND a key is present.
AI_ENABLED: bool = _get_bool("AI_ENABLED", True) and bool(OPENAI_API_KEY)
# hybrid | autopilot | suggester
AI_MODE: str = (_get("AI_MODE", "hybrid").strip().lower() or "hybrid")
if AI_MODE not in {"hybrid", "autopilot", "suggester"}:
    log.warning("AI_MODE=%r invalid, falling back to 'hybrid'", AI_MODE)
    AI_MODE = "hybrid"
AI_MODEL: str = _get("AI_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
AI_MAX_OUTPUT_TOKENS: int = _get_int("AI_MAX_OUTPUT_TOKENS", 700)
AI_PAUSE_MINUTES: int = _get_int("AI_PAUSE_MINUTES", 120)
# Hard cap on model<->tool round-trips per user turn (cost-DoS guard).
AI_MAX_TOOL_ROUNDS: int = _get_int("AI_MAX_TOOL_ROUNDS", 4)
# Per-user request throttle: max AI turns allowed inside AI_RATE_WINDOW_SEC.
AI_RATE_MAX_TURNS: int = _get_int("AI_RATE_MAX_TURNS", 6)
AI_RATE_WINDOW_SEC: int = _get_int("AI_RATE_WINDOW_SEC", 60)

# Telegram chat/forum to receive suggester drafts + escalations.
# Defaults to the first ADMIN_ID when unset.
MANAGER_CHAT_ID: int = _get_int(
    "MANAGER_CHAT_ID", ADMIN_IDS[0] if ADMIN_IDS else 0
)

# Optional fact-injection for the system prompt. Empty -> prompt falls back to
# content.py constants only (never invents facts).
EVENT_DATES: str = _get("EVENT_DATES", "").strip()
VENUE: str = _get("VENUE", "").strip()
EXTRA_FACTS: str = _get("EXTRA_FACTS", "").strip()

# --------------------------------------------------------------------------- #
# amoJo two-way chat bridge (gated on AMOJO_ENABLED + INTERNAL_TOKEN).
# Signing/credentials live on the BACKEND; the bot only knows the loopback
# endpoints + the shared INTERNAL_TOKEN that protects them.
# --------------------------------------------------------------------------- #
INTERNAL_TOKEN: str = _get("INTERNAL_TOKEN", "").strip()
AMOJO_ENABLED: bool = _get_bool("AMOJO_ENABLED", False) and bool(INTERNAL_TOKEN)
# Backend loopback endpoint the bot pushes client messages to.
AMOJO_OUTBOUND_URL: str = _get(
    "AMOJO_OUTBOUND_URL", "http://127.0.0.1:8081/amojo/outbound"
).strip()
# host:port the bot's internal delivery server binds to (loopback only).
INTERNAL_LISTEN: str = _get("INTERNAL_LISTEN", "127.0.0.1:8082").strip()


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def internal_listen_host_port() -> tuple[str, int]:
    host, _, port = INTERNAL_LISTEN.partition(":")
    host = host.strip() or "127.0.0.1"
    # The /internal/deliver endpoint is an arbitrary send_message primitive.
    # It must NEVER bind to a non-loopback address — force loopback if someone
    # mis-sets INTERNAL_LISTEN=0.0.0.0:8082.
    if host not in _LOOPBACK_HOSTS:
        log.warning(
            "INTERNAL_LISTEN host %r is not loopback — forcing 127.0.0.1 "
            "(internal delivery server must stay on loopback)",
            host,
        )
        host = "127.0.0.1"
    try:
        port_i = int(port.strip()) if port.strip() else 8082
    except ValueError:
        log.warning("INTERNAL_LISTEN port %r invalid, using 8082", port)
        port_i = 8082
    return host, port_i


def _warn_insecure_lead_url() -> None:
    """Warn if PII would be sent over plaintext http:// to a non-loopback host."""
    try:
        parsed = urlparse(LEAD_API_URL)
    except Exception:  # pragma: no cover - defensive
        return
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning(
            "LEAD_API_URL uses plaintext http:// to non-loopback host %r — "
            "phone numbers and names will be sent unencrypted. Use https://.",
            host or LEAD_API_URL,
        )


_warn_insecure_lead_url()


def summary() -> str:
    """Human-readable, secret-free config snapshot for startup logs."""
    return (
        "config: channel=%s admins=%d lead_api=%s payment_urls=%d db=%s "
        "event_date=%s early_bird=%s ai=%s(mode=%s,model=%s) amojo=%s"
        % (
            CHANNEL_USERNAME,
            len(ADMIN_IDS),
            LEAD_API_URL,
            len(PAYMENT_URLS),
            DB_PATH,
            EVENT_DATE or "(disabled)",
            EARLY_BIRD_DEADLINE or "(none)",
            "on" if AI_ENABLED else "off",
            AI_MODE,
            AI_MODEL,
            "on" if AMOJO_ENABLED else "off",
        )
    )
