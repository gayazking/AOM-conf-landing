"""Registrar desk — on-site registration/check-in panel for up to ~10 staff.

A mobile/tablet web page at /reg where registrars: search an attendee by
name/phone (or scan their ticket QR), see tariff + payment status, and issue
the correct WRISTBAND by tariff, marking attendance. Multi-registrar (each
types their name; actions are logged). Backend endpoints guarded by STAFF_KEY.
"""
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response

import reg

log = logging.getLogger("sato")
bp = Blueprint("desk", __name__)

# package -> (короткое имя тарифа, что выдать, цвет браслета)
WRISTBAND = {
    "500":  ("Онлайн",            "Без браслета · доступ к трансляции", "#7a7f8a"),
    "800":  ("Теория (оффлайн)",  "ЗЕЛЁНЫЙ браслет",                    "#1f9d55"),
    "1000": ("Практика",          "СИНИЙ браслет",                      "#2b6cb0"),
    "1800": ("Полный · 4 дня",    "ЗОЛОТОЙ браслет",                    "#c9a572"),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def init():
    c = reg._conn()
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(registrations)").fetchall()}
        for col, ddl in (("wristband_issued", "INTEGER NOT NULL DEFAULT 0"),
                         ("wristband_type", "TEXT"),
                         ("issued_by", "TEXT"),
                         ("issued_at", "TEXT")):
            if col not in cols:
                c.execute("ALTER TABLE registrations ADD COLUMN %s %s" % (col, ddl))
        c.commit()
    finally:
        c.close()


def _staff_ok():
    cfg = _cfg()
    k = cfg.get("STAFF_KEY") or ""
    return k and request.headers.get("X-Staff-Key", "") == k


def _cfg():
    import app
    return app.load_env()


def _row_view(r):
    pkg = str(r.get("package") or "")
    wb = WRISTBAND.get(pkg, ("—", "уточнить тариф", "#7a7f8a"))
    return {
        "id": r.get("id"), "name": r.get("full_name"), "phone": r.get("phone_e164"),
        "email": r.get("email_lc"), "city": r.get("city"),
        "package": pkg, "price": r.get("price_eur"), "status": r.get("status"),
        "paid": r.get("status") in ("paid", "ticket_issued", "checked_in"),
        "checked_in_at": r.get("checked_in_at"),
        "wristband_issued": bool(r.get("wristband_issued")),
        "wristband_type": r.get("wristband_type"),
        "tariff": wb[0], "wristband": wb[1], "color": wb[2],
        "ticket_id": r.get("ticket_id"),
    }


@bp.route("/api/desk/find", methods=["GET"])
def desk_find():
    if not _staff_ok():
        return jsonify(ok=False, error="forbidden"), 403
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(ok=True, results=[])
    c = reg._conn()
    try:
        digits = "".join(ch for ch in q if ch.isdigit())
        rows = []
        if digits and len(digits) >= 4:
            ph = "%" + digits[-7:] + "%"
            rows = c.execute("SELECT * FROM registrations WHERE phone_e164 LIKE ? ORDER BY created_at DESC LIMIT 20", (ph,)).fetchall()
        if not rows:
            rows = c.execute("SELECT * FROM registrations WHERE full_name LIKE ? OR email_lc LIKE ? ORDER BY created_at DESC LIMIT 20",
                             ("%" + q + "%", "%" + q.lower() + "%")).fetchall()
        return jsonify(ok=True, results=[_row_view(dict(r)) for r in rows])
    finally:
        c.close()


@bp.route("/api/desk/by_ticket", methods=["POST"])
def desk_by_ticket():
    if not _staff_ok():
        return jsonify(ok=False, error="forbidden"), 403
    import tickets
    token = (request.get_json(force=True, silent=True) or {}).get("token", "")
    tid = tickets.verify_ticket(_cfg(), token)
    if not tid:
        return jsonify(ok=True, results=[])
    c = reg._conn()
    try:
        r = c.execute("SELECT * FROM registrations WHERE ticket_id=?", (tid,)).fetchone()
        return jsonify(ok=True, results=[_row_view(dict(r))] if r else [])
    finally:
        c.close()


@bp.route("/api/desk/issue", methods=["POST"])
def desk_issue():
    if not _staff_ok():
        return jsonify(ok=False, error="forbidden"), 403
    d = request.get_json(force=True, silent=True) or {}
    rid = d.get("id")
    registrar = (d.get("registrar") or "").strip()[:60]
    if not rid:
        return jsonify(ok=False, error="no_id"), 400
    c = reg._conn()
    try:
        r = c.execute("SELECT * FROM registrations WHERE id=?", (rid,)).fetchone()
        if not r:
            return jsonify(ok=False, error="not_found"), 404
        r = dict(r)
        pkg = str(r.get("package") or "")
        wb = WRISTBAND.get(pkg, ("—", "уточнить", "#888"))
        c.execute(
            "UPDATE registrations SET wristband_issued=1, wristband_type=?, issued_by=?, issued_at=?, "
            "checked_in_at=COALESCE(checked_in_at,?), checked_in_by=COALESCE(checked_in_by,?), "
            "status=CASE WHEN status IN ('checked_in') THEN status ELSE 'checked_in' END, updated_at=? WHERE id=?",
            (wb[0], registrar, _now(), _now(), registrar, _now(), rid),
        )
        c.execute("INSERT INTO scan_log (ticket_id, ts, result, gate, staff) VALUES (?,?,?,?,?)",
                  (r.get("ticket_id"), _now(), "wristband:" + wb[0], "desk", registrar))
        c.commit()
        return jsonify(ok=True, wristband=wb[1], tariff=wb[0], color=wb[2], name=r.get("full_name"))
    finally:
        c.close()


PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Регистрация · Казань—Токио</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--gold:#c9a572;--line:#262b36;--muted:#9aa3b2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e7e9ee;font-family:-apple-system,Segoe UI,Roboto,Arial}
.wrap{max-width:620px;margin:0 auto;padding:14px}
h2{color:var(--gold);margin:6px 0 12px}
input,button{font-size:16px;padding:11px;border-radius:9px;border:1px solid var(--line);background:#0e1219;color:#fff;width:100%}
button{background:var(--gold);color:#10131a;font-weight:700;border:0;cursor:pointer;margin-top:8px}
button.sec{background:#222733;color:#cdd3dd}
.row{display:flex;gap:8px}.row>*{flex:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-top:12px}
.name{font-size:20px;font-weight:700}.muted{color:var(--muted);font-size:14px}
.badge{display:inline-block;padding:4px 10px;border-radius:20px;font-weight:700;font-size:13px;margin-top:6px}
.paid{background:#0f7a32}.notpaid{background:#a01b1b}
.wb{margin-top:10px;padding:12px;border-radius:10px;font-weight:800;font-size:18px;text-align:center;color:#10131a}
.done{background:#0f7a32;color:#fff}
#setup{margin-bottom:10px}
small{color:var(--muted)}
</style></head><body><div class=wrap>
<h2>Регистрация участников</h2>
<div id=setup class=card>
 <small>Ключ персонала и ваше имя (один раз):</small>
 <input id=key type=password placeholder="Ключ персонала">
 <input id=reg placeholder="Ваше имя (регистратор)" style="margin-top:8px">
 <button onclick=save()>Войти</button>
</div>
<div id=app style=display:none>
 <div class=row><input id=q placeholder="Поиск: ФИО или телефон" oninput=debounced()>
  <button class=sec style=flex:0;min-width:120px onclick=scan()>📷 QR</button></div>
 <div id=reader></div>
 <div id=results></div>
</div>
</div>
<script src="https://unpkg.com/html5-qrcode"></script>
<script>
function save(){let k=key.value.trim(),r=reg.value.trim();if(!k||!r){alert('Заполните ключ и имя');return}
 localStorage.deskKey=k;localStorage.deskReg=r;setup.style.display='none';app.style.display='block';}
if(localStorage.deskKey&&localStorage.deskReg){setup.style.display='none';app.style.display='block'}
let H={'X-Staff-Key':localStorage.deskKey||'','Content-Type':'application/json'};
function H2(){return {'X-Staff-Key':localStorage.deskKey||'','Content-Type':'application/json'}}
let t;function debounced(){clearTimeout(t);t=setTimeout(find,350)}
async function find(){let q=document.getElementById('q').value.trim();if(q.length<2){results.innerHTML='';return}
 let r=await fetch('/api/desk/find?q='+encodeURIComponent(q),{headers:H2()});render(await r.json());}
function render(d){if(!d.ok){results.innerHTML='<div class=card>Ошибка доступа — проверьте ключ</div>';return}
 if(!d.results.length){results.innerHTML='<div class=card>Не найдено</div>';return}
 results.innerHTML=d.results.map(card).join('');}
function card(x){let paid=x.paid?'<span class="badge paid">ОПЛАЧЕНО</span>':'<span class="badge notpaid">НЕ ОПЛАЧЕНО</span>';
 let done=x.wristband_issued?('<div class="wb done">✅ Браслет выдан: '+x.tariff+'</div>'):
   ('<div class=wb style="background:'+x.color+'">'+x.wristband+'</div><button onclick="issue(\\''+x.id+'\\')">Выдать браслет / отметить вход</button>');
 return '<div class=card id=c_'+x.id+'><div class=name>'+(x.name||'—')+'</div>'+
  '<div class=muted>'+(x.phone||'')+' · '+(x.city||'')+'</div>'+
  '<div>Тариф: <b>'+x.tariff+'</b> '+(x.price?('('+x.price+'€)'):'')+'</div>'+paid+
  (x.checked_in_at?'<div class=muted>Вход отмечен ранее</div>':'')+done+'</div>';}
async function issue(id){let r=await fetch('/api/desk/issue',{method:'POST',headers:H2(),body:JSON.stringify({id:id,registrar:localStorage.deskReg})});
 let d=await r.json();if(d.ok){document.getElementById('c_'+id).querySelector('.wb,button')&&find();alert('✅ '+(d.name||'')+'\\nВыдать: '+d.wristband);}else alert('Ошибка: '+(d.error||''));}
function scan(){if(document.getElementById('reader').dataset.on){return}reader.dataset.on=1;
 let h=new Html5Qrcode('reader');h.start({facingMode:'environment'},{fps:10,qrbox:240},async t=>{h.stop();reader.dataset.on='';
  let r=await fetch('/api/desk/by_ticket',{method:'POST',headers:H2(),body:JSON.stringify({token:t})});render(await r.json());}).catch(e=>{reader.dataset.on='';alert('Камера: '+e)});}
</script></body></html>"""


@bp.route("/reg", methods=["GET"])
def reg_page():
    return Response(PAGE, mimetype="text/html; charset=utf-8")


def register(flask_app):
    init()
    flask_app.register_blueprint(bp)
    log.info("desk blueprint registered")
