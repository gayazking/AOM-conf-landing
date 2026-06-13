"""amoCRM funnel automation for the summit.

FORWARD: our internal events -> amoCRM (move stage + set fields/tags + task).
REVERSE helpers: manager drags a deal in amoCRM -> our actions (see app route
/amo/lead-webhook). Reuses app.amo_request (token refresh). Keeps reg.py
untouched: extra columns + lookups live here via reg._conn().

Decisions wired in: amoCRM-first (bot /paid stays a backup, both idempotent),
reverse WIN issues the ticket immediately, FORWARD aggressiveness = medium
(auto up to «проверить оплату»; never auto-move to WIN/LOST by intent — only by
real money/manual), existing stage names kept.
"""
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import reg

log = logging.getLogger("sato")

# --- voronka 10957730 stage ids ---
PIPELINE = 10957730
ST_UNSORTED = 86154866
ST_REG = 86154870        # Рег без оплаты
ST_DECIDE = 86154878     # Принимают решение
ST_CHECKPAY = 86326414   # проверить оплату
ST_WIN = 142             # Успешно — оплачено
ST_LOST = 143            # Закрыто и не реализовано
_RANK = {ST_UNSORTED: 0, ST_REG: 1, ST_DECIDE: 2, ST_CHECKPAY: 3, ST_WIN: 4, ST_LOST: 4}

ACCOUNT_ID = 33077174
DEFAULT_RESPONSIBLE = 13874042  # Almaz; overridable via env AMO_RESPONSIBLE_USER
ECHO_WINDOW = 120  # seconds: ignore reverse webhook that echoes our own forward

_FCACHE = {}

# package -> select-field values
_TARIFF = {"500": "Теория онлайн — 500 €", "800": "Теория оффлайн — 800 €",
           "1000": "Практика оффлайн — 1000 €", "1800": "Полный пакет (4 дня) — 1800 €"}
_FORMAT = {"500": "Онлайн", "800": "Оффлайн", "1000": "Оффлайн", "1800": "Оффлайн"}
_WB = {"500": "Без браслета (онлайн)", "800": "Зелёный (800)", "1000": "Синий (1000)", "1800": "Золотой (1800)"}
_TARIFF_TAG = {"500": "Теория · онлайн — 500 €", "800": "Теория · оффлайн — 800 €",
               "1000": "Практика · оффлайн — 1000 €", "1800": "Полный пакет (4 дня) — 1800 €"}
_TARIFF_REV = {v: k for k, v in _TARIFF.items()}  # «Теория онлайн — 500 €» -> "500"


# --------------------------------------------------------------------------- #
# config / api
# --------------------------------------------------------------------------- #
def _cfg():
    import app
    return app.load_env()


def _responsible(cfg):
    try:
        return int(cfg.get("AMO_RESPONSIBLE_USER") or DEFAULT_RESPONSIBLE)
    except Exception:
        return DEFAULT_RESPONSIBLE


def _amo(method, path, body=None):
    import app
    return app.amo_request(_cfg(), method, path, body)


def _now_ts():
    return int(time.time())


