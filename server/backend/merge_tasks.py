#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""(B) Turn merged-duplicate flags into amoCRM "merge these two cards" tasks.

When link_identity merges two registrations that each carried a DIFFERENT amo
lead, it records reason='amo_dup' in merge_log. This runner creates one manager
task per flag (idempotent via handled=1) so no duplicate amo card is missed.

Safe to run repeatedly from cron:
    */5 * * * * www-data /opt/sato/venv/bin/python /opt/sato/merge_tasks.py >> /var/lib/sato/merge_tasks.log 2>&1
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as sato  # noqa: E402
import amo_sync     # noqa: E402


def main():
    cfg = sato.load_env()
    if not sato.amo_enabled(cfg):
        print("amoCRM not configured; skipping")
        return 0
    n = amo_sync.process_merge_conflicts()
    print("merge tasks created: %d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
