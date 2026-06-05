# AOM Conf — «Казань — Токио» 2026 (sadaosato.pro)

Лендинг + Telegram-бот + backend для международного стоматологического саммита.

## Структура
- `Казань-Токио-2026-TRACKED.html` — прод-лендинг (самораспаковывающийся бандл: форма→/api/lead, UTM, СБП-оплата, 2 согласия).
- `oferta.html` / `privacy.html` / `consent.html` — юр-страницы (из `Юр.документы/`).
- `server/backend/` — Flask+gunicorn (`/opt/sato`): `/api/lead` (amoCRM+локальная БД+аудит согласий), amoJo-чаты, билеты QR (`tickets.py`), чек-ин, реестр регистраций (`reg.py`).
- `server/bot/` — aiogram 3.x бот (`/opt/sato-bot`): регистрация (подписка→согласие→телефон→имя→город), ИИ-продавец (gpt-5-mini), воронка, `/paid`/`/myticket`, amoJo-мост.
- `infra/` — systemd-юниты, nginx-vhost, datasette metadata.

## Прод
Сервер 31.56.196.8 (Ubuntu 24.04). nginx + Let's Encrypt. amoCRM = воронка; локальная SQLite `registrations.sqlite3` = операционная истина (Datasette `/admin/` + NocoDB). Билеты выдаёт менеджер `/paid` после подтверждения СБП-оплаты → QR+PDF в Telegram. Чек-ин: `/checkin` (html5-qrcode, single-use).

Секреты (токены, ключи) — в `/etc/sato*/.env` на сервере, НЕ в репо.
