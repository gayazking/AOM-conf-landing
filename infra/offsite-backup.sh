#!/usr/bin/env bash
# Офсайт-бэкап sato (NL → jump 217.60.5.92). Шифрование gpg (symmetric).
# Кладёт sato-YYYYMMDD-HHMM.tar.gz.gpg в jump:/var/backups/sato-offsite.
# Содержит: registrations.sqlite3, bot.sqlite3, amo_tokens.json, выгрузку сделок, pretix pg_dump.
set -euo pipefail

JUMP=root@217.60.5.92
JKEY=/root/.ssh/id_ed25519
REMOTE_DIR=/var/backups/sato-offsite
PASSFILE=/etc/sato/backup.pass
KEEP_LOCAL=7
KEEP_REMOTE=21
LOCAL_DIR=/var/lib/sato/offsite
TS=$(date +%Y%m%d-%H%M)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$LOCAL_DIR"

log(){ echo "[$(date '+%F %T')] $*"; }

# 1) SQLite (consistent online .backup, безопасно при WAL)
[ -f /var/lib/sato/registrations.sqlite3 ] && \
  sqlite3 /var/lib/sato/registrations.sqlite3 ".backup '$WORK/registrations.sqlite3'" || true
[ -f /var/lib/sato-bot/bot.sqlite3 ] && \
  sqlite3 /var/lib/sato-bot/bot.sqlite3 ".backup '$WORK/bot.sqlite3'" || true

# 2) amo-токены + свежая выгрузка сделок (backup_deals.py пишет JSON)
[ -f /var/lib/sato/amo_tokens.json ] && cp -a /var/lib/sato/amo_tokens.json "$WORK/" || true
if [ -f /opt/sato/backup_deals.py ]; then
  runuser -u www-data -- /opt/sato/venv/bin/python /opt/sato/backup_deals.py >/dev/null 2>&1 || true
fi
# подобрать любые выгрузки сделок (json/json.gz) из стандартных мест
mkdir -p "$WORK/deals"
find /var/lib/sato /var/lib/sato/backups -maxdepth 2 \( -iname 'deal*.json*' -o -iname '*deals*.json*' \) 2>/dev/null \
  -exec cp -a {} "$WORK/deals/" \; || true

# 3) pretix postgres dump
if docker ps --format '{{.Names}}' | grep -q '^pretix-pretix-db-1$'; then
  PGUSER=$(docker exec pretix-pretix-db-1 printenv POSTGRES_USER 2>/dev/null || echo pretix)
  PGDB=$(docker exec pretix-pretix-db-1 printenv POSTGRES_DB 2>/dev/null || echo pretix)
  docker exec pretix-pretix-db-1 pg_dump -U "$PGUSER" "$PGDB" > "$WORK/pretix.sql" 2>/dev/null || \
    log "WARN: pretix pg_dump failed"
fi

# 4) метаданные
{ echo "host=$(hostname)"; echo "ts=$TS"; echo "files:"; ls -la "$WORK"; } > "$WORK/MANIFEST.txt"

# 5) tar + gpg (symmetric)
ART="$LOCAL_DIR/sato-$TS.tar.gz.gpg"
tar czf - -C "$WORK" . | gpg --batch --yes --symmetric --cipher-algo AES256 \
  --passphrase-file "$PASSFILE" -o "$ART"
SZ=$(stat -c%s "$ART")
log "artifact $ART ($SZ bytes)"
[ "$SZ" -gt 1024 ] || { log "ERROR: artifact too small"; exit 1; }

# 6) отправка на jump
ssh -i "$JKEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25 "$JUMP" "mkdir -p $REMOTE_DIR"
rsync -e "ssh -i $JKEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=25" -a "$ART" "$JUMP:$REMOTE_DIR/"
log "uploaded to $JUMP:$REMOTE_DIR/"

# 7) ретенция (локально + на jump)
ls -1t "$LOCAL_DIR"/sato-*.tar.gz.gpg 2>/dev/null | tail -n +$((KEEP_LOCAL+1)) | xargs -r rm -f
ssh -i "$JKEY" -o ConnectTimeout=25 "$JUMP" \
  "ls -1t $REMOTE_DIR/sato-*.tar.gz.gpg 2>/dev/null | tail -n +$((KEEP_REMOTE+1)) | xargs -r rm -f"
log "done"
