# SATO Summit Telegram Bot

Production Telegram bot for the dental summit **«Казань — Токио» — Международный
стоматологический саммит 2026** (headliner: prof. Sadao Sato).

- aiogram 3.x (async), Python 3.12, **long-polling** (single server, no webhook)
- Runs as a systemd service on Ubuntu 24.04 under its own venv at `/opt/sato-bot`
- SQLite (aiosqlite, WAL mode), APScheduler nurture funnel, amoCRM lead push via existing backend

## Flow

1. `/start` → branded greeting + consent line (links `https://sadaosato.pro/privacy`).
2. **Subscription gate** on `@sadaosato` (mandatory). Bot must be a **channel admin**.
3. **Registration FSM**: phone (shared contact only) → name → city/clinic. Saved to
   SQLite + pushed to the amoCRM lead backend (with on-failure retry queue). Phone is
   normalised to E.164 and the lead carries a stable `external_id` so amoCRM can dedupe.
4. **Main menu** (after registration): prices/packages, speakers, program, pay, remind,
   contact. Prices are visible only after registration.
5. **Nurture funnel** drip: +2h, +2d, +5d, +12d. Stops on payment (or the moment the
   user taps «Я оплатил»); survives restarts, including outages longer than a day.
6. **Admin**: `/stats`, `/broadcast`, `/paid <tg_id>`, one-tap payment confirmation.

No card data is ever collected or stored — payments are external links only.

## File layout

```
/opt/sato-bot/
  bot.py                 entry point (dispatcher, polling, error handler, lifecycle)
  config.py              env loading from /etc/sato-bot/bot.env (safe numeric parsing)
  content.py             ALL Russian copy + editable prices/packages + readiness flags
  db.py                  aiosqlite layer (users, scheduled_jobs, lead_queue), WAL
  lead.py                amoCRM POST + retry worker (dedupe key, PII-safe logging)
  funnel.py              APScheduler drip + boot re-arm + event reminder
  keyboards.py           inline/reply keyboards + callback data constants
  states.py              FSM StatesGroup
  subscription.py        get_chat_member gate (+ cooldown admin alert)
  handlers/
    __init__.py          builds the root router
    common.py            shared helpers (gate, menu, safe edits)
    start.py             /start + gate recheck
    registration.py      registration FSM
    menu.py              main-menu navigation
    payment.py           package selection, "Я оплатил", admin confirm, /paid
    admin.py             /stats, /broadcast
  requirements.txt
  README.md
/etc/sato-bot/bot.env(.example)
/etc/systemd/system/sato-bot.service
/var/lib/sato-bot/bot.sqlite3   (created at runtime)
```

## Editing content

The owner edits `content.py` and restarts the service:

- `PACKAGES` / `PACKAGE_ORDER` — titles, prices, descriptions.
- `SPEAKERS`, `PROGRAM` — currently placeholders. Once you fill in the real line-up
  and schedule, **also flip `SPEAKERS_READY = True` / `PROGRAM_READY = True`** so the
  funnel CTAs start pointing users to those screens (until then they point to prices).

Payment links live in `bot.env` (`PAYMENT_URLS`, a JSON map keyed by package key).
Optional `EVENT_DATE` and `EARLY_BIRD_DEADLINE` also live in `bot.env`.

## Deploy (Ubuntu 24.04)

```bash
# 1. System deps
sudo apt update && sudo apt install -y python3.12 python3.12-venv

# 2. Dedicated unprivileged service user (keeps the bot token away from the web stack)
sudo useradd --system --no-create-home --shell /usr/sbin/nologin sato-bot || true

# 3. Code
sudo mkdir -p /opt/sato-bot
sudo cp -r ./* /opt/sato-bot/            # or git clone into /opt/sato-bot
sudo python3.12 -m venv /opt/sato-bot/venv
sudo /opt/sato-bot/venv/bin/pip install --upgrade pip
sudo /opt/sato-bot/venv/bin/pip install -r /opt/sato-bot/requirements.txt

# 4. Data dir (writable by the service user)
sudo mkdir -p /var/lib/sato-bot
sudo chown -R sato-bot:sato-bot /var/lib/sato-bot
sudo chown -R root:root /opt/sato-bot        # code is read-only to the service

# 5. Config / secrets (root-owned, not readable by the service user's group)
sudo mkdir -p /etc/sato-bot
sudo cp /opt/sato-bot/../etc/sato-bot/bot.env.example /etc/sato-bot/bot.env
sudo nano /etc/sato-bot/bot.env          # set BOT_TOKEN, ADMIN_IDS, PAYMENT_URLS...
sudo chown root:root /etc/sato-bot/bot.env
sudo chmod 600 /etc/sato-bot/bot.env

# 6. systemd
sudo cp /opt/sato-bot/../etc/systemd/system/sato-bot.service /etc/systemd/system/sato-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now sato-bot
sudo systemctl status sato-bot
sudo journalctl -u sato-bot -f          # live logs
```

## Make the bot a channel admin (REQUIRED)

`get_chat_member` only works for arbitrary users if the bot is an admin of the channel.

### Via the Telegram app
1. Open the channel **@sadaosato** in Telegram.
2. **Manage Channel → Administrators → Add Admin**.
3. Search for **@sadaosato_bot** and add it. It needs no special powers — just admin
   membership (leave all toggles off; "manage messages" is harmless). Save.

### Via API (exact command, run from anywhere with the bot token)
There is no Bot API method to self-promote, so a human/owner must add the bot once
(step above). You can then **verify** the bot is an admin with:

```bash
# Replace $BOT_TOKEN; @sadaosato is the channel.
curl -s "https://api.telegram.org/bot$BOT_TOKEN/getChatAdministrators?chat_id=@sadaosato" \
  | python3 -m json.tool
# Look for the bot's user id with "status": "administrator".
```

If the bot is not an admin, the gate cannot verify subscriptions: users see a friendly
"временная ошибка, попробуйте позже" and the admins get a cooldown-throttled alert in
the logs and via direct message (re-alerts at most once per hour while the outage lasts).

## Operations

- Restart after editing content/config: `sudo systemctl restart sato-bot`
- Stats: send `/stats` to the bot as an admin.
- Broadcast: `/broadcast <текст>` or reply to a message with `/broadcast`.
- Confirm a payment: tap the admin confirm button, or `/paid <tg_id>` (idempotent —
  confirming twice does not re-message the user).
- The bot auto-restarts on crash (`Restart=always`) and re-arms funnel jobs from SQLite
  on every boot, so no drip messages are lost or duplicated — even after a multi-day outage.
