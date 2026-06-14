"""QR e-tickets + check-in + bot consent — backend module (Flask blueprint).

Issued ONLY after a manager marks the registration paid. Token = HMAC-signed
(itsdangerous), single-use enforced by DB state. QR (segno) + branded PDF
(reportlab), rendered to BytesIO (no temp files). Registered on the app via
one line: `import tickets; tickets.register(app)`.

Env (in /etc/sato/amo.env, read via app.load_env): TICKET_SECRET (fallback
INTERNAL_TOKEN), STAFF_KEY, EVENT_DATES, VENUE.
"""
import base64
import io
import logging
import secrets
import time
from datetime import datetime, timezone, timedelta

import segno
from flask import Blueprint, request, jsonify, Response
from itsdangerous import URLSafeSerializer, BadSignature
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os as _os
_FONT, _FONTB = "Helvetica", "Helvetica-Bold"
try:
    _dv="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    _db="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if _os.path.exists(_dv):
        pdfmetrics.registerFont(TTFont("DejaVu", _dv)); _FONT="DejaVu"
        if _os.path.exists(_db):
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", _db)); _FONTB="DejaVu-Bold"
        else: _FONTB="DejaVu"
except Exception:
    pass

import reg
import pretix_link

log = logging.getLogger("sato")
bp = Blueprint("tickets", __name__)
SALT = "sato-ticket-2026"
_HUMAN = "ABCDEFGHJKLMNPQRTUVWXYZ23456789"
_PKG_LABEL = {"500": "Теория · онлайн", "800": "Теория · оффлайн", "1000": "Практика · оффлайн", "1800": "Полный пакет (4 дня)"}


def _cfg():
    import app
    return app.load_env()


def _secret(cfg):
    return cfg.get("TICKET_SECRET") or cfg.get("INTERNAL_TOKEN") or "sato-fallback-secret"


def _ser(cfg):
    return URLSafeSerializer(_secret(cfg), salt=SALT)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _human_code(n=6):
    return "".join(secrets.choice(_HUMAN) for _ in range(n))


# --------------------------------------------------------------------------- #
# Ticket generation
# --------------------------------------------------------------------------- #
def _render_qr_png(token):
    buf = io.BytesIO()
    segno.make_qr(token, error="h").save(buf, kind="png", scale=6, border=4)
    return buf.getvalue()


def _render_pdf(cfg, full_name, package_label, price_eur, human_code, qr_png):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    gold = (0.788, 0.647, 0.447)
    c.setFillColorRGB(0.055, 0.067, 0.082); c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setStrokeColorRGB(*gold); c.setLineWidth(1.2); c.rect(12*mm, 12*mm, W-24*mm, H-24*mm, fill=0, stroke=1)
    c.setFillColorRGB(*gold); c.setFont(_FONTB, 30)
    c.drawCentredString(W/2, H-52*mm, "КАЗАНЬ — ТОКИО")
    c.setFillColorRGB(0.9, 0.9, 0.93); c.setFont(_FONT, 13)
    c.drawCentredString(W/2, H-63*mm, "Международный стоматологический саммит 2026")
    c.setFillColorRGB(0.68, 0.71, 0.77); c.setFont(_FONT, 11)
    c.drawCentredString(W/2, H-71*mm, "Профессор Садао Сато и его команда")
    c.setFillColorRGB(*gold); c.setFont(_FONT, 13); c.drawCentredString(W/2, H-79*mm, "ЭЛЕКТРОННЫЙ БИЛЕТ")
    qs = 70*mm; qx = (W-qs)/2; qy = H/2 - 18*mm
    c.setFillColorRGB(1, 1, 1); c.roundRect(qx-6*mm, qy-6*mm, qs+12*mm, qs+12*mm, 4*mm, fill=1, stroke=0)
    c.drawImage(ImageReader(io.BytesIO(qr_png)), qx, qy, qs, qs, mask="auto")
    c.setFillColorRGB(1, 1, 1); c.setFont(_FONTB, 18)
    c.drawCentredString(W/2, qy-18*mm, full_name or "Участник")
    c.setFillColorRGB(*gold); c.setFont(_FONT, 13)
    pl = _PKG_LABEL.get(str(package_label), package_label or "Участие")
    if price_eur:
        pl = "%s — %s €" % (pl, price_eur)
    c.drawCentredString(W/2, qy-27*mm, pl)
    c.setFillColorRGB(0.72, 0.75, 0.8); c.setFont(_FONT, 11)
    y = qy-40*mm
    lines = []
    if cfg.get("EVENT_DATES"): lines.append("Даты: " + cfg.get("EVENT_DATES"))
    if cfg.get("VENUE"): lines.append("Место: " + cfg.get("VENUE"))
    lines.append("Код билета: " + human_code)
    if str(package_label) == "500":
        lines.append("Онлайн-доступ. QR открывает трансляцию.")
    else:
        lines.append("Вход однократный. Предъявите QR на входе.")
    if cfg.get("SUPPORT_PHONE"): lines.append("Поддержка: " + cfg.get("SUPPORT_PHONE"))
    for ln in lines:
        c.drawCentredString(W/2, y, ln); y -= 7*mm
    c.setFillColorRGB(0.5, 0.53, 0.6); c.setFont(_FONT, 9)
    c.drawCentredString(W/2, 20*mm, "sadaosato.pro")
    c.showPage(); c.save(); return buf.getvalue()


