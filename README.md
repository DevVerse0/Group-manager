<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11"/>
  <img src="https://img.shields.io/badge/pyTelegramBotAPI-4.x-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="pyTelegramBotAPI"/>
  <img src="https://img.shields.io/badge/FastAPI-0.10x-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI"/>
  <img src="https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite"/>
  <img src="https://img.shields.io/badge/PostgreSQL-31648C?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL"/>
  <img src="https://img.shields.io/badge/Pillow-9.x-3776AB?style=for-the-badge&logo=pillow&logoColor=white" alt="Pillow"/>
</p>

<h1 align="center">🛡️ Ultimate Group Manager</h1>

<p align="center">
  <b>Your all-in-one Telegram Group Management Bot</b><br/>
  Powerful moderation, anti-spam protection, smart welcomes, CAPTCHA verification,
  chat rankings &amp; activity statistics — plus a full web dashboard.
</p>

<p align="center">
  Keep your community <b>safe, clean and under control</b> — one bot to rule them all. ⚡
</p>

---

## ✨ Highlights

| 🛡️ Protection | ⚔️ Moderation | 🎉 Engagement | 📊 Intelligence |
|---|---|---|---|
| Anti-Spam flood control | Ban / Kick / Mute / Warn | Smart welcome (text/photo/GIF) | Live leaderboard **image** + **public website** |
| Anti-Link (invite blocking) | Time-based ban & mute | Goodbye messages | `/rankings` · `/top` · `/mystats` |
| Bad words auto-filter | Promote / Demote | Custom auto-reply filters | `/chatstats` + milestones |
| CAPTCHA join verification | Lock / Unlock group | Group rules | Activity system panel |
| Keyword Alert (@admin/help) | **Removewarn / Resetwarn** | **View on Website** (public) | Per-group daily/weekly stats |
| **Ranking Anti-Spam** (flood → cooldown) | Pin / Unpin / Delete | `/report` admin alerts | **Live Dashboard Leaderboard** |

---

## 📖 Full Command List

### 🛡️ Anti-Spam & Protection
| Command | Description |
|---|---|
| `/antispam` | Toggle Anti-Spam (inline buttons) |
| `/antilink` | Toggle Anti-Link (inline buttons) |
| `/addbadword` | Add word to Auto-Mod filter |
| `/delbadword` | Remove word from filter |
| `/captcha` | CAPTCHA settings panel for new members |
| `/captchamutetime` | Auto-unmute time if CAPTCHA unsolved (e.g. `5m`) |
| `/captchakicktime` | Auto-kick time if CAPTCHA unsolved (e.g. `5m`) |
| `/setcaptchatext` / `/resetcaptchatext` | Custom / reset CAPTCHA button text |
| `/approve` | Toggle approve mode on/off |
| `/keywordalert` | Toggle Keyword Alert ON/OFF (admins) |
| `/setkeywords` | Set alert keywords (e.g. `@admin,help,support`) |

### ⚔️ Moderation
| Command | Description |
|---|---|
| `/ban` / `/unban` | Permanently ban / revoke ban |
| `/tban` / `/tmute` | Time-based ban / mute (e.g. `1d`, `2h`) |
| `/kick` | Remove user from group |
| `/mute` / `/unmute` | Restrict / restore talking |
| `/warn` | Issue a formal warning (3 = ban) |
| `/removewarn` / `/unwarn` / `/delwarn` | Remove 1 warning |
| `/resetwarn` / `/clearwarn` | Clear all warnings |
| `/mutelist` / `/banlist` | Show active mutes / bans |
| `/promote` / `/demote` | Grant / remove Administrator rights |
| `/lock` / `/unlock` | Lock / unlock the group |
| `/pin` / `/unpin` | Pin / unpin messages |
| `/del` | Delete a replied message |
| `/report` | Alert admins about a message |

### 🎉 Welcome & Engagement
| Command | Description |
|---|---|
| `/setwelcome` | Set welcome (reply to text/photo/GIF) |
| `/setleave` | Set goodbye (reply to text/photo/GIF) |
| `/addfilter` | Add auto-reply filter |
| `/removefilter` | Remove auto-reply filter |
| `/filters` | List active filters |
| `/rules` / `/setrules` | Show / set group rules |
| `/send` | Bot sends a custom message |

