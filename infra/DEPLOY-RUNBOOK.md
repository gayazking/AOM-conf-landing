# DEPLOY-RUNBOOK — Казань-Токио 2026 (sadaosato.pro)

Полный рецепт подъёма всего стека с нуля на чистом Ubuntu 24.04. Проверено при
аварийном переезде (Timeweb→NL). Все секреты — в `docs/CREDENTIALS.md` (не в git).

## Архитектура (микросервисы)
| Сервис | Где | Порт | Юнит |
|---|---|---|---|
| Лендинг (статика) | `/var/www/sadaosato` | nginx 80/443 | nginx |
| Backend API (Flask+gunicorn) | `/opt/sato` (`app:app`) | 127.0.0.1:8081 | `sato-api` |
| Telegram-бот (aiogram) | `/opt/sato-bot` | — (long-poll) | `sato-bot` |
| Datasette (GUI БД) | `/var/lib/sato/registrations.sqlite3` | 127.0.0.1:8001 | `datasette` |
| pretix (билеты, docker) | `/opt/pretix` | 127.0.0.1:8345 | docker compose |
| amoCRM | облако | — | токен в `/var/lib/sato/amo_tokens.json` |

nginx проксирует: `/api/ /amo/` → 8081, `/r/` → 8081 (QR-трекинг), `/reg /checkin` → 8081,
`/admin/` → 8001 (basic-auth), `/` → статика. pretix — отдельный vhost `tickets.sadaosato.pro` → 8345.

## Быстрый подъём
```bash
# 1. На рабочей машине: залить код и запустить bootstrap
scp -r server infra <repo-files> root@NEWIP:/root/sato-src/   # или git clone
# 2. Создать /root/sato-creds.env из docs/CREDENTIALS.md (см. шаблон в deploy.sh)
# 3. Запустить:
bash infra/deploy.sh        # идемпотентный, ставит весь стек
# 4. Вручную (см. ниже): amo-токен, SSL, pretix-bootstrap, DNS
```
Скрипт `infra/deploy.sh` делает: пакеты, юзеры (`sato-bot`), каталоги, venv'ы,
env-файлы из creds, nginx-vhost, init БД + `qrtrack.seed()`, systemd, datasette
basic-auth, cron, ufw, fail2ban.

## Ручные шаги (нельзя автоматизировать)

### 1. amoCRM токен (долгосрочный, до 2030)
JWT лежит в `docs/CREDENTIALS.md`. На сервере:
```bash
printf '%s' '<AMO_LONG_JWT>' > /tmp/amo_token && chown www-data /tmp/amo_token
cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python set_token.py
# должно: TOKEN_SET exp=1903824000, pipelines_status=200
```
Reverse-webhook (после того как домен указывает на сервер):
```bash
cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python -c "
import app;c=app.load_env()
url='https://sadaosato.pro/amo/lead-webhook?key='+c['AMO_WEBHOOK_SECRET']
print(app.amo_request(c,'POST','/api/v4/webhooks',{'destination':url,'settings':['status_lead']}).status_code)"
```

### 2. SSL (Let's Encrypt)
**НЕ-RU сервер (NL/EU) — предпочтительно `certbot --nginx` (HTTP-01, авто-renew):**
```bash
certbot --nginx --non-interactive --agree-tos -m admin@sadaosato.pro --redirect \
  -d sadaosato.pro -d www.sadaosato.pro
certbot --nginx --non-interactive --agree-tos -m admin@sadaosato.pro --redirect -d tickets.sadaosato.pro
# certbot.timer продлевает сам; проверка: certbot certificates
```
**RU-сервер:** HTTP-01 часто не проходит (гео/edge режет :80 для валидаторов).
Тогда **DNS-01** (acme.sh, ручной TXT, БЕЗ авто-renew):
```bash
curl https://get.acme.sh | sh -s email=admin@sadaosato.pro
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
~/.acme.sh/acme.sh --issue --dns -d sadaosato.pro -d www.sadaosato.pro --yes-I-know-dns-manual-mode-enough-go-ahead-please
# добавить выданные TXT _acme-challenge в reg.ru, подождать, затем:
~/.acme.sh/acme.sh --renew -d sadaosato.pro --yes-I-know-dns-manual-mode-enough-go-ahead-please
~/.acme.sh/acme.sh --install-cert -d sadaosato.pro --ecc \
  --fullchain-file /etc/ssl/sato/fullchain.pem --key-file /etc/ssl/sato/privkey.pem \
  --reloadcmd "systemctl reload nginx"
```
Альтернатива при переезде: скопировать `/etc/ssl/sato/{fullchain,privkey}.pem` со старого сервера (валиден ~90 дн).
`tickets.sadaosato.pro` — отдельный сертификат тем же DNS-01.

