"""QR scan tracking — poster QR codes redirect through /r/<code>.

Each poster variant (1..5) has 3 codes (tg/site/max). The QR encodes
https://sadaosato.pro/r/<code>; this endpoint logs the scan (ip, ua, referer,
lang, device, variant, channel, ts) into qr_scans, then 302-redirects to the
real destination. Analytics live in registrations.sqlite3 (Datasette/GUI).
"""
import logging
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, request, redirect, Response, jsonify, session

import reg

log = logging.getLogger("sato")
bp = Blueprint("qrtrack", __name__)

# Poster variant -> human label (marketing report).
VARIANTS = {
    "1": "Лопухова Н.Б.",
    "2": "Сойхер М.Г.",
    "3": "Столбовая И.В.",
    "4": "Насуев Г.",
    "5": "Сато С.",
    "6": "Наш канал",
    "7": "Фазлиев А.",
    "8": "Рязяпов М.",
    "9": "Халиуллина Г.Р.",
}
# code suffix -> channel key/label. 'b' (bot) and legacy 't' both = Telegram.
CHAN_LABEL = {"tg": "Telegram", "site": "Сайт", "max": "MAX"}
_SUFFIX_CHAN = {"b": "tg", "t": "tg", "s": "site", "m": "max"}
_CODE_RE = re.compile(r"^p(\d+)([a-z])$")


