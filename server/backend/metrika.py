#!/usr/bin/env python3
"""amo → Яндекс.Метрика: офлайн-конверсии (подтверждённая оплата).

Когда сделка переходит в «Успешно — оплачено», шлём в Метрику конверсию по
ClientID (или yclid) с суммой — чтобы Метрика/Директ видели, какой источник/РК
реально принёс деньги (сквозная аналитика, обратная связь).

Включается переменными в /etc/sato/amo.env (читаются через app.load_env()):
  YANDEX_METRIKA_TOKEN    — OAuth-токен с правом записи (scope metrika:write)
  YANDEX_METRIKA_COUNTER  — id счётчика (по умолчанию 109717932)
  YANDEX_METRIKA_TARGET   — идентификатор цели-конверсии (по умолчанию "purchase";
                            в Метрике создать цель типа «JavaScript-событие» с этим id)
  YANDEX_METRIKA_CURRENCY — валюта суммы (по умолчанию EUR)
Без токена модуль — no-op (логирует skip). Никогда не бросает исключение наружу.

API: POST https://api-metrika.yandex.net/management/v1/counter/{id}/offline_conversions/upload
     ?client_id_type=CLIENT_ID|YCLID   тело: multipart/form-data CSV.
"""
import io
import csv
import time
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("metrika")
API = "https://api-metrika.yandex.net"


def _conf(cfg):
    cfg = cfg or {}
    return (
        (cfg.get("YANDEX_METRIKA_TOKEN") or "").strip(),
        (cfg.get("YANDEX_METRIKA_COUNTER") or "109717932").strip(),
        (cfg.get("YANDEX_METRIKA_TARGET") or "purchase").strip(),
        (cfg.get("YANDEX_METRIKA_CURRENCY") or "EUR").strip(),
    )


def enabled(cfg):
    tok, counter, _, _ = _conf(cfg)
    return bool(tok and counter)


def offline_purchase(cfg, client_id=None, yclid=None, price_eur=0, reg_id="", target=None):
    """Одна офлайн-конверсия в Метрику. Приоритет ClientID, фолбэк yclid.
    Возвращает True при успехе. Никогда не бросает исключение."""
    try:
        tok, counter, def_target, currency = _conf(cfg)
        if not (tok and counter):
            logger.info("metrika offline skip (no token/counter): reg=%s", reg_id)
            return False
        client_id = str(client_id).strip() if client_id else ""
        yclid = str(yclid).strip() if yclid else ""
        if not client_id and not yclid:
            logger.info("metrika offline skip (no ClientID/yclid): reg=%s", reg_id)
            return False
        tgt = target or def_target
        ts = int(time.time())
        buf = io.StringIO()
        w = csv.writer(buf)
        if client_id:
            id_type, id_col, id_val = "CLIENT_ID", "ClientId", client_id
        else:
            id_type, id_col, id_val = "YCLID", "Yclid", yclid
        w.writerow([id_col, "Target", "DateTime", "Price", "Currency"])
        w.writerow([id_val, tgt, ts, int(price_eur or 0), currency])
        return _post(tok, counter, id_type, buf.getvalue(), reg_id)
    except Exception as exc:
        logger.warning("metrika offline error reg=%s: %s", reg_id, exc)
        return False


def _post(token, counter, id_type, csv_text, reg_id):
    url = ("%s/management/v1/counter/%s/offline_conversions/upload?client_id_type=%s"
           % (API, counter, id_type))
    boundary = "----satometrika%d" % int(time.time() * 1000)
    body = (
        ("--%s\r\n" % boundary)
        + 'Content-Disposition: form-data; name="file"; filename="conv.csv"\r\n'
        + "Content-Type: text/csv\r\n\r\n"
        + csv_text + "\r\n"
        + ("--%s--\r\n" % boundary)
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "OAuth " + token)
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
            logger.info("metrika offline OK reg=%s http=%s resp=%s", reg_id, r.status, txt[:200])
            return True
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        logger.warning("metrika offline HTTP %s reg=%s: %s", e.code, reg_id, txt[:300])
        return False
    except Exception as exc:
        logger.warning("metrika offline transport reg=%s: %s", reg_id, exc)
        return False


if __name__ == "__main__":
    # ручной тест: python metrika.py <client_id> [price]
    import sys
    import app
    logging.basicConfig(level=logging.INFO)
    cid = sys.argv[1] if len(sys.argv) > 1 else ""
    price = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    print("enabled:", enabled(app.load_env()))
    print("result:", offline_purchase(app.load_env(), client_id=cid, price_eur=price, reg_id="manual-test"))