def _t(s):
    """reportlab core fonts are latin-only; transliterate Cyrillic for the PDF
    text lines (the QR + DB keep full Cyrillic). Keeps the PDF readable without
    bundling a TTF font."""
    table = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
        "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
        "х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
        "—":"-","«":'"',"»":'"',"№":"No",
    }
    out = []
    for ch in str(s):
        low = ch.lower()
        if low in table:
            r = table[low]
            out.append(r.upper() if ch.isupper() and r else r)
        else:
            out.append(ch)
    return "".join(out)


def _fmt_msk(iso):
    """ISO/UTC -> 'дд.мм.гггг ЧЧ:ММ МСК' for ticket captions."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M") + " МСК"
    except Exception:
        return ""


def _email_ticket(cfg, to_email, pdf_bytes, png_bytes, human_code, name, package_label):
    """E-mail the branded PDF ticket (QR embedded) to the buyer. Returns bool.
    SMTP via env: SMTP_HOST, SMTP_PORT (587 STARTTLS / 465 SSL), SMTP_USER,
    SMTP_PASS, SMTP_FROM. No-op (False) until SMTP_HOST is set."""
    host = (cfg.get("SMTP_HOST") or "").strip()
    if not host or not to_email:
        return False
    import smtplib
    import ssl
    from email.message import EmailMessage
    try:
        port = int(cfg.get("SMTP_PORT") or 587)
    except Exception:
        port = 587
    user = cfg.get("SMTP_USER") or ""
    pw = cfg.get("SMTP_PASS") or ""
    sender = cfg.get("SMTP_FROM") or user or "no-reply@sadaosato.pro"
    pl = _PKG_LABEL.get(str(package_label), package_label or "Участие")
    msg = EmailMessage()
    msg["Subject"] = "Ваш билет — саммит «Казань — Токио 2026»"
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(
        "Здравствуйте, %s!\n\nВаша оплата подтверждена. Билет на международный стоматологический "
        "саммит «Казань — Токио 2026» — во вложении (PDF с QR-кодом).\n\nТариф: %s\nКод билета: %s\n\n"
        "Предъявите QR на входе (вход однократный).\nПоддержка: %s\n\nsadaosato.pro"
        % (name or "участник", pl, human_code, cfg.get("SUPPORT_PHONE") or "")
    )
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="ticket-kazan-tokyo.pdf")
    if png_bytes:
        msg.add_attachment(png_bytes, maintype="image", subtype="png", filename="ticket-qr.png")
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except Exception:
                    pass
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        log.info("ticket emailed to %s (code %s)", to_email, human_code)
        return True
    except Exception as exc:
        log.error("ticket email to %s failed: %s", to_email, exc)
        return False


def issue_ticket(reg_id):
    """Mint + persist a ticket for a registration. Sets status ticket_issued.

    Primary path: create (or reuse) a PAID order in pretix; the QR payload is
    the pretix position secret, so registrars validate it with pretixSCAN. If
    pretix is unreachable, fall back to the legacy HMAC token + local single-use.
    Returns dict(ticket_id, human_code, pretix_order, png_b64, pdf_b64) or None.
    """
    cfg = _cfg()
    c = reg._conn()
    try:
        row = c.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
    finally:
        c.close()

    qr_payload = None
    human_code = None
    ticket_id = row.get("ticket_id")
    pretix_code = None

    # --- primary: pretix paid order, QR = position secret ---
    try:
        porder = pretix_link.create_paid_order(row)
    except Exception as exc:
        log.error("pretix create_paid_order error: %s", exc)
        porder = None
    if porder and porder.get("secret"):
        qr_payload = porder["secret"]
        pretix_code = porder.get("code")
        human_code = pretix_code
        ticket_id = ticket_id or pretix_code
        row["pretix_order_code"] = pretix_code
        row["pretix_secret"] = qr_payload
        c = reg._conn()
        try:
            c.execute("UPDATE registrations SET ticket_id=COALESCE(ticket_id,?), ticket_issued_at=COALESCE(ticket_issued_at,?), updated_at=? WHERE id=?",
                      (ticket_id, _now(), _now(), reg_id))
            c.commit()
        finally:
            c.close()

    # --- fallback: legacy HMAC token + local DB single-use ---
    if qr_payload is None:
        if not ticket_id:
            ticket_id = secrets.token_urlsafe(12)
            human_code = _human_code()
            c = reg._conn()
            try:
                c.execute(
                    "UPDATE registrations SET ticket_id=?, ticket_issued_at=?, qr_delivered_channels=COALESCE(qr_delivered_channels,''), updated_at=? WHERE id=?",
                    (ticket_id, _now(), _now(), reg_id),
                )
                c.commit()
            finally:
                c.close()
        qr_payload = _ser(cfg).dumps({"tid": ticket_id})
        if human_code is None:
            human_code = ticket_id[:6].upper()

    # online tariff (500): QR opens the broadcast stream instead of a door secret
    # (pretix order/secret stay persisted for records); falls back to the secret
    # until BROADCAST_URL is configured.
    if str(row.get("package") or "") == "500" and cfg.get("BROADCAST_URL"):
        qr_payload = cfg.get("BROADCAST_URL")

    png = _render_qr_png(qr_payload)
    pdf = _render_pdf(cfg, row.get("full_name"), row.get("package"), row.get("price_eur"), human_code, png)

    # e-mail the PDF ticket (QR embedded) to the buyer if we have an address + SMTP
    emailed = False
    to_email = (row.get("email_lc") or "").strip()
    if to_email and "@" in to_email:
        emailed = _email_ticket(cfg, to_email, pdf, png, human_code, row.get("full_name"), row.get("package"))
        if emailed:
            try:
                c = reg._conn()
                c.execute("UPDATE registrations SET qr_delivered_channels="
                          "COALESCE(NULLIF(qr_delivered_channels,''),'')||',email', updated_at=? WHERE id=?",
                          (_now(), reg_id))
                c.commit()
                c.close()
            except Exception:
                pass

    # status -> ticket_issued (best effort; allow from paid)
    try:
        reg.set_status(reg_id, "ticket_issued", actor="manager", reason="ticket issued")
    except Exception:
        pass
    return {
        "ticket_id": ticket_id, "human_code": human_code, "pretix_order": pretix_code, "emailed": emailed,
        "name": row.get("full_name"),
        "tariff_label": _PKG_LABEL.get(str(row.get("package")), row.get("package")),
        "paid_at_h": _fmt_msk(row.get("paid_at") or _now()),
        "png_b64": base64.b64encode(png).decode(),
        "pdf_b64": base64.b64encode(pdf).decode(),
    }


def verify_ticket(cfg, token):
    try:
        data = _ser(cfg).loads(token)
        return data.get("tid")
    except BadSignature:
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _loopback_ok(cfg):
    tok = cfg.get("INTERNAL_TOKEN") or ""
    return tok and request.headers.get("X-Internal-Token", "") == tok


@bp.route("/api/issue_ticket", methods=["POST"])
def api_issue_ticket():
    cfg = _cfg()
    if not _loopback_ok(cfg):
        return jsonify(ok=False, error="unauthorized"), 401
    d = request.get_json(force=True, silent=True) or {}
    reg_id = d.get("reg_id")
    if not reg_id:
        return jsonify(ok=False, error="no_reg_id"), 400
    # ensure paid first (manager flow marks paid then issues)
    try:
        c = reg._conn()
        r = c.execute("SELECT status FROM registrations WHERE id=?", (reg_id,)).fetchone()
        c.close()
        if not r:
            return jsonify(ok=False, error="not_found"), 404
        if r["status"] in ("lead", "registered"):
            reg.set_status(reg_id, "paid", actor="manager", reason="manual payment confirmed", force=True)
    except Exception as exc:
        log.error("issue_ticket pre-paid failed: %s", exc)
    t = issue_ticket(reg_id)
    if not t:
        return jsonify(ok=False, error="issue_failed"), 500
    # reflect to amoCRM: WIN + ticket fields
    try:
        import amo_sync
        amo_sync.forward(reg_id, "paid")
        amo_sync.forward(reg_id, "ticket_issued")
    except Exception as exc:
        log.error("amo forward (paid/ticket) failed: %s", exc)
    return jsonify(ok=True, **t)


@bp.route("/api/kassa/webhook", methods=["POST"])
def api_kassa_webhook():
    """DORMANT reverse webhook for a future online cash register (онлайн-касса).

    Disabled until KASSA_WEBHOOK_SECRET is set in the env (returns 403). When the
    касса is connected it POSTs a paid notification here; we mark the registration
    paid and issue the ticket (which creates the PAID pretix order). Delivery to
    the buyer's Telegram is best-effort via the bot internal URL.
    """
    cfg = _cfg()
    secret = cfg.get("KASSA_WEBHOOK_SECRET") or ""
    if not secret:
        return jsonify(ok=False, error="disabled"), 403
    d = request.get_json(force=True, silent=True) or {}
    sig = request.headers.get("X-Kassa-Secret", "") or d.get("secret", "")
    if sig != secret:
        return jsonify(ok=False, error="forbidden"), 403
    if str(d.get("status") or "paid").lower() not in ("paid", "succeeded", "success", "confirmed"):
        return jsonify(ok=True, ignored=True)
    ident = str(d.get("reg_id") or d.get("phone") or d.get("email") or d.get("order") or "").strip()
    if not ident:
        return jsonify(ok=False, error="no_identifier"), 400
    c = reg._conn()
    try:
        row = c.execute(
            "SELECT id FROM registrations WHERE id=? OR phone_e164=? OR email_lc=? OR pretix_order_code=?",
            (ident, reg.normalize_phone(ident), ident.lower(), ident)).fetchone()
    finally:
        c.close()
    if not row:
        return jsonify(ok=False, error="reg_not_found"), 404
    reg_id = row["id"]
    try:
        reg.set_status(reg_id, "paid", actor="kassa", reason="online kassa webhook", force=True)
    except Exception:
        pass
    t = issue_ticket(reg_id)
    if not t:
        return jsonify(ok=False, error="issue_failed"), 500
    delivered = False
    bot_url = cfg.get("BOT_INTERNAL_URL") or ""
    if bot_url:
        try:
            import json as _json
            import urllib.request
            from urllib.parse import urlparse
            u = urlparse(bot_url)
            ticket_url = "%s://%s/internal/deliver_ticket" % (u.scheme, u.netloc)
            req = urllib.request.Request(
                ticket_url,
                data=_json.dumps({"reg_id": reg_id}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Internal-Token": cfg.get("INTERNAL_TOKEN", "")})
            resp = urllib.request.urlopen(req, timeout=15)
            delivered = '"ok": true' in resp.read().decode("utf-8", "replace")
        except Exception as exc:
            log.warning("kassa webhook bot deliver failed: %s", exc)
    return jsonify(ok=True, pretix_order=t.get("pretix_order"), human_code=t.get("human_code"), delivered=delivered)


@bp.route("/api/checkin", methods=["POST"])
def api_checkin():
    cfg = _cfg()
    staff = cfg.get("STAFF_KEY") or ""
    if not staff or request.headers.get("X-Staff-Key", "") != staff:
        return jsonify(result="forbidden"), 403
    d = request.get_json(force=True, silent=True) or {}
    token = (d.get("token") or "").strip()
    gate = d.get("gate") or "main"
    staff_name = d.get("staff") or ""

    # --- primary: pretix redeem (new QRs carry the pretix position secret) ---
    try:
        res, info = pretix_link.redeem(token)
    except Exception as exc:
        log.error("pretix redeem error: %s", exc)
        res, info = "error", {}
    if res in ("ok", "already", "unpaid"):
        name = (info or {}).get("name")
        _scan(token[:24], {"ok": "ok", "already": "duplicate", "unpaid": "unpaid"}[res], gate, staff_name)
        if res == "ok":
            # reflect check-in locally + to amoCRM (pretix is source of truth for the scan)
            try:
                import amo_sync
                c = reg._conn()
                rr = c.execute("SELECT id FROM registrations WHERE pretix_secret=?", (token,)).fetchone()
                if rr:
                    c.execute("UPDATE registrations SET checked_in_at=COALESCE(checked_in_at,?), checked_in_by=?, "
                              "check_in_gate=?, status='checked_in', updated_at=? WHERE id=?",
                              (_now(), staff_name, gate, _now(), rr["id"]))
                    c.commit()
                c.close()
                if rr:
                    amo_sync.forward(rr["id"], "checked_in")
            except Exception as exc:
                log.error("checkin reflect failed: %s", exc)
            return jsonify(result="ok", name=name)
        if res == "already":
            return jsonify(result="duplicate", name=name)
        return jsonify(result="unpaid", name=name)

    # --- fallback: legacy HMAC token + local DB single-use ---
    tid = verify_ticket(cfg, token)
    if not tid:
        _scan(None, "invalid", gate, staff_name)
        return jsonify(result="invalid")
    c = reg._conn()
    try:
        row = c.execute("SELECT * FROM registrations WHERE ticket_id=?", (tid,)).fetchone()
        if not row:
            _scan(tid, "invalid", gate, staff_name)
            return jsonify(result="invalid")
        row = dict(row)
        if row.get("checked_in_at"):
            _scan(tid, "duplicate", gate, staff_name)
            return jsonify(result="duplicate", name=row.get("full_name"), package=row.get("package"),
                           checked_in_at=row.get("checked_in_at"))
        c.execute("UPDATE registrations SET checked_in_at=?, checked_in_by=?, check_in_gate=?, status='checked_in', updated_at=? WHERE ticket_id=? AND checked_in_at IS NULL",
                  (_now(), staff_name, gate, _now(), tid))
        changed = c.total_changes
        c.commit()
        if changed:
            c.execute("INSERT INTO status_history (registration_id, from_status, to_status, ts, actor, reason) VALUES (?,?,?,?,?,?)",
                      (row["id"], row.get("status"), "checked_in", _now(), staff_name or "door", "check-in"))
            c.commit()
            _scan(tid, "ok", gate, staff_name)
            try:
                import amo_sync
                amo_sync.forward(row["id"], "checked_in")
            except Exception as exc:
                log.error("amo forward(checked_in) failed: %s", exc)
            return jsonify(result="ok", name=row.get("full_name"), package=row.get("package"))
        else:
            _scan(tid, "duplicate", gate, staff_name)
            return jsonify(result="duplicate", name=row.get("full_name"), package=row.get("package"))
    finally:
        c.close()


def _scan(tid, result, gate, staff):
    try:
        c = reg._conn()
        c.execute("INSERT INTO scan_log (ticket_id, ts, result, gate, staff) VALUES (?,?,?,?,?)",
                  (tid, _now(), result, gate, staff))
        c.commit(); c.close()
    except Exception:
        pass


@bp.route("/api/consent", methods=["POST"])
def api_consent():
    cfg = _cfg()
    if not _loopback_ok(cfg):
        return jsonify(ok=False, error="unauthorized"), 401
    d = request.get_json(force=True, silent=True) or {}
    docs = d.get("docs") or []
    ver = d.get("doc_version") or "Редакция №1 от 01.06.2026"
    action = d.get("action") or "granted"
    tg = d.get("telegram_user_id")
    phone = d.get("phone")
    n = 0
    for dt in docs:
        try:
            reg.log_consent(d.get("registration_id"), dt, ver, action=action,
                            subject_phone=phone, subject_tg=tg, channel="bot",
                            user_agent="telegram")
            n += 1
        except Exception as exc:
            log.error("consent log failed: %s", exc)
    return jsonify(ok=True, logged=n)


@bp.route("/api/reg/find", methods=["GET"])
def api_reg_find():
    cfg = _cfg()
    if not _loopback_ok(cfg):
        return jsonify(ok=False, error="unauthorized"), 401
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(ok=False, error="no_query"), 400
    c = reg._conn()
    try:
        row = None
        if q.startswith("@"):
            row = c.execute("SELECT * FROM registrations WHERE telegram_username=? ORDER BY created_at DESC LIMIT 1", (q[1:],)).fetchone()
        if row is None and q.isdigit() and len(q) >= 9:
            ph = reg.normalize_phone(q)
            row = c.execute("SELECT * FROM registrations WHERE phone_e164=? ORDER BY created_at DESC LIMIT 1", (ph,)).fetchone()
        if row is None and q.isdigit():
            row = c.execute("SELECT * FROM registrations WHERE telegram_user_id=? ORDER BY created_at DESC LIMIT 1", (int(q),)).fetchone()
        if row is None:
            row = c.execute("SELECT * FROM registrations WHERE id=? OR phone_e164=?", (q, reg.normalize_phone(q))).fetchone()
        if not row:
            return jsonify(ok=True, found=False)
        row = dict(row)
        return jsonify(ok=True, found=True, reg={k: row.get(k) for k in
                       ("id","full_name","phone_e164","email_lc","city","format","package","price_eur","status","telegram_user_id","ticket_id")})
    finally:
        c.close()


CHECKIN_HTML = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Check-in</title>
<style>body{margin:0;background:#0f1115;color:#e7e9ee;font-family:-apple-system,Segoe UI,Roboto,Arial}
#r{font-size:26px;font-weight:700;padding:24px;text-align:center;min-height:90px}
.ok{background:#0f7a32}.dup{background:#9a7d1a}.bad{background:#a01b1b}
#reader{max-width:480px;margin:0 auto}input,button{font-size:16px;padding:10px;margin:6px}
.wrap{max-width:520px;margin:0 auto;padding:16px}</style></head><body>
<div class=wrap><h2>Чек-ин · Казань—Токио</h2>
<div id=setup><p>Введите ключ персонала:</p><input id=key type=password placeholder="STAFF_KEY">
<input id=gate placeholder="Вход (напр. main)" value="main"><button onclick=start()>Старт сканера</button></div>
<div id=r></div><div id=reader></div></div>
<script src="https://unpkg.com/html5-qrcode"></script><script>
let last=0;
function show(cls,txt){let r=document.getElementById('r');r.className=cls;r.textContent=txt;}
async function onScan(t){let now=Date.now();if(now-last<2500)return;last=now;
 try{let res=await fetch('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json','X-Staff-Key':localStorage.satoKey},
  body:JSON.stringify({token:t,gate:localStorage.satoGate||'main'})});let d=await res.json();
  if(d.result==='ok')show('ok','✅ ВХОД — '+(d.name||'')+' · '+(d.package||''));
  else if(d.result==='duplicate')show('dup','⚠️ УЖЕ ВХОДИЛ — '+(d.name||''));
  else if(d.result==='forbidden')show('bad','⛔ Неверный ключ персонала');
  else show('bad','❌ Билет недействителен');
 }catch(e){show('bad','Ошибка сети');}}
function start(){localStorage.satoKey=document.getElementById('key').value;localStorage.satoGate=document.getElementById('gate').value;
 document.getElementById('setup').style.display='none';
 let h=new Html5Qrcode('reader');h.start({facingMode:'environment'},{fps:10,qrbox:250},onScan).catch(e=>show('bad','Камера: '+e));}
</script></body></html>"""


@bp.route("/checkin", methods=["GET"])
def checkin_page():
    return Response(CHECKIN_HTML, mimetype="text/html; charset=utf-8")


def register(flask_app):
    try:
        pretix_link.init()
    except Exception as exc:
        log.error("pretix_link.init failed: %s", exc)
    flask_app.register_blueprint(bp)
    log.info("tickets blueprint registered")