def _parse(code):
    """Derive (variant, channel) from a code like 'p6b' — robust to NULL rows."""
    m = _CODE_RE.match((code or "").strip().lower())
    if not m:
        return None, None
    return m.group(1), _SUFFIX_CHAN.get(m.group(2))

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
    """Create 9 variants x 3 channels = 27 codes. Idempotent.

    Telegram suffix is 'b' (the live codes on the QR/flyers); legacy 't' rows,
    if present, are left untouched and still resolve to Telegram in reports.
    """
    init()
    c = reg._conn()
    try:
        for v in range(1, 10):
            site_dest = (SITE + "?utm_source=poster&utm_medium=qr&utm_campaign=afisha-v%d&utm_content=site" % v)
            rows = [
                ("p%db" % v, str(v), "tg", TG, "Telegram-канал @sadaosato"),
                ("p%ds" % v, str(v), "site", site_dest, "Сайт sadaosato.pro"),
                ("p%dm" % v, str(v), "max", MAX, "MAX-канал"),
            ]
            for code, variant, channel, dest, label in rows:
                c.execute(
                    "INSERT INTO qr_codes (code,variant,channel,dest_url,label,created_at) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET dest_url=excluded.dest_url, label=excluded.label, "
                    "variant=excluded.variant, channel=excluded.channel",
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


def _auth_ok():
    """Token via ?token= / ?key= query (browser-friendly) or X-Internal-Token header."""
    supplied = (request.args.get("token") or request.args.get("key")
                or request.headers.get("X-Internal-Token", ""))
    expected = os.environ.get("QR_STATS_TOKEN") or ""
    if not expected:
        try:
            import app  # lazy import: app imports qrtrack, so import here avoids a cycle
            expected = (app.load_env() or {}).get("INTERNAL_TOKEN") or ""
        except Exception:
            expected = ""
    if session.get("dash") is True:
        return True
    return bool(expected) and supplied == expected


def _dash_creds():
    """Dashboard login creds — read from DASH_USER/DASH_PASS in amo.env (no secret in code)."""
    try:
        import app
        cfg = app.load_env() or {}
    except Exception:
        cfg = {}
    return (cfg.get("DASH_USER") or "admin", cfg.get("DASH_PASS") or "change-me")


def _collect(since=None):
    """Return marketing aggregates parsed from qr_scans. Variant/channel are
    derived from the code itself, so rows logged before a code was seeded
    (variant/channel NULL) are still attributed correctly."""
    where = "WHERE 1=1"
    params = []
    if since:
        where += " AND ts >= ?"
        params.append(since)
    c = reg._conn()
    try:
        human = c.execute(
            "SELECT code, COUNT(*) hits, COUNT(DISTINCT ip) uniq, MIN(ts) f, MAX(ts) l "
            "FROM qr_scans %s AND device!='bot' GROUP BY code" % where, params).fetchall()
        bots = c.execute(
            "SELECT code, COUNT(*) hits FROM qr_scans %s AND device='bot' GROUP BY code" % where,
            params).fetchall()
        dev = c.execute(
            "SELECT code, device, COUNT(*) n FROM qr_scans %s GROUP BY code, device" % where,
            params).fetchall()
        span = c.execute("SELECT MIN(ts) f, MAX(ts) l, COUNT(*) n FROM qr_scans %s" % where,
                         params).fetchone()
    finally:
        c.close()
    bot_by = {r["code"]: r["hits"] for r in bots}
    mob = {}
    for r in dev:
        if r["device"] == "mobile":
            mob[r["code"]] = mob.get(r["code"], 0) + r["n"]
    codes = []
    for r in human:
        v, ch = _parse(r["code"])
        codes.append({
            "code": r["code"], "variant": v, "channel": ch,
            "hits": r["hits"], "uniq": r["uniq"], "mobile": mob.get(r["code"], 0),
            "bots": bot_by.get(r["code"], 0), "first": r["f"], "last": r["l"],
        })
    # also surface codes that ONLY have bot hits (so nothing silently vanishes)
    seen = {x["code"] for x in codes}
    for code, n in bot_by.items():
        if code not in seen:
            v, ch = _parse(code)
            codes.append({"code": code, "variant": v, "channel": ch, "hits": 0,
                          "uniq": 0, "mobile": 0, "bots": n, "first": None, "last": None})
    codes.sort(key=lambda x: (x["variant"] or "z", x["channel"] or "z"))
    return codes, span


@bp.route("/api/qr/stats", methods=["GET"])
def stats():
    if not _auth_ok():
        return Response("unauthorized — add ?token=INTERNAL_TOKEN", status=401,
                        mimetype="text/plain; charset=utf-8")
    days = request.args.get("days", type=int)
    since = None
    if days and days > 0:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    codes, span = _collect(since)

    # aggregate by variant and by channel
    by_var = {}   # variant -> {channel -> (hits,uniq)}  + totals
    by_chan = {"tg": [0, 0], "site": [0, 0], "max": [0, 0]}
    tot_hits = tot_uniq = tot_bots = 0
    for x in codes:
        v = x["variant"] or "?"
        bv = by_var.setdefault(v, {"tg": [0, 0], "site": [0, 0], "max": [0, 0], "th": 0, "tu": 0})
        if x["channel"] in bv:
            bv[x["channel"]][0] += x["hits"]
            bv[x["channel"]][1] += x["uniq"]
        bv["th"] += x["hits"]; bv["tu"] += x["uniq"]
        if x["channel"] in by_chan:
            by_chan[x["channel"]][0] += x["hits"]; by_chan[x["channel"]][1] += x["uniq"]
        tot_hits += x["hits"]; tot_uniq += x["uniq"]; tot_bots += x["bots"]

    if request.args.get("format") == "json":
        return jsonify({"period": {"from": span["f"], "to": span["l"]},
                        "totals": {"hits": tot_hits, "uniq": tot_uniq, "bots": tot_bots},
                        "by_variant": by_var, "by_channel": by_chan, "codes": codes})

    # ---- HTML dashboard ----
    def td(h, u):
        return "<b>%d</b> <span class=u>/ %d</span>" % (h, u) if (h or u) else "<span class=z>·</span>"
    rows_var = []
    for v in sorted(by_var, key=lambda k: (k == "?", k)):
        bv = by_var[v]
        rows_var.append(
            "<tr><td class=name>%s <span class=u>p%s</span></td>"
            "<td>%s</td><td>%s</td><td>%s</td><td class=tot>%d <span class=u>/ %d</span></td></tr>"
            % (VARIANTS.get(v, "вариант " + v), v if v != "?" else "?",
               td(*bv["tg"]), td(*bv["site"]), td(*bv["max"]), bv["th"], bv["tu"]))
    rows_code = []
    for x in codes:
        rows_code.append(
            "<tr><td class=mono>%s</td><td>%s</td><td>%s</td>"
            "<td>%d</td><td>%d</td><td>%d</td><td>%d</td><td class=u>%s</td><td class=u>%s</td></tr>"
            % (x["code"], VARIANTS.get(x["variant"], "?"), CHAN_LABEL.get(x["channel"], "?"),
               x["hits"], x["uniq"], x["mobile"], x["bots"],
               (x["first"] or "")[:16].replace("T", " "), (x["last"] or "")[:16].replace("T", " ")))
    period = "%s — %s" % ((span["f"] or "?")[:16].replace("T", " "),
                          (span["l"] or "?")[:16].replace("T", " "))
    flt = ("за %d дн." % days) if days else "за всё время"
    html = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>QR-аналитика · Казань-Токио 2026</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1320;color:#e7ecf3;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#8a96a8;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:#161f31;border:1px solid #24304a;border-radius:12px;padding:14px 18px;min-width:130px}}
 .card .n{{font-size:26px;font-weight:700}} .card .k{{color:#8a96a8;font-size:12px}}
 table{{border-collapse:collapse;width:100%;margin:0 0 26px;background:#121a29;border-radius:10px;overflow:hidden}}
 th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #1e2840}}
 th{{background:#1a2336;color:#aab6c8;font-weight:600;font-size:13px}}
 td.tot,td.name{{font-weight:600}} .u{{color:#7f8ba0;font-weight:400;font-size:12px}}
 .z{{color:#46506a}} .mono{{font-family:ui-monospace,Menlo,monospace}}
 h2{{font-size:15px;color:#c3cde0;margin:0 0 8px}} a{{color:#5aa2ff}}
</style>
<h1>QR-аналитика — Казань-Токио 2026</h1>
<div class=sub>период: {period} · {flt} ·
 <a href="/api/qr/funnel?token={tok}">воронка до оплаты →</a> ·
 <a href="?token={tok}&format=json">JSON</a> ·
 <a href="?token={tok}">всё</a> · <a href="?token={tok}&days=7">7 дн.</a> · <a href="?token={tok}&days=1">сутки</a></div>
<div class=cards>
 <div class=card><div class=n>{tot_hits}</div><div class=k>переходов (люди)</div></div>
 <div class=card><div class=n>{tot_uniq}</div><div class=k>уникальных IP</div></div>
 <div class=card><div class=n>{tg_h}</div><div class=k>Telegram</div></div>
 <div class=card><div class=n>{site_h}</div><div class=k>Сайт</div></div>
 <div class=card><div class=n>{max_h}</div><div class=k>MAX</div></div>
 <div class=card><div class=n>{tot_bots}</div><div class=k>боты (исключены)</div></div>
</div>
<h2>По флаерам (кто) — переходы / уникальные</h2>
<table><tr><th>Флаер</th><th>Telegram</th><th>Сайт</th><th>MAX</th><th>Итого</th></tr>{rows_var}</table>
<h2>Детально по кодам</h2>
<table><tr><th>код</th><th>флаер</th><th>канал</th><th>переходы</th><th>уник.</th><th>моб.</th><th>боты</th><th>первый</th><th>последний</th></tr>{rows_code}</table>
</html>""".format(
        period=period, flt=flt, tok=request.args.get("token") or request.args.get("key") or "",
        tot_hits=tot_hits, tot_uniq=tot_uniq, tot_bots=tot_bots,
        tg_h=by_chan["tg"][0], site_h=by_chan["site"][0], max_h=by_chan["max"][0],
        rows_var="".join(rows_var) or "<tr><td colspan=5 class=z>нет данных</td></tr>",
        rows_code="".join(rows_code) or "<tr><td colspan=9 class=z>нет данных</td></tr>")
    return Response(html, mimetype="text/html; charset=utf-8")


_AF_RE = re.compile(r"afisha-v(\d+)")
PAID_STATES = ("paid", "ticket_issued", "checked_in")   # canonical "оплачено" (см. desk.py)


def _scan_variant_channel(row):
    v = row["variant"] or None
    ch = row["channel"] or None
    if not v or not ch:
        pv, pch = _parse(row["code"])
        v = v or pv
        ch = ch or pch
    return v, ch


def _attribute(slack_days=14):
    """Map each registration -> (variant, channel, how).

    Priority: (1) utm_campaign=afisha-v{N} + utm_content (precise, site QR);
    (2) IP bridge — registration's consent IP matches a human QR scan IP, scan
    no later than registration + slack. Else unattributed. QR can't change, so
    these are the only signals available end-to-end."""
    c = reg._conn()
    try:
        regs = c.execute(
            "SELECT id, status, created_at, utm_campaign, utm_content, "
            "amount_paid, price_eur FROM registrations WHERE merged_into IS NULL").fetchall()
        cons = c.execute(
            "SELECT registration_id rid, ip FROM consent_log WHERE ip IS NOT NULL AND ip!=''").fetchall()
        scans = c.execute(
            "SELECT code, variant, channel, ip, ts FROM qr_scans "
            "WHERE device!='bot' AND ip IS NOT NULL AND ip!=''").fetchall()
    finally:
        c.close()
    ip2reg = {}
    for r in cons:
        ip2reg.setdefault(r["rid"], set()).add(r["ip"])
    # index scans by ip for the bridge
    by_ip = {}
    for s in scans:
        by_ip.setdefault(s["ip"], []).append(s)

    out = {}
    for r in regs:
        v = ch = how = None
        m = _AF_RE.search(r["utm_campaign"] or "")
        if m:
            v = m.group(1)
            uc = (r["utm_content"] or "").lower()
            ch = uc if uc in ("site", "tg", "max") else None
            how = "utm"
        else:
            ips = ip2reg.get(r["id"], ())
            best = None
            for ip in ips:
                for s in by_ip.get(ip, ()):
                    if s["ts"] <= r["created_at"]:        # scan before/at registration
                        if best is None or s["ts"] > best["ts"]:
                            best = s
            if best is not None:
                v, ch = _scan_variant_channel(best)
                how = "ip"
        out[r["id"]] = {
            "variant": v, "channel": ch, "how": how, "status": r["status"],
            "paid": r["status"] in PAID_STATES,
            "amount_paid": r["amount_paid"] or 0,
            "price_eur": r["price_eur"] or 0,
        }
    return out


@bp.route("/api/qr/funnel", methods=["GET"])
def funnel():
    if not _auth_ok():
        return Response("unauthorized — add ?token=INTERNAL_TOKEN", status=401,
                        mimetype="text/plain; charset=utf-8")
    codes, span = _collect()                 # scans (human) per code
    attr = _attribute()

    # scans per variant
    scans_v = {}
    for x in codes:
        v = x["variant"] or "?"
        scans_v[v] = scans_v.get(v, 0) + x["hits"]

    # funnel per variant from attributed registrations
    blank = lambda: {"leads": 0, "reg": 0, "paid": 0, "rev": 0, "plan": 0}
    fv = {}
    direct = blank()                         # unattributed registrations
    for a in attr.values():
        bucket = fv.setdefault(a["variant"], blank()) if a["variant"] else direct
        bucket["leads"] += 1
        if a["status"] != "lead":
            bucket["reg"] += 1
        if a["paid"]:
            bucket["paid"] += 1
            bucket["rev"] += a["amount_paid"]
        bucket["plan"] += a["price_eur"]

    variants = sorted(set(list(scans_v) + list(fv)) - {"?"}, key=lambda k: int(k) if k.isdigit() else 99)
    tot = {"scans": 0, "leads": 0, "reg": 0, "paid": 0, "rev": 0, "plan": 0}

    def pct(a, b):
        return ("%.1f%%" % (100.0 * a / b)) if b else "—"

    rows = []
    for v in variants:
        s = scans_v.get(v, 0)
        f = fv.get(v, blank())
        tot["scans"] += s
        for k in ("leads", "reg", "paid", "rev", "plan"):
            tot[k] += f[k]
        rows.append(
            "<tr><td class=name>%s <span class=u>p%s</span></td>"
            "<td>%d</td><td>%d</td><td>%d</td><td class=tot>%d</td>"
            "<td>%s</td><td>%s</td><td>%d €<span class=u> / план %d</span></td></tr>"
            % (VARIANTS.get(v, "вариант " + v), v, s, f["leads"], f["reg"], f["paid"],
               pct(f["leads"], s), pct(f["paid"], f["leads"]), f["rev"], f["plan"]))

    if request.args.get("format") == "json":
        return jsonify({"totals": tot, "by_variant": fv, "scans": scans_v,
                        "unattributed": direct})

    tok = request.args.get("token") or request.args.get("key") or ""
    html = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Сквозная аналитика · Казань-Токио 2026</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1320;color:#e7ecf3;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 2px}} .sub{{color:#8a96a8;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:#161f31;border:1px solid #24304a;border-radius:12px;padding:14px 18px;min-width:120px}}
 .card .n{{font-size:26px;font-weight:700}} .card .k{{color:#8a96a8;font-size:12px}}
 .arw{{color:#46506a;align-self:center;font-size:20px}}
 table{{border-collapse:collapse;width:100%;background:#121a29;border-radius:10px;overflow:hidden;margin-bottom:16px}}
 th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid #1e2840}}
 th{{background:#1a2336;color:#aab6c8;font-weight:600;font-size:13px}}
 td.tot,td.name{{font-weight:600}} .u{{color:#7f8ba0;font-weight:400;font-size:12px}}
 a{{color:#5aa2ff}} .note{{color:#8a96a8;font-size:12px;margin-top:6px}}
</style>
<h1>Сквозная аналитика — путь до оплаты</h1>
<div class=sub>QR-скан → лид → регистрация → оплата · <a href="/api/qr/stats?token={tok}">← QR-переходы</a> ·
 <a href="?token={tok}&format=json">JSON</a></div>
<div class=cards>
 <div class=card><div class=n>{scans}</div><div class=k>сканов (люди)</div></div>
 <div class=arw>→</div>
 <div class=card><div class=n>{leads}</div><div class=k>лидов</div></div>
 <div class=arw>→</div>
 <div class=card><div class=n>{reg}</div><div class=k>регистраций</div></div>
 <div class=arw>→</div>
 <div class=card><div class=n>{paid}</div><div class=k>оплат</div></div>
 <div class=arw>→</div>
 <div class=card><div class=n>{rev} €</div><div class=k>выручка (факт)</div></div>
</div>
<table>
 <tr><th>Флаер</th><th>Сканы</th><th>Лиды</th><th>Рег.</th><th>Оплаты</th>
     <th>скан→лид</th><th>лид→оплата</th><th>Выручка</th></tr>
 {rows}
 <tr><td class=name>Без атрибуции <span class=u>(прямые/органика)</span></td>
     <td>·</td><td>{d_leads}</td><td>{d_reg}</td><td>{d_paid}</td><td>·</td><td>{d_conv}</td>
     <td>{d_rev} €<span class=u> / план {d_plan}</span></td></tr>
</table>
<div class=note>Атрибуция: точная по utm (site-QR несёт <code>afisha-vN</code>), для Telegram/MAX — мост по IP
 (совпадение IP скана и согласия на лендинге). Лиды от ботов/без IP попадают в «без атрибуции».
 «План» — сумма выбранных тарифов (<code>price_eur</code>); «факт» — подтверждённые оплаты.</div>
</html>""".format(
        tok=tok, scans=tot["scans"], leads=tot["leads"], reg=tot["reg"], paid=tot["paid"],
        rev=tot["rev"],
        rows="".join(rows) or "<tr><td colspan=8 class=u>нет данных</td></tr>",
        d_leads=direct["leads"], d_reg=direct["reg"], d_paid=direct["paid"],
        d_conv=pct(direct["paid"], direct["leads"]), d_rev=direct["rev"], d_plan=direct["plan"])
    return Response(html, mimetype="text/html; charset=utf-8")


PAID_REV = ("paid", "ticket_issued", "checked_in")


def _timeseries(since):
    """Per-day {date, scans, leads, paid} over the range."""
    wsc = "WHERE device!='bot'" + (" AND ts>=?" if since else "")
    wre = "WHERE merged_into IS NULL" + (" AND created_at>=?" if since else "")
    ps = [since] if since else []
    c = reg._conn()
    try:
        sc = {r[0]: r[1] for r in c.execute(
            "SELECT substr(ts,1,10) d, COUNT(*) FROM qr_scans %s GROUP BY d" % wsc, ps)}
        ld = {r[0]: r[1] for r in c.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) FROM registrations %s GROUP BY d" % wre, ps)}
        pd = {r[0]: r[1] for r in c.execute(
            "SELECT substr(COALESCE(paid_at,updated_at),1,10) d, COUNT(*) FROM registrations "
            "WHERE merged_into IS NULL AND status IN ('paid','ticket_issued','checked_in')"
            + (" AND COALESCE(paid_at,updated_at)>=?" if since else "") + " GROUP BY d", ps)}
    finally:
        c.close()
    days = sorted(set(sc) | set(ld) | set(pd))
    return [{"date": d, "scans": sc.get(d, 0), "leads": ld.get(d, 0), "paid": pd.get(d, 0)} for d in days]


def _by_source(since=None):
    """Сквозная разбивка по рекламным источникам/кампаниям из registrations:
    источник·канал·кампания → лиды / регистрации / оплаты / выручка / план.
    Источник склейки с Метрикой — utm_* (+ ym_client_id для офлайн-конверсий)."""
    c = reg._conn()
    try:
        ps = (since,) if since else ()
        wre = "WHERE merged_into IS NULL" + (" AND created_at>=?" if since else "")
        rows = c.execute(
            "SELECT utm_source, utm_medium, utm_campaign, status, "
            "amount_paid, price_eur FROM registrations " + wre, ps).fetchall()
    finally:
        c.close()
    agg = {}
    for r in rows:
        src = (r["utm_source"] or "(direct)")
        med = (r["utm_medium"] or "")
        camp = (r["utm_campaign"] or "")
        key = (src, med, camp)
        a = agg.setdefault(key, {"source": src, "medium": med, "campaign": camp,
                                 "leads": 0, "reg": 0, "paid": 0, "rev": 0, "plan": 0})
        paid = r["status"] in PAID_STATES
        a["leads"] += 1
        if r["status"] != "lead":
            a["reg"] += 1
        a["plan"] += (r["price_eur"] or 0)
        if paid:
            a["paid"] += 1
            a["rev"] += (r["amount_paid"] or r["price_eur"] or 0)
    out = list(agg.values())
    for a in out:
        a["conv"] = round(100.0 * a["paid"] / a["leads"], 1) if a["leads"] else 0.0
    return sorted(out, key=lambda x: (-x["paid"], -x["leads"]))


def _dash_data(days=None):
    from datetime import timedelta
    since = None
    if days and days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    codes, span = _collect(since)
    by_flyer, by_channel = {}, {"tg": 0, "site": 0, "max": 0}
    tot = {"scans": 0, "uniq": 0, "bots": 0, "mobile": 0, "leads": 0, "reg": 0, "paid": 0, "rev": 0}
    for x in codes:
        v = x["variant"] or "?"
        f = by_flyer.setdefault(v, {"variant": v, "label": VARIANTS.get(v, "вариант " + v),
                                    "scans": 0, "uniq": 0, "mobile": 0, "bots": 0,
                                    "leads": 0, "reg": 0, "paid": 0, "rev": 0})
        f["scans"] += x["hits"]; f["uniq"] += x["uniq"]; f["mobile"] += x["mobile"]; f["bots"] += x["bots"]
        if x["channel"] in by_channel:
            by_channel[x["channel"]] += x["hits"]
        tot["scans"] += x["hits"]; tot["uniq"] += x["uniq"]; tot["bots"] += x["bots"]; tot["mobile"] += x["mobile"]
    # attribution-based funnel (filtered by reg created_at)
    attr = _attribute()
    c = reg._conn()
    try:
        cre = {r["id"]: r["created_at"] for r in
               c.execute("SELECT id, created_at FROM registrations WHERE merged_into IS NULL")}
    finally:
        c.close()
    direct = {"leads": 0, "reg": 0, "paid": 0, "rev": 0}
    for rid, a in attr.items():
        if since and cre.get(rid, "") < since:
            continue
        money = (a.get("amount_paid") or a.get("price_eur") or 0) if a["paid"] else 0
        tot["leads"] += 1
        if a["status"] != "lead":
            tot["reg"] += 1
        if a["paid"]:
            tot["paid"] += 1; tot["rev"] += money
        b = by_flyer.get(a["variant"]) if a["variant"] else None
        tgt = b if b is not None else (direct if not a["variant"] else None)
        if tgt is not None:
            tgt["leads"] += 1
            if a["status"] != "lead":
                tgt["reg"] += 1
            if a["paid"]:
                tgt["paid"] += 1; tgt["rev"] += money

    def pct(a, b):
        return round(100.0 * a / b, 1) if b else 0.0
    flyers = sorted(by_flyer.values(), key=lambda r: (r["variant"] == "?", -r["scans"]))
    return {
        "by_source": _by_source(since),
        "period": {"from": (span["f"] or ""), "to": (span["l"] or ""), "days": days or 0},
        "kpi": dict(tot,
                    conv_scan_lead=pct(tot["leads"], tot["scans"]),
                    conv_lead_paid=pct(tot["paid"], tot["leads"])),
        "funnel": [
            {"k": "Сканы (люди)", "v": tot["scans"]},
            {"k": "Лиды", "v": tot["leads"], "conv": pct(tot["leads"], tot["scans"])},
            {"k": "Регистрации", "v": tot["reg"], "conv": pct(tot["reg"], tot["leads"])},
            {"k": "Оплаты", "v": tot["paid"], "conv": pct(tot["paid"], tot["reg"])},
        ],
        "by_channel": by_channel,
        "by_flyer": [dict(f, conv=pct(f["paid"], f["leads"])) for f in flyers],
        "timeseries": _timeseries(since),
        "attribution": {"utm_ip": tot["leads"] - direct["leads"], "direct": direct["leads"]},
        "direct": direct,
    }


@bp.route("/api/dash/data", methods=["GET"])
def dash_data():
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    days = request.args.get("days", type=int)
    return jsonify(_dash_data(days))


@bp.route("/dash/login", methods=["GET", "POST"])
def dash_login():
    u_ok, p_ok = _dash_creds()
    if request.method == "POST":
        if (request.form.get("u") == u_ok) and (request.form.get("p") == p_ok):
            session["dash"] = True
            session.permanent = True
            return redirect("/dash")
        return Response(LOGIN_HTML.replace("<!--ERR-->", "<div class=err>Неверный логин или пароль</div>"),
                        mimetype="text/html; charset=utf-8")
    if session.get("dash"):
        return redirect("/dash")
    return Response(LOGIN_HTML.replace("<!--ERR-->", ""), mimetype="text/html; charset=utf-8")


@bp.route("/dash/logout", methods=["GET"])
def dash_logout():
    session.clear()
    return redirect("/dash/login")


@bp.route("/dash", methods=["GET"])
def dash_page():
    if not session.get("dash"):
        return redirect("/dash/login")
    return Response(DASH_HTML, mimetype="text/html; charset=utf-8")


def register(flask_app):
    flask_app.register_blueprint(bp)
    if not getattr(flask_app, "secret_key", None):
        try:
            import app
            flask_app.secret_key = (app.load_env() or {}).get("INTERNAL_TOKEN") or "sato-dash-fallback"
        except Exception:
            flask_app.secret_key = "sato-dash-fallback"
    try:
        init()
    except Exception:
        log.exception("qrtrack init failed")
    log.info("qrtrack blueprint registered")


LOGIN_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Вход · Аналитика Казань-Токио 2026</title>
<style>
 body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
   font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
   background:linear-gradient(135deg,#0d1320,#13243d);color:#e7ecf3}
 .card{background:#161f31;border:1px solid #24304a;border-radius:16px;padding:32px;width:320px;
   box-shadow:0 20px 60px rgba(0,0,0,.4)}
 h1{font-size:18px;margin:0 0 4px} .sub{color:#8a96a8;font-size:13px;margin-bottom:22px}
 label{display:block;font-size:12px;color:#aab6c8;margin:12px 0 5px}
 input{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid #2a3650;
   background:#0f1726;color:#fff;font-size:15px}
 button{width:100%;margin-top:20px;padding:12px;border:0;border-radius:9px;background:#2f7bff;color:#fff;
   font-weight:700;font-size:15px;cursor:pointer}
 button:hover{background:#1f6bf0}
 .err{background:#3a1620;color:#ff8a9b;padding:9px 12px;border-radius:8px;font-size:13px;margin-bottom:14px}
 .logo{font-size:26px;margin-bottom:8px}
</style>
<form class=card method=post action="/dash/login">
 <div class=logo>📊</div>
 <h1>Аналитика саммита</h1>
 <div class=sub>Казань — Токио 2026</div>
 <!--ERR-->
 <label>Логин</label><input name=u autofocus autocomplete=username>
 <label>Пароль</label><input name=p type=password autocomplete=current-password>
 <button type=submit>Войти</button>
</form></html>"""


DASH_HTML = """<!doctype html><html lang=ru><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Аналитика · Казань-Токио 2026</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
 :root{--bg:#0d1320;--card:#161f31;--bd:#24304a;--mut:#8a96a8;--fg:#e7ecf3;--acc:#2f7bff}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
 header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 22px;border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--bg);z-index:5}
 header h1{font-size:17px;margin:0;flex:0 0 auto}
 .period{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
 .period button{background:var(--card);border:1px solid var(--bd);color:var(--fg);padding:7px 13px;border-radius:8px;cursor:pointer;font-size:13px}
 .period button.on{background:var(--acc);border-color:var(--acc);font-weight:700}
 .period a{color:var(--mut);text-decoration:none;padding:7px 10px;font-size:13px}
 main{padding:22px;max-width:1200px;margin:0 auto}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:22px}
 .kpi{background:var(--card);border:1px solid var(--bd);border-radius:13px;padding:16px 18px}
 .kpi .n{font-size:28px;font-weight:800;line-height:1.1}
 .kpi .k{color:var(--mut);font-size:12px;margin-top:4px}
 .kpi .b{display:inline-block;margin-top:8px;font-size:12px;color:#7ee0a6;background:#13301f;padding:2px 8px;border-radius:20px}
 .grid{display:grid;grid-template-columns:1.6fr 1fr;gap:18px;margin-bottom:18px}
 @media(max-width:820px){.grid{grid-template-columns:1fr}}
 .panel{background:var(--card);border:1px solid var(--bd);border-radius:13px;padding:18px}
 .panel h2{font-size:14px;margin:0 0 14px;color:#c3cde0}
 .funnel .row{margin:10px 0}
 .funnel .lab{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}
 .funnel .bar{height:26px;background:linear-gradient(90deg,#2f7bff,#46d3ff);border-radius:7px;min-width:2px}
 .funnel .cv{color:var(--mut);font-size:12px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #1e2840}
 th{color:#aab6c8;font-weight:600} td.r,th.r{text-align:right}
 .name{font-weight:600}
 .muted{color:var(--mut)}
</style>
<header>
 <h1>📊 Аналитика — Казань-Токио 2026</h1>
 <div class=period id=per>
   <button data-d=1>Сутки</button><button data-d=7 class=on>7 дней</button>
   <button data-d=30>30 дней</button><button data-d=0>Всё время</button>
   <a href="#" onclick="load();return false">⟳ Обновить</a>
   <a href="/dash/logout">Выйти</a>
 </div>
</header>
<main>
 <div class=cards id=kpis></div>
 <div class=grid>
   <div class=panel><h2>Динамика по дням</h2><canvas id=ts height=110></canvas></div>
   <div class=panel><h2>Каналы (сканы)</h2><canvas id=ch height=110></canvas></div>
 </div>
 <div class=grid>
   <div class=panel><h2>Воронка: скан → лид → регистрация → оплата</h2><div class=funnel id=funnel></div></div>
   <div class=panel><h2>Атрибуция лидов</h2><canvas id=attr height=110></canvas></div>
 </div>
 <div class=panel><h2>По флаерам</h2>
   <table id=flyers><thead><tr><th>Флаер</th><th class=r>Сканы</th><th class=r>Уник.</th><th class=r>Лиды</th><th class=r>Оплаты</th><th class=r>Конв.</th><th class=r>Выручка</th></tr></thead><tbody></tbody></table>
   <div class=muted id=period style="margin-top:10px;font-size:12px"></div>
 </div>
 <div class=panel><h2>Источники трафика (UTM · сквозная)</h2>
   <table id=sources><thead><tr><th>Источник</th><th>Канал</th><th>Кампания</th><th class=r>Лиды</th><th class=r>Рег.</th><th class=r>Оплаты</th><th class=r>Конв.</th><th class=r>Выручка</th><th class=r>План</th></tr></thead><tbody></tbody></table>
   <div class=muted style="margin-top:8px;font-size:12px">«Источник/Канал/Кампания» = utm_source/utm_medium/utm_campaign из заявки. «(direct)» — без меток. Оплата+выручка приходят при переводе сделки в «Оплачено» (и шлются офлайн-конверсией в Метрику).</div>
 </div>
</main>
<script>
var DAYS=7, tsC=null, chC=null, atC=null;
function el(id){return document.getElementById(id)}
function card(n,k,b){return '<div class=kpi><div class=n>'+n+'</div><div class=k>'+k+'</div>'+(b?'<div class=b>'+b+'</div>':'')+'</div>'}
function load(){
 fetch('/api/dash/data?days='+DAYS).then(function(r){if(!r.ok)throw 0;return r.json()}).then(render).catch(function(){location.href='/dash/login'});
}
function render(d){
 var k=d.kpi;
 el('kpis').innerHTML=
   card(k.scans,'Сканы (люди)','уник. '+k.uniq)+
   card(k.leads,'Лиды',k.conv_scan_lead+'% от сканов')+
   card(k.reg,'Регистрации','')+
   card(k.paid,'Оплаты',k.conv_lead_paid+'% от лидов')+
   card((k.rev||0)+' €','Выручка','')+
   card(k.bots,'Боты (искл.)','');
 // funnel
 var f=d.funnel, mx=Math.max(1,f[0].v), h='';
 f.forEach(function(s){var w=Math.round(100*s.v/mx);
   h+='<div class=row><div class=lab><span>'+s.k+'</span><span>'+s.v+(s.conv!=null?' <span class=cv>('+s.conv+'%)</span>':'')+'</span></div><div class=bar style="width:'+w+'%"></div></div>';});
 el('funnel').innerHTML=h;
 // flyers
 var tb=el('flyers').querySelector('tbody'), rows='';
 (d.by_flyer||[]).forEach(function(x){rows+='<tr><td class=name>'+x.label+'</td><td class=r>'+x.scans+'</td><td class=r>'+x.uniq+'</td><td class=r>'+x.leads+'</td><td class=r>'+x.paid+'</td><td class=r>'+x.conv+'%</td><td class=r>'+(x.rev||0)+' €</td></tr>';});
 if(d.direct&&(d.direct.leads||d.direct.paid))rows+='<tr><td class="name muted">Без атрибуции</td><td class=r>·</td><td class=r>·</td><td class=r>'+d.direct.leads+'</td><td class=r>'+d.direct.paid+'</td><td class=r>·</td><td class=r>'+(d.direct.rev||0)+' €</td></tr>';
 tb.innerHTML=rows;
 var p=d.period; el('period').textContent='Период: '+(p.from||'').slice(0,10)+' — '+(p.to||'').slice(0,10)+(p.days?(' · '+p.days+' дн.'):' · всё время');
 // источники (UTM, сквозная)
 var sb=el('sources').querySelector('tbody'), srows='';
 (d.by_source||[]).forEach(function(x){srows+='<tr><td class=name>'+(x.source||'')+'</td><td>'+(x.medium||'')+'</td><td>'+(x.campaign||'')+'</td><td class=r>'+x.leads+'</td><td class=r>'+x.reg+'</td><td class=r>'+x.paid+'</td><td class=r>'+x.conv+'%</td><td class=r>'+(x.rev||0)+' €</td><td class=r>'+(x.plan||0)+' €</td></tr>';});
 sb.innerHTML=srows||'<tr><td colspan=9 class=muted>нет данных</td></tr>';
 drawTS(d.timeseries||[]); drawCh(d.by_channel||{}); drawAttr(d.attribution||{});
}
function drawTS(ts){var lab=ts.map(function(x){return x.date.slice(5)});
 if(tsC)tsC.destroy();
 tsC=new Chart(el('ts'),{type:'line',data:{labels:lab,datasets:[
   {label:'Сканы',data:ts.map(function(x){return x.scans}),borderColor:'#46d3ff',backgroundColor:'transparent',tension:.3},
   {label:'Лиды',data:ts.map(function(x){return x.leads}),borderColor:'#2f7bff',backgroundColor:'transparent',tension:.3},
   {label:'Оплаты',data:ts.map(function(x){return x.paid}),borderColor:'#7ee0a6',backgroundColor:'transparent',tension:.3}]},
   options:{plugins:{legend:{labels:{color:'#aab6c8'}}},scales:{x:{ticks:{color:'#8a96a8'},grid:{color:'#1e2840'}},y:{ticks:{color:'#8a96a8'},grid:{color:'#1e2840'},beginAtZero:true}}}});}
function drawCh(c){if(chC)chC.destroy();
 chC=new Chart(el('ch'),{type:'doughnut',data:{labels:['Telegram','Сайт','MAX'],datasets:[{data:[c.tg||0,c.site||0,c.max||0],backgroundColor:['#229ED9','#2f7bff','#7e57ff']}]},options:{plugins:{legend:{labels:{color:'#aab6c8'}}}}});}
function drawAttr(a){if(atC)atC.destroy();
 atC=new Chart(el('attr'),{type:'doughnut',data:{labels:['Атрибутировано (utm/IP)','Без атрибуции'],datasets:[{data:[a.utm_ip||0,a.direct||0],backgroundColor:['#7ee0a6','#46506a']}]},options:{plugins:{legend:{labels:{color:'#aab6c8'}}}}});}
el('per').addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
 DAYS=+b.dataset.d;[].forEach.call(el('per').querySelectorAll('button'),function(x){x.classList.remove('on')});b.classList.add('on');load();});
load(); setInterval(load,60000);
</script></html>"""
