#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backfill pending leads into amoCRM.

Reads /var/lib/sato/pending.jsonl, attempts to deliver each queued lead via the
same code path as the live API, and rewrites pending.jsonl with ONLY the leads
that still failed. Succeeded leads are dropped.

Safe to run repeatedly (e.g. from cron every few minutes):
    */5 * * * * www-data /opt/sato/venv/bin/python /opt/sato/backfill.py >> /var/lib/sato/backfill.log 2>&1

Run manually:
    sudo -u www-data /opt/sato/venv/bin/python /opt/sato/backfill.py
"""

import json
import os
import sys
import time

# Reuse the live app's logic so behaviour matches the API exactly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as sato  # noqa: E402


def main():
    pending_path = sato.PENDING_FILE
    if not os.path.exists(pending_path):
        print("no pending file; nothing to do")
        return 0

    cfg = sato.load_env()
    if not sato.amo_enabled(cfg):
        print("amoCRM not configured/connected yet; leaving pending queue intact")
        return 0

    # Read all pending entries.
    entries = []
    with open(pending_path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except Exception as exc:
                print("skipping unparseable pending line: %s" % exc)

    if not entries:
        print("pending file empty; removing")
        _truncate(pending_path)
        return 0

    print("attempting backfill of %d pending lead(s)" % len(entries))

    still_failed = []
    delivered = 0
    for entry in entries:
        lead = entry.get("lead") or {}
        try:
            lead_id = sato.push_to_amo(cfg, lead)
            delivered += 1
            print("delivered pending lead -> amo id=%s" % lead_id)
        except Exception as exc:
            print("still failing, re-queueing: %s" % str(exc)[:300])
            entry["last_error"] = str(exc)[:500]
            entry["last_attempt_ts"] = sato.now_iso()
            still_failed.append(entry)
        # Be polite to amoCRM's ~7 req/s limit (each lead = 2 calls).
        time.sleep(0.5)

    _rewrite(pending_path, still_failed)
    print("done: delivered=%d, still_pending=%d" % (delivered, len(still_failed)))
    return 0


def _truncate(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _rewrite(path, entries):
    """Atomically rewrite the pending file with only the still-failed entries.
    If empty, remove the file."""
    if not entries:
        _truncate(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
