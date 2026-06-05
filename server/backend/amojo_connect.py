#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot CLI: connect the custom amoJo channel and persist scope_id.

Run once, as www-data, AFTER AMOJO_CHANNEL_ID + AMOJO_CHANNEL_SECRET are in
/etc/sato/amo.env:

    sudo -u www-data /opt/sato/venv/bin/python /opt/sato/amojo_connect.py

It calls GET /api/v4/account?with=amojo_id (OAuth), then the signed
POST /v2/origin/custom/{channel_id}/connect, and writes scope_id +amojo_id to
/var/lib/sato/amojo_state.json. Prints the scope_id so you can also paste it
into AMOJO_SCOPE_ID in amo.env for reference.
"""

import logging
import sys

import amojo
import app  # reuse load_env + amo_request (OAuth) from the Flask backend


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("amojo_connect")

    cfg = app.load_env()
    if not amojo.amojo_enabled(cfg):
        log.error(
            "AMOJO_CHANNEL_ID / AMOJO_CHANNEL_SECRET missing in %s — cannot connect.",
            app.ENV_FILE,
        )
        return 2
    if not app.amo_enabled(cfg):
        log.error(
            "amoCRM OAuth not configured/authorized (need tokens for the "
            "account?with=amojo_id call)."
        )
        return 2

    try:
        scope_id = amojo.connect(cfg, app.amo_request, logger=log)
    except Exception as exc:
        log.error("connect failed: %s", exc)
        return 1

    log.info("amoJo connected. scope_id=%s", scope_id)
    print(scope_id)
    log.info(
        "Persisted to %s. You may also set AMOJO_SCOPE_ID=%s in %s.",
        amojo.STATE_FILE, scope_id, app.ENV_FILE,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
