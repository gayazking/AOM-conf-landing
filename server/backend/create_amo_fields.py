#!/usr/bin/env python3
"""Идемпотентно создаёт кастом-поля СДЕЛКИ в amoCRM для сквозной аналитики
Метрика↔amo и печатает строки AMO_CF_*=<id> для добавления в /etc/sato/amo.env.

Запуск на сервере:
  cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python create_amo_fields.py
Потом скопировать выведенные AMO_CF_*=<id> в /etc/sato/amo.env и перезапустить sato-api.
"""
import app

# env-ключ -> человекочитаемое имя поля в amo
FIELDS = [
    ("AMO_CF_UTM_SOURCE",   "UTM Source"),
    ("AMO_CF_UTM_MEDIUM",   "UTM Medium"),
    ("AMO_CF_UTM_CAMPAIGN", "UTM Campaign"),
    ("AMO_CF_UTM_CONTENT",  "UTM Content"),
    ("AMO_CF_UTM_TERM",     "UTM Term"),
    ("AMO_CF_YM_CLIENT_ID", "Yandex ClientID"),
    ("AMO_CF_YCLID",        "yclid"),
    ("AMO_CF_PAGE_URL",     "Страница входа"),
]


# кастом-поля КОНТАКТА (entity=contacts)
CONTACT_FIELDS = [
    ("AMO_CF_CT_TELEGRAM", "Telegram"),
]


def existing_fields(cfg, entity="leads"):
    """name -> id для всех уже существующих кастом-полей сущности (leads/contacts)."""
    out, page = {}, 1
    while True:
        r = app.amo_request(cfg, "GET",
                            "/api/v4/%s/custom_fields?page=%d&limit=250" % (entity, page), None)
        if r.status_code in (204, 404):
            break
        if r.status_code != 200:
            print("ERR list page=%d: %s %s" % (page, r.status_code, r.text[:300]))
            break
        data = r.json()
        for f in data.get("_embedded", {}).get("custom_fields", []):
            out[f.get("name")] = f.get("id")
        if not data.get("_links", {}).get("next"):
            break
        page += 1
    return out


def _process(cfg, entity, fields, env_lines):
    have = existing_fields(cfg, entity)
    for env_key, name in fields:
        fid = have.get(name)
        if fid:
            print("EXISTS  %-26s -> %s (%s)" % (name, fid, entity))
        else:
            r = app.amo_request(cfg, "POST", "/api/v4/%s/custom_fields" % entity,
                                [{"name": name, "type": "text"}])
            if r.status_code not in (200, 201):
                print("ERR create %r (%s): %s %s" % (name, entity, r.status_code, r.text[:300]))
                continue
            fid = (r.json().get("_embedded", {}).get("custom_fields", [{}])[0] or {}).get("id")
            print("CREATED %-26s -> %s (%s)" % (name, fid, entity))
        if fid:
            env_lines.append("%s=%s" % (env_key, fid))


def main():
    cfg = app.load_env()
    env_lines = []
    _process(cfg, "leads", FIELDS, env_lines)
    _process(cfg, "contacts", CONTACT_FIELDS, env_lines)
    print("\n# ---- добавить в /etc/sato/amo.env ----")
    for line in env_lines:
        print(line)


if __name__ == "__main__":
    main()
