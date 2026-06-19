"""Local operational registrations store (SQLite, shared by web backend + bot).

amoCRM = sales funnel. THIS db = operational source of truth for
registration / payment / ticket / check-in + an append-only consent audit
trail (152-FZ / GDPR Art.7). Lives at REG_DB, WAL, group-writable by `sato`.

Defensive: every public call wraps its own short-lived connection and never
raises into the request path on a benign error (caller wraps in try/except).
"""
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

REG_DB = os.environ.get("REG_DB", "/var/lib/sato/registrations.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  full_name TEXT, phone_e164 TEXT, email_lc TEXT,
  city TEXT, clinic TEXT, specialty TEXT, badge_name TEXT,
  source TEXT, telegram_user_id INTEGER, telegram_username TEXT,
  utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
  ym_client_id TEXT, yclid TEXT, page_url TEXT,
  format TEXT, channel TEXT, message TEXT,
  package TEXT, price_eur INTEGER, currency TEXT DEFAULT 'EUR',
  amocrm_lead_id INTEGER, amocrm_contact_id INTEGER,
  payment_provider TEXT, payment_id TEXT UNIQUE,
  payment_link TEXT, amount_paid INTEGER, paid_at TEXT,
  ticket_id TEXT UNIQUE, ticket_issued_at TEXT, qr_delivered_channels TEXT,
  status TEXT NOT NULL DEFAULT 'lead',
  checked_in_at TEXT, checked_in_by TEXT, check_in_gate TEXT,
  reminders_sent TEXT DEFAULT '',
  merged_into TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_reg_phone ON registrations(phone_e164) WHERE phone_e164 IS NOT NULL AND phone_e164<>'';
CREATE INDEX IF NOT EXISTS ix_reg_email ON registrations(email_lc);
CREATE INDEX IF NOT EXISTS ix_reg_status ON registrations(status);
CREATE INDEX IF NOT EXISTS ix_reg_tg ON registrations(telegram_user_id);

CREATE TABLE IF NOT EXISTS merge_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  canonical_id TEXT, merged_id TEXT, matched_on TEXT, reason TEXT,
  amo_lead_a INTEGER, amo_lead_b INTEGER, handled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS status_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registration_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
  ts TEXT NOT NULL, actor TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS consent_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  registration_id TEXT, subject_phone TEXT, subject_email TEXT, subject_tg INTEGER,
  doc_type TEXT NOT NULL, doc_version TEXT, action TEXT NOT NULL,
  ts TEXT NOT NULL, ip TEXT, user_agent TEXT, channel TEXT
);
CREATE TABLE IF NOT EXISTS scan_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT, ts TEXT, result TEXT, gate TEXT, staff TEXT
);
"""

_PRICE = {"500": 500, "800": 800, "1000": 1000, "1800": 1800}
_ALLOWED = {
    "lead": {"registered", "paid", "cancelled"},
    "registered": {"paid", "cancelled"},
    "paid": {"ticket_issued", "refunded", "cancelled"},
    "ticket_issued": {"checked_in", "no_show", "refunded", "cancelled"},
    "checked_in": set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    os.makedirs(os.path.dirname(REG_DB), exist_ok=True)
    c = sqlite3.connect(REG_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init():
    c = _conn()
    c.executescript(SCHEMA)
    # migrate: add columns to a pre-existing registrations table
    cols = {r[1] for r in c.execute("PRAGMA table_info(registrations)")}
    if "merged_into" not in cols:
        c.execute("ALTER TABLE registrations ADD COLUMN merged_into TEXT")
    for _mcol in ("ym_client_id", "yclid", "page_url"):
        if _mcol not in cols:
            c.execute("ALTER TABLE registrations ADD COLUMN %s TEXT" % _mcol)
    c.execute("CREATE INDEX IF NOT EXISTS ix_reg_merged ON registrations(merged_into)")
    c.commit()
    c.close()


def normalize_phone(p):
    """Canonical E.164. RU is unified regardless of how the prefix was typed —
    +7…, 8…, 7…, bare 10 digits, with spaces/brackets/dashes — all collapse to
    +7XXXXXXXXXX (one number). Foreign numbers keep their country code. Idempotent."""
    if not p:
        return ""
    d = re.sub(r"\D", "", str(p))
    if d.startswith("00"):          # 00 = international access prefix -> drop
        d = d[2:]
    if len(d) == 11 and d[0] in ("7", "8"):   # RU national: 7XXXXXXXXXX / 8XXXXXXXXXX
        d = d[1:]
    if len(d) == 10:                # bare RU subscriber number
        return "+7" + d
    return ("+" + d) if d else ""


def price_from_format(fmt):
    s = str(fmt or "")
    for tok in ("1800", "1000", "800", "500"):
        if tok in s:
            return int(tok)
    return None


# Identity resolution -------------------------------------------------------- #
# A person is the union of stable keys: phone_e164 (E.164) > email_lc > telegram_user_id.
# Phone is the identity owner: records with DIFFERENT non-null phones are NEVER
# auto-merged (logged as a conflict). tg_id / non-shared email add a link only
# when phones are compatible (equal or one side absent).
STATUS_RANK = {"lead": 0, "registered": 1, "paid": 2, "ticket_issued": 3, "checked_in": 4}
# fields filled on the canonical only when it is missing them (latest wins for blanks)
_FILL = ["full_name", "phone_e164", "email_lc", "telegram_user_id", "telegram_username",
         "city", "clinic", "specialty", "badge_name", "message", "currency"]
# first-touch marketing attribution — taken from the OLDEST record, never overwritten
_FIRST_TOUCH = ["source", "channel", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
                "ym_client_id", "yclid", "page_url"]
# payment / fulfilment block — copied as a unit from whichever record actually paid
_PAYMENT = ["payment_provider", "payment_id", "payment_link", "amount_paid", "paid_at",
            "ticket_id", "ticket_issued_at", "qr_delivered_channels",
            "checked_in_at", "checked_in_by", "check_in_gate", "pretix_order_code",
            "pretix_secret", "pretix_position_id"]


def _g(row, k):
    try:
        return row[k]
    except (KeyError, IndexError):
        return None


def _rank(s):
    return STATUS_RANK.get(s, -1)


def _row_price(row):
    return _g(row, "price_eur") or price_from_format(_g(row, "package")) or 0


def _has_payment(row):
    return bool(_g(row, "paid_at") or _g(row, "ticket_id") or _g(row, "pretix_order_code"))


def _combine(canon, other):
    """Return (updates, amo_conflict) to fold `other` (row or rec dict) into `canon`."""
    upd = {}
    for k in _FILL + _FIRST_TOUCH:
        if not _g(canon, k) and _g(other, k):
            upd[k] = _g(other, k)
    # status: most advanced active stage wins
    if _rank(_g(other, "status")) > _rank(upd.get("status", _g(canon, "status"))):
        upd["status"] = _g(other, "status")
    # tariff: keep the higher-value package
    if _row_price(other) > max(_row_price(canon), upd.get("price_eur", 0) or 0):
        upd["price_eur"] = _row_price(other)
        if _g(other, "package"):
            upd["package"] = _g(other, "package")
    # payment block: copy as a unit if canon hasn't paid but other has
    if _has_payment(other) and not _has_payment(canon):
        for k in _PAYMENT:
            if _g(other, k) is not None:
                upd[k] = _g(other, k)
    # amoCRM: fill if missing; flag conflict if two different leads
    amo_conflict = False
    a, b = _g(canon, "amocrm_lead_id"), _g(other, "amocrm_lead_id")
    if not a and b:
        upd["amocrm_lead_id"] = b
    elif a and b and int(a) != int(b):
        amo_conflict = True
    if not _g(canon, "amocrm_contact_id") and _g(other, "amocrm_contact_id"):
        upd["amocrm_contact_id"] = _g(other, "amocrm_contact_id")
    return upd, amo_conflict


def _email_shared(c, email):
    """True if `email` is attached to 2+ distinct phones — a corporate/clinic
    inbox, NOT an identity key on its own."""
    if not email:
        return False
    n = c.execute("SELECT COUNT(DISTINCT phone_e164) FROM registrations "
                  "WHERE email_lc=? AND phone_e164 IS NOT NULL AND phone_e164<>'' "
                  "AND merged_into IS NULL", (email,)).fetchone()[0]
    return n >= 2


def _find_matches(c, phone, email, tg):
    """Active rows matching ANY identity key (email only if not shared)."""
    seen, out = set(), []
    def add(rows):
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"]); out.append(r)
    if phone:
        add(c.execute("SELECT * FROM registrations WHERE phone_e164=? AND merged_into IS NULL", (phone,)))
    if tg:
        add(c.execute("SELECT * FROM registrations WHERE telegram_user_id=? AND merged_into IS NULL", (tg,)))
    if email and not _email_shared(c, email):
        add(c.execute("SELECT * FROM registrations WHERE email_lc=? AND merged_into IS NULL", (email,)))
    return out


def _write(c, rid, upd):
    if not upd:
        return
    upd = dict(upd); upd["updated_at"] = _now()
    c.execute("UPDATE registrations SET %s WHERE id=?" % ",".join("%s=?" % k for k in upd),
              list(upd.values()) + [rid])


def _absorb(c, canon, other, matched_on):
    """Merge existing row `other` into `canon`: repoint children, fold fields,
    null `other`'s UNIQUE fields, tombstone it (merged), log it."""
    upd, amo_conflict = _combine(canon, other)
    # repoint audit/child tables
    for tbl in ("status_history", "consent_log"):
        c.execute("UPDATE %s SET registration_id=? WHERE registration_id=?" % tbl,
                  (canon["id"], other["id"]))
    # free UNIQUE columns on the tombstone so canon can hold them
    c.execute("UPDATE registrations SET phone_e164=NULL, payment_id=NULL, ticket_id=NULL, "
              "status='merged', merged_into=?, updated_at=? WHERE id=?",
              (canon["id"], _now(), other["id"]))
    _write(c, canon["id"], upd)
    c.execute("INSERT INTO merge_log(ts,canonical_id,merged_id,matched_on,reason,amo_lead_a,amo_lead_b) "
              "VALUES (?,?,?,?,?,?,?)",
              (_now(), canon["id"], other["id"], matched_on,
               "amo_dup" if amo_conflict else "merge",
               canon["amocrm_lead_id"], other["amocrm_lead_id"]))
    # refresh canon view for subsequent combines
    return c.execute("SELECT * FROM registrations WHERE id=?", (canon["id"],)).fetchone()