### 3. pretix (docker)
```bash
curl -fsSL https://get.docker.com | sh
# RU-сервер: добавить mirror (docker hub режет анонимные pull):
echo '{"registry-mirrors":["https://huecker.io","https://mirror.gcr.io"]}' > /etc/docker/daemon.json
systemctl restart docker
cd /opt/pretix   # docker-compose.yml + pretix.cfg (secret+db pw, chown 15371:15371 pretix.cfg, chmod 640)
docker compose pull && docker compose up -d   # первый старт ~2 мин (миграции)
# bootstrap события/товаров/токена — см. infra/pretix/README.md (pretix shell, scopes_disabled)
# новый API-токen → PRETIX_TOKEN в /etc/sato/amo.env
```
RAM<2ГБ → добавить swap: `fallocate -l 3G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile`.

### 4. DNS (reg.ru)
A `sadaosato.pro`, `www`, `tickets.sadaosato.pro` → IP сервера. AAAA при наличии IPv6.

### 5. Сквозная аналитика Яндекс.Метрика ↔ amoCRM
```bash
# создать кастом-поля сделки в amo (идемпотентно), вывести AMO_CF_*=<id>:
cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python create_amo_fields.py
# вписать выведенные AMO_CF_* + блок Метрики в /etc/sato/amo.env:
#   YANDEX_METRIKA_COUNTER=109717932 / _TARGET=purchase / _CURRENCY=EUR / _TOKEN=<oauth>
systemctl restart sato-api   # app.py на старте сам прогонит reg.init() (ALTER ym_client_id/yclid/page_url)
```
- ym_client_id ловится на сайте: сниппет `__satoYmPatched` в index.html (исходник — `tracking.js`).
  При пересборке бандла он уже внутри `Казань-Токио-2026-TRACKED.html`.
- Офлайн-конверсии (amo→Метрика при WIN) включаются токеном `YANDEX_METRIKA_TOKEN` —
  получить по `docs/ЯНДЕКС-МЕТРИКА-ТОКЕН.md`. Без токена — no-op. В Метрике создать
  цель JS-событие с id `purchase`.

## Уроки/грабли (КРИТИЧНО — экономит часы)
1. **Timeweb (и часть RU-хостингов) режут payload на 80/443 на edge** (TCP-handshake проходит, данные дропаются; :22 работает). Диагностика: `tcpdump -ni any "host <IP> and port 80"` — видно только SYN/SYN-ACK/ACK, без данных; `mtr` 0% потерь. Лечится только на стороне хостинга (тикет) или переездом. **NL/EU-сервер проблемы не имеет.**
2. **RU-сети режут TLS 1.3** (частично). Если сайт «то работает, то нет» из РФ — в nginx `ssl_protocols TLSv1.2;`.
3. **RU-сервер + чужой IP не подключается** — гео-блок иностранных. Ходить через РФ-jump (Tailscale) или РФ-IP. Для долгих сессий ставить ssh-ключ/ACL accept, иначе fail2ban/ре-авторизации рвут.
4. **docker hub rate-limit** на анонимных pull → registry-mirror (см. выше).
5. **systemd**: после rsync юнитов — `systemctl daemon-reload` ПЕРЕД `enable --now`, иначе «inactive, no entries».
6. **/etc/sato/amo.env** читается www-data → `chown root:www-data`, `chmod 640`, каталог `750`.
7. **ispmanager** на сервере занимает 80/apache → `systemctl disable --now apache2 ihttpd`, чистить `sites-enabled`, свой vhost `default_server`.
8. amo-токен — **долгосрочный** (set_token.py), не OAuth-код (тот 20-мин — неактуально).

## Восстановление данных
- **amoCRM (облако) — источник истины** по сделкам/контактам, переживает смерть сервера.
- `registrations.sqlite3` (локальная операционка), `bot.sqlite3`, pretix-заказы — только на сервере. Локальный бэкап: `infra/sato-backup.sh` (cron 04:00) + `backup_deals.py` (выгрузка всех сделок, cron 03:00).
- **Офсайт-бэкап (КРИТИЧНО — уже потеряли старую стату из-за его отсутствия):** `infra/offsite-backup.sh` на NL (cron 05:30) шифрует gpg (AES256) и шлёт `registrations.sqlite3`+`bot.sqlite3`+`amo_tokens.json`+выгрузку сделок+`pretix.sql` на jump `217.60.5.92:/var/backups/sato-offsite/` по ssh-ключу. Passphrase + восстановление — в `docs/CREDENTIALS.md` (раздел «Офсайт-бэкап»). При переносе на новый сервер: завести `/root/.ssh/id_ed25519`, прописать pubkey в jump `authorized_keys`, положить `/etc/sato/backup.pass`, поставить cron.
- Перед ЛЮБОЙ правкой прода — `backup_deals.py` (бэкап-выгрузка всех сделок).
