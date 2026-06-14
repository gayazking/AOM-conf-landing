#!/usr/bin/env bash
# Idempotent full-stack deploy for sadaosato.pro on a clean Ubuntu 24.04.
# Run ON the server, FROM the repo root (where ./server and ./infra live):
#   sudo bash infra/deploy.sh
# Reads secrets from $SATO_CREDS (default /root/sato-creds.env). Template:
#
#   CLIENT_ID=...            CLIENT_SECRET=...
#   AMO_LONG_JWT=...         (долгосрочный amo-токен, для set_token)
#   BOT_TOKEN=...            ADMIN_IDS=...     OPENAI_API_KEY=...
#   PRETIX_TOKEN=...         AMOJO_CHANNEL_ID=... AMOJO_CHANNEL_SECRET=... AMOJO_SCOPE_ID=...
#   PAYMENT_URLS={...json...}
#   DATASETTE_ADMIN_PW=...   (опц., иначе сгенерится)
# Secrets-генераторы (INTERNAL_TOKEN, AMO_WEBHOOK_SECRET, TICKET_SECRET, STAFF_KEY)
# создаются автоматически, если не заданы. См. docs/CREDENTIALS.md.
set -uo pipefail
CREDS="${SATO_CREDS:-/root/sato-creds.env}"
[ -f "$CREDS" ] && . "$CREDS" || { echo "WARN: $CREDS не найден — env-файлы с плейсхолдерами"; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
gen(){ openssl rand -hex "${1:-32}"; }

echo "== пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx git python3-venv python3-pip build-essential sqlite3 \
  curl ufw fail2ban certbot python3-certbot-nginx rsync cron ca-certificates >/dev/null
# ispmanager (если есть) — отключить веб-стек
for s in apache2 ihttpd ispmanager ispmanager-ddos; do systemctl disable --now "$s" 2>/dev/null; done

echo "== юзеры + каталоги =="
id sato-bot >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin sato-bot
mkdir -p /opt/sato /opt/sato-bot /var/lib/sato /var/lib/sato-bot /etc/sato /etc/sato-bot \
  /etc/datasette /var/lib/sato/backups /var/www/sadaosato /opt/pretix /opt/datasette-venv /etc/ssl/sato

echo "== код =="
rsync -a --exclude __pycache__ --exclude '*.pyc' --exclude '*.deployed' --exclude '*.bak*' "$REPO/server/backend/" /opt/sato/
rsync -a --exclude __pycache__ --exclude '*.pyc' "$REPO/server/bot/" /opt/sato-bot/
cp -f "$REPO"/Казань-Токио-2026-TRACKED.html /var/www/sadaosato/index.html 2>/dev/null || true
cp -f "$REPO"/oferta.html "$REPO"/privacy.html "$REPO"/consent.html "$REPO"/tracking.js /var/www/sadaosato/ 2>/dev/null || true
cp -f "$REPO"/infra/systemd/*.service /etc/systemd/system/
cp -f "$REPO"/infra/metadata.json /etc/datasette/metadata.json
cp -f "$REPO"/server/backend/nginx-api.snippet /opt/sato/nginx-api.snippet
cp -f "$REPO"/infra/sato-backup.sh "$REPO"/infra/sato-health.sh /usr/local/bin/ 2>/dev/null; chmod +x /usr/local/bin/sato-*.sh 2>/dev/null
cp -f "$REPO"/infra/pretix/docker-compose.yml "$REPO"/infra/pretix/pretix.cfg.example /opt/pretix/ 2>/dev/null
chown -R www-data:www-data /opt/sato /var/lib/sato /var/www/sadaosato
chown -R sato-bot:sato-bot /opt/sato-bot /var/lib/sato-bot

echo "== venv =="
for v in "/opt/sato/venv www-data" "/opt/sato-bot/venv sato-bot" "/opt/datasette-venv www-data"; do
  set -- $v; [ -x "$1/bin/python" ] || python3 -m venv "$1"
done
/opt/sato/venv/bin/pip install -q -U pip wheel && /opt/sato/venv/bin/pip install -q -r /opt/sato/requirements.txt reportlab segno Pillow
/opt/sato-bot/venv/bin/pip install -q -U pip wheel && /opt/sato-bot/venv/bin/pip install -q -r /opt/sato-bot/requirements.txt
/opt/datasette-venv/bin/pip install -q -U pip wheel && /opt/datasette-venv/bin/pip install -q datasette
chown -R www-data:www-data /opt/sato/venv /opt/datasette-venv; chown -R sato-bot:sato-bot /opt/sato-bot/venv

echo "== env-файлы =="
ITOK="${INTERNAL_TOKEN:-$(gen 32)}"
cat > /etc/sato/amo.env <<EOF
SUBDOMAIN=gayazking
BASE_URL=https://gayazking.amocrm.ru
REDIRECT_URI=https://sadaosato.pro/amo/callback
CLIENT_ID=${CLIENT_ID:-__FILL__}
CLIENT_SECRET=${CLIENT_SECRET:-__FILL__}
INTERNAL_TOKEN=$ITOK
AMO_WEBHOOK_SECRET=${AMO_WEBHOOK_SECRET:-$(gen 24)}
AMO_RESPONSIBLE_USER=13874042
TICKET_SECRET=${TICKET_SECRET:-$(gen 32)}
STAFF_KEY=${STAFF_KEY:-$(gen 16)}
SUPPORT_PHONE=+7 986 848-78-25
VENUE=Казань, отель «Ривьера», ул. Фатыха Амирхана, 1А
EVENT_DATES=15–18 октября 2026
PIPELINE_ID=10957730
STATUS_ID=86154866
PRETIX_TOKEN=${PRETIX_TOKEN:-__FILL__}
PRETIX_ORG=sato
PRETIX_EVENT=ktk2026
PRETIX_HOST=tickets.sadaosato.pro
AMOJO_CHANNEL_ID=${AMOJO_CHANNEL_ID:-}
AMOJO_CHANNEL_SECRET=${AMOJO_CHANNEL_SECRET:-}
AMOJO_SCOPE_ID=${AMOJO_SCOPE_ID:-}
AMOJO_TITLE=SadaoSatoBot
EOF
cat > /etc/sato-bot/bot.env <<EOF
BOT_TOKEN=${BOT_TOKEN:-__FILL__}
CHANNEL_USERNAME=@sadaosato
ADMIN_IDS=${ADMIN_IDS:-}
MANAGER_CHAT_ID=${MANAGER_CHAT_ID:-${ADMIN_IDS:-}}
LEAD_API_URL=http://127.0.0.1:8081/api/lead
PAYMENT_URLS=${PAYMENT_URLS:-{} }
DB_PATH=/var/lib/sato-bot/bot.sqlite3
EVENT_DATE=2026-10-15
LEAD_TIMEOUT=12
SEND_RATE=25
OPENAI_API_KEY=${OPENAI_API_KEY:-}
AI_ENABLED=1
AI_MODE=hybrid
AI_MODEL=gpt-5-mini
AI_MAX_OUTPUT_TOKENS=700
AMOJO_ENABLED=1
AMOJO_OUTBOUND_URL=http://127.0.0.1:8081/amojo/outbound
INTERNAL_LISTEN=127.0.0.1:8082
INTERNAL_TOKEN=$ITOK
EOF
chown root:www-data /etc/sato /etc/sato/amo.env; chmod 750 /etc/sato; chmod 640 /etc/sato/amo.env
chown root:sato-bot /etc/sato-bot /etc/sato-bot/bot.env; chmod 750 /etc/sato-bot; chmod 640 /etc/sato-bot/bot.env

echo "== nginx (HTTP; SSL добавить после cert) =="
APW="${DATASETTE_ADMIN_PW:-$(openssl rand -base64 12 | tr -d '/+=' | head -c 14)}"
printf 'admin:%s\n' "$(openssl passwd -apr1 "$APW")" > /etc/nginx/.sato_admin
chmod 640 /etc/nginx/.sato_admin; chown root:www-data /etc/nginx/.sato_admin
rm -f /etc/nginx/sites-enabled/default
cat > /etc/nginx/sites-available/sadaosato.pro <<'NG'
server {
    listen 80 default_server; listen [::]:80 default_server;
    server_name sadaosato.pro www.sadaosato.pro _;
    root /var/www/sadaosato; index index.html;
    location = /reg { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location /r/ { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location = /checkin { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
    location /admin/ { auth_basic "Sato Admin"; auth_basic_user_file /etc/nginx/.sato_admin; proxy_pass http://127.0.0.1:8001; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
    location = /consent { try_files /consent.html =404; }
    location = /oferta { try_files /oferta.html =404; }
    location = /privacy { try_files /privacy.html =404; }
    include /opt/sato/nginx-api.snippet;
    location / { try_files $uri $uri/ /index.html; }
}
NG
ln -sf /etc/nginx/sites-available/sadaosato.pro /etc/nginx/sites-enabled/sadaosato.pro
nginx -t && systemctl reload nginx

echo "== БД + сервисы =="
cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python -c "import reg,qrtrack; reg.init(); qrtrack.seed()"
systemctl daemon-reload
systemctl enable --now sato-api datasette
[ -n "${BOT_TOKEN:-}" ] && systemctl enable --now sato-bot

echo "== amo-токен =="
if [ -n "${AMO_LONG_JWT:-}" ]; then
  printf '%s' "$AMO_LONG_JWT" > /tmp/amo_token; chown www-data /tmp/amo_token
  cd /opt/sato && sudo -u www-data HOME=/var/lib/sato venv/bin/python set_token.py | grep -E 'TOKEN_SET|pipelines' || true
fi

echo "== cron + ufw + fail2ban =="
( crontab -l 2>/dev/null | grep -vE 'sato-backup|sato-health|backfill.py|merge_tasks.py|backup_deals.py'
  echo "0 4 * * * /usr/local/bin/sato-backup.sh"
  echo "*/5 * * * * /usr/local/bin/sato-health.sh"
  echo "*/5 * * * * runuser -u www-data -- /opt/sato/venv/bin/python /opt/sato/backfill.py >> /var/lib/sato/backfill.log 2>&1"
  echo "*/5 * * * * runuser -u www-data -- /opt/sato/venv/bin/python /opt/sato/merge_tasks.py >> /var/lib/sato/merge_tasks.log 2>&1"
  echo "0 3 * * * runuser -u www-data -- /opt/sato/venv/bin/python /opt/sato/backup_deals.py >> /var/lib/sato/backup_deals.log 2>&1"
) | crontab -
for p in 22 80 443; do ufw allow "$p"/tcp >/dev/null; done; yes | ufw enable >/dev/null 2>&1
printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1\n[sshd]\nenabled=true\n' > /etc/fail2ban/jail.local
systemctl restart fail2ban 2>/dev/null

echo
echo "===== ГОТОВО (база). Ручное добей: ====="
echo "datasette /admin/ пароль: $APW   (запиши в docs/CREDENTIALS.md)"
echo "1) SSL: acme.sh DNS-01 (см. DEPLOY-RUNBOOK.md §2) -> /etc/ssl/sato/, добавь 443-server в nginx vhost"
echo "2) pretix: docker + compose + bootstrap (§3)"
echo "3) amo reverse-webhook (после указания домена на сервер) (§1)"
echo "4) DNS A -> этот сервер"
echo "Проверка: curl -sk https://127.0.0.1/api/health -H 'Host: sadaosato.pro'"