def _insert_new(c, fields):
    rid = uuid.uuid4().hex
    status = fields.get("status") or ("registered" if fields["package"] else "lead")
    cols = ["id", "created_at", "updated_at"] + [k for k in fields if k != "status"] + ["status"]
    vals = [rid, _now(), _now()] + [fields[k] for k in fields if k != "status"] + [status]
    c.execute("INSERT INTO registrations (%s) VALUES (%s)"
              % (",".join(cols), ",".join("?" * len(cols))), vals)
    c.execute("INSERT INTO status_history (registration_id,from_status,to_status,ts,actor,reason) "
              "VALUES (?,?,?,?,?,?)", (rid, None, status, _now(), fields["source"], "created"))
    return rid


def link_identity(rec):
    """Resolve `rec` to a single canonical registration, merging any duplicates.
    Deterministic: phone owns identity; differing non-null phones never merge.
    Returns the canonical registration id."""
    phone = normalize_phone(rec.get("phone") or rec.get("phone_e164"))
    email = (rec.get("email") or rec.get("email_lc") or "").strip().lower()
    tg = rec.get("telegram_user_id")
    price = price_from_format(rec.get("format")) or price_from_format(rec.get("package"))
    fields = {
        "full_name": (rec.get("name") or rec.get("full_name") or "").strip() or None,
        "phone_e164": phone or None, "email_lc": email or None,
        "city": rec.get("city") or None, "clinic": rec.get("clinic") or None,
        "specialty": rec.get("specialty") or None,
        "source": rec.get("source") or "web",
        "telegram_user_id": tg, "telegram_username": rec.get("telegram_username"),
        "utm_source": rec.get("utm_source"), "utm_medium": rec.get("utm_medium"),
        "utm_campaign": rec.get("utm_campaign"), "utm_content": rec.get("utm_content"),
        "utm_term": rec.get("utm_term"),
        "ym_client_id": (rec.get("ym_client_id") or "").strip() or None,
        "yclid": (rec.get("yclid") or "").strip() or None,
        "page_url": rec.get("page_url") or None,
        "format": rec.get("format"), "channel": rec.get("channel"), "message": rec.get("message"),
        "package": rec.get("package") or (str(price) if price else None), "price_eur": price,
        "amocrm_lead_id": rec.get("amocrm_lead_id"), "amocrm_contact_id": rec.get("amocrm_contact_id"),
        "status": rec.get("status"),
    }
    c = _conn()
    try:
        matches = _find_matches(c, phone, email, tg)
        if not matches:
            rid = _insert_new(c, fields); c.commit(); return rid

        # phone owns identity: collect every distinct non-null phone in play
        phones = {m["phone_e164"] for m in matches if m["phone_e164"]}
        if phone:
            phones.add(phone)
        canon = min(matches, key=lambda r: r["created_at"])      # first touch

        if len(phones) > 1:
            # conflict — differing phones must NEVER share a record
            owner = next((m for m in matches if m["phone_e164"] == phone), None) if phone else None
            others = [m["id"] for m in matches if not owner or m["id"] != owner["id"]]
            c.execute("INSERT INTO merge_log(ts,canonical_id,merged_id,matched_on,reason,amo_lead_a,amo_lead_b) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (_now(), (owner or canon)["id"], ",".join(others), "phone_conflict",
                       "phones=" + ",".join(sorted(phones)), None, None))
            if owner:
                canon = owner                       # rec belongs to its exact phone owner
            elif phone:
                rid = _insert_new(c, fields); c.commit(); return rid   # new distinct phone -> own record
            # else: rec has no phone, matches span >1 phone -> attach to oldest (best effort)
        else:
            # one phone (or none) -> all matches are the same person -> merge into oldest
            for other in matches:
                if other["id"] != canon["id"]:
                    canon = _absorb(c, canon, other, "phone/email/tg")

        # fold the incoming rec into the canonical
        upd, _ = _combine(canon, fields)
        if not canon["status"] or canon["status"] == "lead":
            inc = fields.get("status") or ("registered" if fields.get("package") else None)
            if inc and _rank(inc) > _rank(canon["status"]):
                upd["status"] = inc
        _write(c, canon["id"], upd)
        c.commit()
        return canon["id"]
    finally:
        c.close()