### 📊 Chat Activity & Rankings
| Command | Description |
|---|---|
| `/rankings` | Show the chat activity leaderboard (with View on Website) |
| `/top` | Alias for `/rankings` |
| `/mystats` | Your personal chat statistics |
| `/chatstats` | This group's chat statistics |
| `/activity` | Chat activity system settings (admins) |
| `/resetactivity` | Reset chat activity stats (admins) |

### ⚙️ Utility
| Command | Description |
|---|---|
| `/start` | Bot introduction |
| `/help` | Full command list |
| `/info` | User profile & stats |
| `/id` | Get User/Chat IDs (for Dashboard) |
| `/settitle` / `/setdesc` | Change group title / description |
| `/link` | Fetch group invite link |
| `/admins` | List all group admins |
| `/setgroup` | Group setup helper |

---

## 📊 Chat Activity & Rankings System

A fully independent module that plugs into the bot without touching any existing feature.

- ✅ **Message tracking** — overall / daily / weekly, persisted in SQLite/PostgreSQL, isolated per group
- 🏆 **Leaderboard image** — a dynamic dark/red PNG that auto-fits its height to the number of members
- 🌍 **Unicode font fallback** — renders Latin, Bangla, Devanagari, Arabic, Hebrew, Cyrillic, Hangul, CJK, emoji & flags correctly — **never blank** (fallback: display_name → username → ID → Unknown)
- 🎯 **Proportional bars** — top chatter is 100%, everyone else scaled relative to them
- 🔄 **Modes** — Overall / Today / Week + complete leaderboard with pagination buttons
- 🌐 **Public Website** — `GET /leaderboard?chat_id=-100xxx&mode=overall` — no login, non-admins can view; Telegram `🌐 View on Website` button (requires `PUBLIC_URL`)
- 📊 **Live Dashboard** — Data → **🏆 Leaderboard** with group selector, mode tabs, Top 10 table, pagination & image preview via `GET /api/rankings` & `GET /api/rankings/image`
- 🛡️ **Ranking Anti-Spam** — if a user sends > `RANK_SPAM_MAX` msgs within `RANK_SPAM_WINDOW` seconds, their msgs don't count for `RANK_SPAM_COOLDOWN` seconds (applies to everyone, even owner/admin) + group notification + **live dashboard control** (Settings → Ranking Anti-Spam)
- 👤 **`/mystats`** — your message totals + live group rank (ties share a rank; untracked users are *Unranked*)
- 👥 **`/chatstats`** — group totals, active members today, top chatter of the week
- 🎉 **Milestones** — user milestones (100 / 500 / 1K / 1.5K / 2K / 2.5K / 3K) and daily group milestones announced once
- 🎛️ **Admin panel** — `/activity` toggles tracking, milestones & the leaderboard globally or per group
- ⚙️ **Env-configurable** — `CHAT_ACTIVITY_ENABLED`, `CHAT_ACTIVITY_TIMEZONE` (default `Asia/Dhaka`), `RANK_SPAM_MAX` / `RANK_SPAM_WINDOW` / `RANK_SPAM_COOLDOWN` (default `4 / 4.0s → 10min`, dashboard overrides)

> 💡 Bot messages are never counted, group stats are fully isolated, and everything survives restarts. Works on both SQLite and PostgreSQL (Neon).

---

## 🚨 Keyword Alert System

- 🔑 **Trigger words** — per-group, default `@admin,admin,help,support` — fully customizable via `/setkeywords` or dashboard
- 👮 **Admin notify** — when a non-admin mentions a keyword, the bot tags all admins in the group **+ DMs each admin**
- ⏱️ **60s cooldown** per user per keyword per group — no spam
- 🎛️ **Toggle** — `/keywordalert` inline ON/OFF buttons + dashboard **Groups → Keyword** toggle + **Automod → 🚨 Keyword Alert** card
- 📊 **Dashboard** — `POST /api/keyword_alert/toggle` & `POST /api/keyword_alert/words`

---

## 🗂️ Project Structure

```
├── app.py                 # FastAPI dashboard + unified ASGI entry point
├── bot_manager.py         # Bot lifecycle: polling, 409-conflict handling, scheduler
├── bot_handlers.py        # All Telegram commands & handlers
├── chat_activity.py       # Rankings / activity system + leaderboard image generator
├── database.py            # DB layer (SQLite + PostgreSQL via DATABASE_URL, auto-migration)
├── templates/
│   ├── dashboard.html     # Main dashboard (Overview, Leaderboard, Groups, etc.)
│   └── leaderboard.html  # Public leaderboard (no auth)
├── .env.example           # Environment template
├── requirements.txt       # Python dependencies
├── render.yaml            # Render cloud deployment config
├── DEPLOY_GUIDE.md        # VPS / Render deployment guide
├── migrate_to_pg.py       # One-time SQLite → PostgreSQL migrator
└── manager.db             # SQLite database (auto-created if missing, ignored on PG)
```

