# pretix — развёртывание (self-hosted, Docker)

Прод: сервер `31.56.196.8`, субпуть `https://sadaosato.pro/pretix/`.

## 1. Контейнеры

```bash
mkdir -p /opt/pretix && cd /opt/pretix
cp <repo>/infra/pretix/docker-compose.yml .
cp <repo>/infra/pretix/pretix.cfg.example pretix.cfg
# заполнить secret (openssl rand -hex 32) и пароль БД (тот же в compose и cfg)
chown 15371:15371 pretix.cfg && chmod 640 pretix.cfg     # ВАЖНО: иначе HTTP 400
docker compose pull && docker compose up -d
# первый старт сам прогоняет миграции (~1–2 мин), затем gunicorn слушает :8345
```

## 2. nginx (субпуть, префикс снимается trailing-slash'ем)

```nginx
location = /pretix { return 301 /pretix/; }
location ^~ /pretix/ {                 # ^~ важен: бьёт regex статики
    proxy_pass http://127.0.0.1:8345/; # trailing slash снимает /pretix -> upstream видит /control/...
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 25M;
}
```
(Уже включено в `infra/nginx/sadaosato.pro`.)

## 3. Событие/товары/токен/устройства

Bootstrap-скрипт работает через `pretix shell` под `scopes_disabled()` (pretix
использует django-scopes — запросы к Event/Item требуют активного скоупа). Создаёт:
пользователя-админа, организатора `sato`, команду с `all_event_permissions` +
`all_organizer_permissions` (в этой версии вместо отдельных `can_*`), API-токен,
событие `ktk2026` (live), 4 товара по `internal_name`, безлимитную квоту,
чек-лист, устройство для pretixSCAN.

Проверка REST-связки:
```
POST /api/v1/organizers/sato/events/ktk2026/orders/        # payment_provider=manual -> статус n
POST .../orders/<code>/mark_paid/                          # -> статус p
POST .../checkinlists/1/positions/<SECRET>/redeem/         # вход; повтор -> reason=already_redeemed
```

## 4. Бэкенд

Переменные в `/etc/sato/amo.env`: `PRETIX_TOKEN`, `PRETIX_ORG=sato`,
`PRETIX_EVENT=ktk2026`, `PRETIX_HOST=sadaosato.pro`, `PRETIX_HTTP_HOST=127.0.0.1`,
`PRETIX_HTTP_PORT=8345`, `PRETIX_CHECKINLIST=1`. Модуль — `server/backend/pretix_link.py`.

## RAM

pretix(all)+postgres+redis ≈ 1.2 ГБ. На 4 ГБ-боксе перед установкой убрали NocoDB.
Бэкапы: `pg_dump` контейнера БД встроен в `infra/sato-backup.sh` (cron 04:00).
