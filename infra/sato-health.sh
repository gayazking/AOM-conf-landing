#!/bin/bash
# Health monitor: alert admin in Telegram on state CHANGE (no spam).
BOT_TOKEN=$(grep '^BOT_TOKEN=' /etc/sato-bot/bot.env | cut -d= -f2-)
ADMIN=1673538157
STATE=/var/lib/sato/health.state
f=""
systemctl is-active --quiet sato-api   || f+="• sato-api DOWN%0A"
systemctl is-active --quiet sato-bot   || f+="• sato-bot DOWN%0A"
systemctl is-active --quiet datasette  || f+="• datasette DOWN%0A"
systemctl is-active --quiet nginx      || f+="• nginx DOWN%0A"
curl -fsS -m5 http://127.0.0.1:8081/api/health >/dev/null 2>&1 || f+="• backend /api/health FAIL%0A"
pc=$(curl -s -m8 -o /dev/null -w '%{http_code}' -H 'Host: tickets.sadaosato.pro' http://127.0.0.1:8345/control/ 2>/dev/null)
[ "$pc" = "302" ] || [ "$pc" = "200" ] || f+="• pretix web FAIL ($pc)%0A"
[ "$(docker ps --format '{{.Names}}' | grep -c pretix)" -ge 3 ] || f+="• pretix containers <3%0A"
ext=$(curl -sk -m10 -o /dev/null -w '%{http_code}' https://tickets.sadaosato.pro/control/login/ 2>/dev/null)
[ "$ext" = "200" ] || [ "$ext" = "302" ] || f+="• tickets.sadaosato.pro ext FAIL ($ext)%0A"
disk=$(df / | awk 'NR==2{gsub("%","",$5);print $5}'); [ "${disk:-0}" -ge 90 ] && f+="• disk ${disk}%25%0A"
mem=$(free | awk '/Mem/{printf "%d",$3*100/$2}'); [ "${mem:-0}" -ge 92 ] && f+="• RAM ${mem}%25%0A"
cur=$(printf '%s' "$f" | md5sum | cut -d' ' -f1); prev=$(cat "$STATE" 2>/dev/null)
send(){ curl -s -m10 "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" -d chat_id=$ADMIN -d parse_mode=HTML --data "text=$1" >/dev/null; }
if [ -n "$f" ]; then
  [ "$cur" != "$prev" ] && send "🚨 <b>SATO мониторинг</b>%0A$f"
  echo "$cur" > "$STATE"
else
  [ -n "$prev" ] && [ "$prev" != "ok" ] && send "✅ <b>SATO</b>: всё восстановлено"
  echo ok > "$STATE"
fi