def upsert_registration(rec):
    """Back-compat shim — now full identity resolution. Returns canonical id."""
    return link_identity(rec)


def reconcile(apply=False):
    """Cluster existing active rows by shared identity keys and report (or, with
    apply=True, perform) merges. Same rules as link_identity: phone owns identity,
    differing non-null phones are flagged, not merged. Returns a report dict."""
    c = _conn()
    try:
        rows = list(c.execute("SELECT * FROM registrations WHERE merged_into IS NULL"))
        idx = {r["id"]: r for r in rows}
        parent = {r["id"]: r["id"] for r in rows}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # shared-email guard: emails attached to 2+ distinct phones are not keys
        shared = set()
        for r in rows:
            if r["email_lc"] and _email_shared(c, r["email_lc"]):
                shared.add(r["email_lc"])
        for key in ("phone_e164", "telegram_user_id", "email_lc"):
            buckets = {}
            for r in rows:
                v = r[key]
                if not v:
                    continue
                if key == "email_lc" and v in shared:
                    continue
                buckets.setdefault(v, []).append(r["id"])
            for ids in buckets.values():
                for other in ids[1:]:
                    union(ids[0], other)

        clusters = {}
        for r in rows:
            clusters.setdefault(find(r["id"]), []).append(r["id"])

        merges, conflicts = [], []
        for ids in clusters.values():
            if len(ids) < 2:
                continue
            members = [idx[i] for i in ids]
            phones = {m["phone_e164"] for m in members if m["phone_e164"]}
            canon = min(members, key=lambda r: r["created_at"])
            if len(phones) > 1:
                conflicts.append({"phones": sorted(phones),
                                  "ids": [m["id"] for m in members]})
                continue
            group = {"canonical": canon["id"], "merged": [m["id"] for m in members if m["id"] != canon["id"]]}
            merges.append(group)
            if apply:
                cn = canon
                for m in members:
                    if m["id"] != canon["id"]:
                        cn = _absorb(c, cn, m, "reconcile")
        if apply:
            c.commit()
        return {"active_rows": len(rows), "merges": merges, "conflicts": conflicts,
                "merged_count": sum(len(g["merged"]) for g in merges), "applied": apply}
    finally:
        c.close()


