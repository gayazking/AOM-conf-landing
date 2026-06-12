#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prod safety backup — consistent snapshot of all deals with full detail.

Run BEFORE every prod change. Writes to /var/lib/sato/backups/<UTC-ts>/:
  - registrations.sqlite3  (consistent sqlite .backup copy)
  - <table>.json           (every table, every column)
  - _manifest.json         (row counts)

    sudo -u www-data /opt/sato/venv/bin/python /opt/sato/backup_deals.py
"""
import os
import sqlite3
import json
import datetime

DB = os.environ.get("REG_DB", "/var/lib/sato/registrations.sqlite3")


def main():
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(os.path.dirname(DB), "backups", ts)
    os.makedirs(out, exist_ok=True)

    src = sqlite3.connect(DB)
    # consistent on-disk snapshot (safe even under concurrent writes / WAL)
    dst = sqlite3.connect(os.path.join(out, "registrations.sqlite3"))
    with dst:
        src.backup(dst)
    dst.close()

    src.row_factory = sqlite3.Row
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    summary = {}
    for t in tables:
        rows = [dict(r) for r in src.execute("SELECT * FROM %s" % t)]
        with open(os.path.join(out, t + ".json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)
        summary[t] = len(rows)
    src.close()

    with open(os.path.join(out, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"ts": ts, "db": DB, "tables": summary}, fh, ensure_ascii=False, indent=1)
    print("BACKUP %s %s" % (out, summary))
    return out


if __name__ == "__main__":
    main()