---

## 🧰 Tech Stack

- **Language:** Python 3.11
- **Bot framework:** pyTelegramBotAPI 4.x
- **Backend:** FastAPI + Uvicorn (ASGI)
- **Database:** SQLite (local) / PostgreSQL (Neon) via `DATABASE_URL` — thread-safe singleton, auto-migration
- **Images:** Pillow (leaderboard rendering)
- **Security:** `captcha` package for join verification, `ADMIN_PASSWORD` for dashboard auth
- **Config:** `.env` + database-backed settings (`python-dotenv`)

---

## 🚀 Quick Start

```bash
# 1. Clone & install
pip install -r requirements.txt

# 2. Configure your bot (token in env or database)
#    copy template and fill:
cp .env.example .env
#    edit .env: BOT_TOKEN, OWNER_USERNAME, optional PUBLIC_URL/ADMIN_PASSWORD

# 3. Run everything (dashboard + bot in one process)
python -m uvicorn app:app --host 0.0.0.0 --port 8000
# Dashboard: http://localhost:8000
# Public leaderboard: http://localhost:8000/leaderboard?chat_id=-100xxx&mode=overall
```

> The database (`manager.db` or Neon) and all tables are created and migrated automatically on first run.

---

## ☁️ Deployment

- **Render** — see `render.yaml` (set `BOT_TOKEN`, `DATABASE_URL`, `PUBLIC_URL`, `ADMIN_PASSWORD` in the dashboard).
- **Railway** — same env vars, `up.railway.app` domain becomes `PUBLIC_URL`.
- **VPS** — full step-by-step guide in [`DEPLOY_GUIDE.md`](DEPLOY_GUIDE.md) (also covers `pm2`/`tmux` background running).

### Optional Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *(from DB)* | Your Telegram bot token |
| `DATABASE_URL` | *(SQLite)* | PostgreSQL URL (e.g. Neon). Leave unset for `manager.db` |
| `PUBLIC_URL` | *(none)* | Public base URL for Telegram “View on Website” buttons (e.g. `https://your-app.onrender.com`) — also accepts `LEADERBOARD_URL`, `RENDER_EXTERNAL_URL` |
| `ADMIN_PASSWORD` | *(open)* | Dashboard password — if set, `/` requires login at `/login` (also `DASHBOARD_PASSWORD`) |
| `CHAT_ACTIVITY_ENABLED` | `1` | Master switch for the whole Chat Activity system |
| `CHAT_ACTIVITY_TIMEZONE` | `Asia/Dhaka` | IANA timezone for daily/weekly leaderboards |
| `RANK_SPAM_MAX` | `4` | Max msgs before ranking cooldown (2-20, dashboard overrides) |
| `RANK_SPAM_WINDOW` | `4.0` | Window seconds for ranking anti-spam (1-60s) |
| `RANK_SPAM_COOLDOWN` | `600` | Cooldown seconds after flood (30-3600s, e.g. 600 = 10min) |
| `OWNER_USERNAME` | *(from DB)* | Bot owner username (no @) |
| `SUPPORT_CHANNEL` | *(none)* | Support channel username (no @) |

---

## 🛠️ Development Notes

- Handlers are registered through `register_handlers(bot)` / `register_chat_handlers(bot)`.
- `bot_manager.py` detects Telegram `409 Conflict` errors (duplicate polling instance) and backs off with a clear diagnostic instead of crash-looping.
- All dynamic user names in messages are HTML-escaped before rendering.
- The leaderboard image returns `None` when a group has no activity — the bot gracefully falls back to a text message.
- Ranking anti-spam is independent of moderation anti-spam — it only affects leaderboard counting, not message deletion.
- Public leaderboard (`/leaderboard`, `/lb`) is **no-auth** by design; admin dashboard (`/`) is protected by `ADMIN_PASSWORD` when set.

---

<p align="center">
  Made with ❤️ · <b>Ultimate Group Manager</b> — powerful tools, simple control, one bot.
</p>
