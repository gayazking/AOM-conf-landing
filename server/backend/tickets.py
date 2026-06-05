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
from datetime import datetime, timezone

import segno
from flask import Blueprint, request, jsonify, Response
from itsdangerous import URLSafeSerializer, BadSignature
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

import reg

log = logging.getLogger("sato")
bp = Blueprint("tickets", __name__)
SALT = "sato-ticket-2026"
_HUMAN = "ABCDEFGHJKLMNPQRTUVWXYZ23456789"


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
    c.setFillColorRGB(0.06, 0.067, 0.082)
    c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColorRGB(*gold)
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(W / 2, H - 60 * mm, "KAZAN — TOKYO")
    c.setFillColorRGB(0.9, 0.9, 0.93)
    c.setFont("Helvetica", 12)
    c.drawCentredString(W / 2, H - 70 * mm, "Mezhdunarodnyy stomatologicheskiy sammit 2026")
    c.drawCentredString(W / 2, H - 78 * mm, "Prof. Sadao Sato")
    # QR
    qr = ImageReader(io.BytesIO(qr_png))
    qs = 70 * mm
    c.drawImage(qr, (W - qs) / 2, H / 2 - 10 * mm, qs, qs, mask="auto")
    # attendee + package
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(W / 2, H / 2 - 25 * mm, _t(full_name or "Uchastnik"))
    c.setFillColorRGB(*gold)
    c.setFont("Helvetica", 13)
    pl = package_label or ""
    if price_eur:
        pl = "%s — %s EUR" % (pl, price_eur)
    c.drawCentredString(W / 2, H / 2 - 33 * mm, _t(pl))
    c.setFillColorRGB(0.68, 0.71, 0.77)
    c.setFont("Helvetica", 11)
    y = H / 2 - 45 * mm
    for line in (("Daty: " + cfg.get("EVENT_DATES", "")) if cfg.get("EVENT_DATES") else None,
                 ("Mesto: " + cfg.get("VENUE", "")) if cfg.get("VENUE") else None,
                 "Kod bileta: " + human_code,
                 "Vkhod odnokratnyy. Pred'yavite QR na vkhode."):
        if line:
            c.drawCentredString(W / 2, y, _t(line))
            y -= 7 * mm
    c.showPage()
    c.save()
    return buf.getvalue()


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


def issue_ticket(reg_id):
    """Mint + persist a ticket for a registration. Sets status ticket_issued.
    Returns dict(ticket_id, human_code, png_b64, pdf_b64) or None."""
    cfg = _cfg()
    c = reg._conn()
    try:
        row = c.execute("SELECT * FROM registrations WHERE id=?", (reg_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        ticket_id = row.get("ticket_id")
        human_code = None
        if not ticket_id:
            ticket_id = secrets.token_urlsafe(12)
            human_code = _human_code()
            c.execute(
                "UPDATE registrations SET ticket_id=?, ticket_issued_at=?, qr_delivered_channels=COALESCE(qr_delivered_channels,''), updated_at=? WHERE id=?",
                (ticket_id, _now(), _now(), reg_id),
            )
            # store human code in message-ish? keep in a column-free way: reuse badge_name? No.
            c.execute("UPDATE registrations SET check_in_gate=COALESCE(check_in_gate,?) WHERE id=?", (None, reg_id))
            c.commit()
        token = _ser(cfg).dumps({"tid": ticket_id})
        png = _render_qr_png(token)
        # human_code stable: derive from ticket_id if not freshly minted
        if human_code is None:
            human_code = ticket_id[:6].upper()
        pdf = _render_pdf(cfg, row.get("full_name"), row.get("package"), row.get("price_eur"), human_code, png)
    finally:
        c.close()
    # status -> ticket_issued (best effort; allow from paid)
    try:
        reg.set_status(reg_id, "ticket_issued", actor="manager", reason="ticket issued")
    except Exception:
        pass
    return {
        "ticket_id": ticket_id, "human_code": human_code,
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
    return jsonify(ok=True, **t)


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
                       ("id","full_name","phone_e164","email_lc","package","price_eur","status","telegram_user_id","ticket_id")})
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
    flask_app.register_blueprint(bp)
    log.info("tickets blueprint registered")