def _to_ts(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# local DB (reg.py stays untouched; columns added here)
# --------------------------------------------------------------------------- #
def init():
    c = reg._conn()
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(registrations)").fetchall()}
        for col, typ in (("amo_last_status_id", "INTEGER"), ("amo_forward_ts", "TEXT")):
            if col not in cols:
                try:
                    c.execute("ALTER TABLE registrations ADD COLUMN %s %s" % (col, typ))
                    c.commit()
                except Exception:
                    c.rollback()
    finally:
        c.close()


def _get_reg(reg_id):
    c = reg._conn()
    try:
        r = c.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _find_by_amo_lead(lead_id):
    c = reg._conn()
    try:
        r = c.execute("SELECT * FROM registrations WHERE amocrm_lead_id=? ORDER BY created_at DESC LIMIT 1",
                      (int(lead_id),)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _find_by_phone(phone):
    ph = reg.normalize_phone(phone) if phone else None
    if not ph:
        return None
    c = reg._conn()
    try:
        r = c.execute("SELECT * FROM registrations WHERE phone_e164=? ORDER BY created_at DESC LIMIT 1", (ph,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _attach_lead(reg_id, lead_id, contact_id=None):
    c = reg._conn()
    try:
        if contact_id:
            c.execute("UPDATE registrations SET amocrm_lead_id=?, amocrm_contact_id=COALESCE(amocrm_contact_id,?), updated_at=? WHERE id=?",
                      (int(lead_id), int(contact_id), _now_iso(), reg_id))
        else:
            c.execute("UPDATE registrations SET amocrm_lead_id=?, updated_at=? WHERE id=?",
                      (int(lead_id), _now_iso(), reg_id))
        c.commit()
    finally:
        c.close()


def _set_forward(reg_id, status_id):
    c = reg._conn()
    try:
        c.execute("UPDATE registrations SET amo_last_status_id=?, amo_forward_ts=? WHERE id=?",
                  (int(status_id), str(_now_ts()), reg_id))
        c.commit()
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# amoCRM field/stage/tag/task primitives
# --------------------------------------------------------------------------- #
def _fields():
    if _FCACHE:
        return _FCACHE
    try:
        r = _amo("GET", "/api/v4/leads/custom_fields?limit=250")
        for f in r.json().get("_embedded", {}).get("custom_fields", []):
            _FCACHE[f["name"]] = {"id": f["id"], "type": f["type"],
                                  "enums": {e["value"]: e["id"] for e in (f.get("enums") or [])}}
    except Exception as exc:
        log.error("amo fields fetch failed: %s", exc)
    return _FCACHE


def _cfv(name, value):
    f = _fields().get(name)
    if not f or value is None or value == "":
        return None
    if f["type"] == "select":
        eid = f["enums"].get(value)
        if not eid:
            log.error("amo: no enum %r for field %s", value, name)
            return None
        return {"field_id": f["id"], "values": [{"enum_id": eid}]}
    if f["type"] == "date":
        ts = value if isinstance(value, int) else _to_ts(value)
        return {"field_id": f["id"], "values": [{"value": ts}]} if ts else None
    if f["type"] == "numeric":
        return {"field_id": f["id"], "values": [{"value": str(value)}]}
    return {"field_id": f["id"], "values": [{"value": str(value)}]}


def _get_lead(lead_id, withp="contacts"):
    try:
        r = _amo("GET", "/api/v4/leads/%d?with=%s" % (int(lead_id), withp))
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _tariff_from_lead(lead_id):
    """Read the «Тариф» select field from an amoCRM lead -> package "500".."1800"."""
    lead = _get_lead(lead_id)
    if not lead:
        return None
    fmap = _fields()
    tid = (fmap.get("Тариф") or {}).get("id")
    e2v = {eid: v for n, f in fmap.items() for v, eid in f["enums"].items()}
    for cf in (lead.get("custom_fields_values") or []):
        if cf.get("field_id") == tid:
            for val in (cf.get("values") or []):
                v = e2v.get(val.get("enum_id"))
                if v in _TARIFF_REV:
                    return _TARIFF_REV[v]
    return None


def safe_set_stage(lead_id, target):
    """Move stage with guards: never move a terminal (WIN/LOST) deal, never go
    backwards in rank. Returns True if the deal is now at <=target meaningfully."""
    lead = _get_lead(lead_id)
    if not lead:
        return False
    cur = lead.get("status_id")
    if cur in (ST_WIN, ST_LOST):
        return False
    if _RANK.get(target, 0) < _RANK.get(cur, 0):
        return False
    if cur == target:
        return True
    try:
        r = _amo("PATCH", "/api/v4/leads/%d" % int(lead_id), {"status_id": target, "pipeline_id": PIPELINE})
        return r.status_code == 200
    except Exception as exc:
        log.error("amo safe_set_stage failed: %s", exc)
        return False


def patch_lead(lead_id, fields=None, add_tags=None):
    body = {}
    cfv = []
    for name, val in (fields or {}).items():
        e = _cfv(name, val)
        if e:
            cfv.append(e)
    if cfv:
        body["custom_fields_values"] = cfv
    if add_tags:
        lead = _get_lead(lead_id)
        cur = [t["name"] for t in lead.get("_embedded", {}).get("tags", [])] if lead else []
        merged = sorted(set(cur) | set(add_tags))
        body["_embedded"] = {"tags": [{"name": t} for t in merged]}
    if not body:
        return True
    try:
        r = _amo("PATCH", "/api/v4/leads/%d" % int(lead_id), body)
        if r.status_code != 200:
            log.error("amo patch_lead %s: %s %s", lead_id, r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as exc:
        log.error("amo patch_lead raised: %s", exc)
        return False


def create_task(lead_id, text, task_type=1, due_in_sec=1800):
    try:
        body = [{"text": text, "task_type_id": task_type, "complete_till": _now_ts() + due_in_sec,
                 "entity_id": int(lead_id), "entity_type": "leads", "responsible_user_id": _responsible(_cfg())}]
        r = _amo("POST", "/api/v4/tasks", body)
        return r.status_code in (200, 201)
    except Exception as exc:
        log.error("amo create_task failed: %s", exc)
        return False


def process_merge_conflicts(limit=100):
    """(B) For each unhandled amo_dup in merge_log, create ONE manager task to
    merge the two amoCRM cards (same human, already merged in our DB). Idempotent
    — sets handled=1 so a card is never re-flagged. Returns count of tasks made."""
    c = reg._conn()
    try:
        rows = c.execute(
            "SELECT id, amo_lead_a, amo_lead_b FROM merge_log "
            "WHERE reason='amo_dup' AND handled=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
    finally:
        c.close()
    done = 0
    for r in rows:
        a, b = r["amo_lead_a"], r["amo_lead_b"]
        ok = True
        if a and b and int(a) != int(b):
            txt = ("Дубль в amoCRM: один человек — две сделки #%s и #%s "
                   "(объединены в нашей БД). Слейте карточки: перенесите примечания/"
                   "задачи в #%s и закройте лишнюю." % (a, b, a))
            ok = create_task(int(a or b), txt, 1, 3600)
        if ok:
            cc = reg._conn()
            try:
                cc.execute("UPDATE merge_log SET handled=1 WHERE id=?", (r["id"],))
                cc.commit()
            finally:
                cc.close()
            done += 1
    return done


def find_lead_by_phone(phone):
    """Return an active (not WIN/LOST) lead id for this phone, or None.

    Searches amoCRM by the 10-digit national core so a contact stored as 8XXX…,
    7XXX…, +7XXX… or with spaces all match the same person (one number)."""
    if not phone:
        return None
    norm = reg.normalize_phone(phone) or phone
    core = re.sub(r"\D", "", norm)[-10:]            # last 10 digits = subscriber
    try:
        q = urllib.parse.quote(core or norm)
        r = _amo("GET", "/api/v4/contacts?query=%s&with=leads" % q)
        if r.status_code != 200:
            return None
        for c in r.json().get("_embedded", {}).get("contacts", []):
            for l in c.get("_embedded", {}).get("leads", []):
                lid = l.get("id")
                lead = _get_lead(lid)
                if lead and lead.get("status_id") not in (ST_WIN, ST_LOST):
                    return lid
    except Exception as exc:
        log.error("amo find_lead_by_phone failed: %s", exc)
    return None


def _ensure_lead(row):
    """Return the lead id for this registration; link by phone if not yet set.
    Does NOT create a new lead (api_lead owns creation) to avoid duplicates."""
    lid = row.get("amocrm_lead_id")
    if lid:
        return lid
    lid = find_lead_by_phone(row.get("phone_e164"))
    if lid:
        _attach_lead(row.get("id"), lid)
    return lid


# --------------------------------------------------------------------------- #
# FORWARD dispatcher
# --------------------------------------------------------------------------- #
def forward(reg_id, event):
    """Push an internal event to amoCRM. Best-effort; never raises to caller."""
    try:
        row = _get_reg(reg_id)
        if not row:
            return
        lead_id = _ensure_lead(row)
        if not lead_id:
            log.info("amo forward(%s,%s): no lead linked, skip", reg_id, event)
            return
        pkg = str(row.get("package") or "")
        fields = {"reg_id (БД)": reg_id}
        if pkg in _TARIFF:
            fields["Тариф"] = _TARIFF[pkg]
            fields["Формат"] = _FORMAT[pkg]
            fields["Браслет"] = _WB[pkg]
        if row.get("city"):
            fields["Город"] = row.get("city")
        if row.get("telegram_username"):
            fields["TG username"] = row.get("telegram_username")
        if row.get("telegram_user_id"):
            fields["TG id"] = row.get("telegram_user_id")
        tags = []
        target = None
        task = None

        if event == "registered":
            target = ST_REG
            # anti-regression: a repeat web/bot submit must NOT drag a lead that's
            # already further down the funnel back to "Не оплачено" / re-spawn the
            # "send SBP" task. Only set those when the deal is still at/below reg.
            _cur = _get_lead(lead_id)
            _cur_rank = _RANK.get((_cur or {}).get("status_id"), 0)
            if _cur_rank <= _RANK[ST_REG]:
                fields["Статус оплаты"] = "Не оплачено"
                task = ("Выслать реквизиты СБП / связаться", 1, 1800)
            if pkg in _TARIFF_TAG:
                tags.append(_TARIFF_TAG[pkg])
            if (row.get("source") or "") == "bot" or row.get("telegram_user_id"):
                tags += ["Telegram-регистрация", "telegram-bot"]
        elif event == "sbp_shown":
            target = ST_DECIDE
            fields["Статус оплаты"] = "Ссылка показана"
            task = ("Проверить оплату по этому лиду", 1, 7200)
        elif event == "i_paid":
            target = ST_CHECKPAY
            fields["Статус оплаты"] = "Заявлена оплата (не подтв.)"
            task = ("ПОДТВЕРДИТЬ ОПЛАТУ по выписке банка/СБП", 1, 1800)
        elif event == "paid":
            target = ST_WIN
            fields["Статус оплаты"] = "Оплачено"
            if row.get("paid_at"):
                fields["Дата оплаты"] = _to_ts(row.get("paid_at"))
        elif event == "ticket_issued":
            fields["Статус билета"] = "Выпущен"
            if row.get("pretix_order_code"):
                fields["№ заказа pretix"] = row.get("pretix_order_code")
        elif event == "checked_in":
            fields["Статус билета"] = "Использован (чек-ин)"
            if row.get("checked_in_at"):
                fields["Дата check-in"] = _to_ts(row.get("checked_in_at"))
            tags.append("checked-in")
        elif event == "no_show":
            fields["Статус билета"] = "No-show"
            tags.append("no-show")
        elif event == "cancelled":
            target = ST_LOST
            fields["Статус оплаты"] = "Отменён"
        elif event == "refunded":
            target = ST_LOST
            fields["Статус билета"] = "Возврат"
            fields["Статус оплаты"] = "Возврат"
            tags.append("refund")
            task = ("Оформить возврат в pretix", 1, 14400)
        else:
            return

        if target is not None:
            if safe_set_stage(lead_id, target):
                _set_forward(reg_id, target)
        patch_lead(lead_id, fields, tags or None)
        if task:
            create_task(lead_id, task[0], task[1], task[2])
    except Exception as exc:
        log.error("amo forward(%s,%s) failed: %s", reg_id, event, exc)


# --------------------------------------------------------------------------- #
# REVERSE: amoCRM webhook -> our actions (called from app route)
# --------------------------------------------------------------------------- #
def _is_echo(row, new_status):
    """True if this status change is our own forward bouncing back."""
    try:
        if row.get("amo_last_status_id") == new_status and row.get("amo_forward_ts"):
            if _now_ts() - int(row["amo_forward_ts"]) < ECHO_WINDOW:
                return True
    except Exception:
        pass
    return False


def _lead_phone(lead_id):
    """Pull the first phone from a lead's linked contact (for reverse matching)."""
    lead = _get_lead(lead_id, withp="contacts")
    if not lead:
        return None
    for c in lead.get("_embedded", {}).get("contacts", []):
        try:
            cr = _amo("GET", "/api/v4/contacts/%d" % int(c.get("id")))
            if cr.status_code == 200:
                for cf in (cr.json().get("custom_fields_values") or []):
                    if cf.get("field_code") == "PHONE" and cf.get("values"):
                        return cf["values"][0].get("value")
        except Exception:
            pass
    return None


def _field_value(lead, name):
    """Read a custom field value (resolves select enums) from a lead dict."""
    fmap = _fields()
    fid = (fmap.get(name) or {}).get("id")
    e2v = {eid: v for n, f in fmap.items() for v, eid in f["enums"].items()}
    for cf in (lead.get("custom_fields_values") or []):
        if cf.get("field_id") == fid:
            vals = cf.get("values") or []
            if vals:
                return e2v.get(vals[0].get("enum_id"), vals[0].get("value"))
    return None


def _flag_no_tariff(lead_id):
    """Create the 'specify tariff' task AT MOST ONCE (guarded by a tag), so a
    no-tariff confirmation can never loop on update_lead webhooks. Returns
    'flagged' (first time) or 'skip'."""
    lead = _get_lead(lead_id)
    tags = [t["name"] for t in (lead or {}).get("_embedded", {}).get("tags", [])]
    if "нужен-тариф" in tags:
        return "skip"
    patch_lead(lead_id, None, ["нужен-тариф"])
    create_task(lead_id, "Оплата отмечена, но «Тариф» не выбран — укажите тариф в карточке, "
                "и билет выпустится автоматически", 1, 7200)
    return "flagged"


def _issue_win(reg_id, lead_id, row):
    """Confirmed payment -> resolve tariff, issue pretix ticket (backend also
    e-mails the PDF), deliver to Telegram, and let the automation move the card to
    «Оплачено». Idempotent. Shared by the drag-to-WIN and «Статус оплаты=Оплачено»
    triggers."""
    # idempotency: never re-issue / re-deliver an already-ticketed registration
    if row.get("status") in ("ticket_issued", "checked_in") and row.get("pretix_order_code"):
        return {"action": "already_done", "reg_id": reg_id}
    pkg = str(row.get("package") or "")
    if pkg not in _TARIFF:
        pkg = _tariff_from_lead(lead_id)
        if pkg:
            try:
                c = reg._conn()
                c.execute("UPDATE registrations SET package=?, price_eur=COALESCE(price_eur,?), updated_at=? WHERE id=?",
                          (pkg, int(pkg), _now_iso(), reg_id))
                c.commit()
                c.close()
            except Exception:
                pass
            row["package"] = pkg
    if pkg not in _TARIFF:
        return {"action": "win_no_package_" + _flag_no_tariff(lead_id), "reg_id": reg_id}
    try:
        reg.set_status(reg_id, "paid", actor="amo:auto", reason="payment confirmed", force=True)
    except Exception:
        pass
    import tickets
    t = tickets.issue_ticket(reg_id)
    if not t:
        create_task(lead_id, "pretix недоступен — билет в очереди, выпустить вручную", 1, 3600)
        return {"action": "win_issue_failed", "reg_id": reg_id}
    delivered = _deliver_via_bot(reg_id)
    emailed = bool(t.get("emailed"))
    if not (delivered or emailed):
        create_task(lead_id, "Билет выпущен (код %s) — отправьте участнику вручную (нет Telegram и e-mail)"
                    % t.get("human_code"), 1, 3600)
    forward(reg_id, "paid")          # Статус оплаты=Оплачено + Дата + автоматика двигает карточку в «Оплачено»
    forward(reg_id, "ticket_issued")  # Статус билета=Выпущен + № заказа pretix
    return {"action": "win_issued", "reg_id": reg_id, "pretix_order": t.get("pretix_order"),
            "human_code": t.get("human_code"), "delivered": delivered, "emailed": emailed}


def reverse_field_paid(lead_id):
    """Lead updated: if «Статус оплаты»=Оплачено and not yet ticketed, the
    automation issues the ticket and moves the card. Idempotent (so our own
    field-writes don't loop)."""
    lead = _get_lead(lead_id)
    if not lead:
        return {"action": "no_lead"}
    if _field_value(lead, "Статус оплаты") != "Оплачено":
        return {"action": "pay_field_not_set"}
    row = _find_by_amo_lead(lead_id) or _find_by_phone(_lead_phone(lead_id))
    if not row:
        return {"action": "no_reg"}
    if row.get("amocrm_lead_id") != int(lead_id):
        _attach_lead(row["id"], lead_id)
    if row.get("status") in ("paid", "ticket_issued", "checked_in") and row.get("pretix_order_code"):
        return {"action": "already_done", "reg_id": row["id"]}
    # No tariff yet -> flag ONCE via tag, never loop creating tasks (the storm fix).
    pkg = str(row.get("package") or "")
    if pkg not in _TARIFF and _tariff_from_lead(lead_id) not in _TARIFF:
        tags = [t["name"] for t in lead.get("_embedded", {}).get("tags", [])]
        if "нужен-тариф" in tags:
            return {"action": "win_no_package_skip"}
        patch_lead(lead_id, None, ["нужен-тариф"])
        create_task(lead_id, "Оплата отмечена, но «Тариф» не выбран — укажите тариф в карточке, "
                    "и билет выпустится автоматически", 1, 7200)
        return {"action": "win_no_package_flagged", "reg_id": row["id"]}
    return _issue_win(row["id"], lead_id, row)


def reverse_status_change(lead_id, new_status, phone=None):
    """Handle a manager-driven stage move. Returns dict(action, ...)."""
    row = (_find_by_amo_lead(lead_id)
           or (_find_by_phone(phone) if phone else None)
           or _find_by_phone(_lead_phone(lead_id)))
    if not row:
        return {"action": "no_reg", "lead_id": lead_id}
    if row.get("amocrm_lead_id") != int(lead_id):
        _attach_lead(row["id"], lead_id)
    if _is_echo(row, new_status):
        return {"action": "echo_skip", "reg_id": row["id"]}
    reg_id = row["id"]

    if new_status == ST_WIN:
        return _issue_win(reg_id, lead_id, row)

    if new_status == ST_LOST:
        try:
            reg.set_status(reg_id, "cancelled", actor="amo:manager", reason="manual LOST in amoCRM", force=True)
        except Exception:
            pass
        flag = bool(row.get("pretix_order_code"))
        if flag:
            create_task(lead_id, "Сделка закрыта, но билет уже выпущен — разобрать возврат", 1, 14400)
        return {"action": "lost", "reg_id": reg_id, "ticket_was_issued": flag}

    return {"action": "noop", "reg_id": reg_id, "status": new_status}


def _deliver_via_bot(reg_id):
    """Best-effort: ask the bot to push the ticket to the buyer's Telegram.
    Returns True only if the bot actually delivered (ok:true)."""
    cfg = _cfg()
    bot_url = cfg.get("BOT_INTERNAL_URL") or ""
    if not bot_url:
        return False
    try:
        import json
        import urllib.request
        from urllib.parse import urlparse
        u = urlparse(bot_url)
        ticket_url = "%s://%s/internal/deliver_ticket" % (u.scheme, u.netloc)
        req = urllib.request.Request(
            ticket_url,
            data=json.dumps({"reg_id": reg_id}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Internal-Token": cfg.get("INTERNAL_TOKEN", "")})
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode("utf-8", "replace")
        return '"ok": true' in body or '"ok":true' in body
    except Exception as exc:
        log.warning("amo reverse: bot deliver failed: %s", exc)
        return False
