#!/bin/bash
ts=$(date +%Y%m%d-%H%M)
mkdir -p /root/backups
# pretix Postgres dump (gzipped)
if docker ps --format '{{.Names}}' | grep -q pretix-pretix-db-1; then
  docker exec pretix-pretix-db-1 pg_dump -U pretix pretix 2>/dev/null | gzip > /root/backups/pretix-db-$ts.sql.gz
fi
# app data + configs (incl. pretix compose/cfg/admin pw)
tar czf /root/backups/sato-data-$ts.tgz \
  /var/lib/sato/registrations.sqlite3 /var/lib/sato/leads.jsonl /var/lib/sato/amo_tokens.json /var/lib/sato/amojo_state.json \
  /var/lib/sato-bot/bot.sqlite3 /etc/sato /etc/sato-bot \
  /opt/pretix/docker-compose.yml /opt/pretix/pretix.cfg /opt/pretix/admin_pw.txt /opt/sato/registrars 2>/dev/null
# retention: keep last 14 of each
ls -1t /root/backups/sato-data-*.tgz 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t /root/backups/pretix-db-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
