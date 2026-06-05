# pretix — развёртывание (self-hosted, Docker)

Прод: сервер `31.56.196.8`, **отдельный поддомен** `https://tickets.sadaosato.pro/`.

> ⚠️ pretix НЕ умеет работать в субпути (`/pretix/`): он не выставляет
> `FORCE_SCRIPT_NAME`, поэтому редиректы/`STATIC_URL=/static/` идут без префикса,
> а `/api/` конфликтует с бэкендом. Поэтому — отдельный origin (поддомен на 443).
> Порт `:8443` не годится: хостинг блокирует не-443 снаружи.

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

## 2. nginx (отдельный vhost поддомена, БЕЗ снятия префикса)

`infra/nginx/tickets.sadaosato.pro` — vhost проксирует корень origin на pretix:

```nginx
server {
    server_name tickets.sadaosato.pro;
    listen 443 ssl;                       # cert от certbot --nginx -d tickets.sadaosato.pro
    location / {
        proxy_pass http://127.0.0.1:8345; # БЕЗ trailing slash — путь как есть
        proxy_set_header Host $host;       # = tickets.sadaosato.pro (ALLOWED_HOSTS)
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        client_max_body_size 25M;
    }
}
```

Cert: `certbot --nginx -d tickets.sadaosato.pro`. Старый `/pretix/` на главном
домене редиректит на поддомен (см. `infra/nginx/sadaosato.pro`). Бэкенд ходит в
API на loopback `127.0.0.1:8345` с `Host: tickets.sadaosato.pro` (env `PRETIX_HOST`).

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