def log_consent(registration_id, doc_type, doc_version, action="granted",
                subject_phone=None, subject_email=None, subject_tg=None,
                ip=None, user_agent=None, channel="web"):
    c = _conn()
    try:
        c.execute(
            "INSERT INTO consent_log (registration_id, subject_phone, subject_email, subject_tg,"
            " doc_type, doc_version, action, ts, ip, user_agent, channel)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (registration_id, normalize_phone(subject_phone) or None,
             (subject_email or "").strip().lower() or None, subject_tg,
             doc_type, doc_version, action, _now(), ip, user_agent, channel),
        )
        c.commit()
    finally:
        c.close()


def set_status(registration_id, new_status, actor="system", reason=None, force=False):
    c = _conn()
    try:
        row = c.execute("SELECT status FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if not row:
            return False
        cur = row["status"]
        if not force and new_status not in _ALLOWED.get(cur, set()):
            return False
        c.execute("UPDATE registrations SET status=?, updated_at=? WHERE id=?",
                  (new_status, _now(), registration_id))
        c.execute(
            "INSERT INTO status_history (registration_id, from_status, to_status, ts, actor, reason) VALUES (?,?,?,?,?,?)",
            (registration_id, cur, new_status, _now(), actor, reason),
        )
        c.commit()
        return True
    finally:
        c.close()


if __name__ == "__main__":
    init()
    print("registrations db initialized at", REG_DB)
