# 🚀 Nexus Premium Dashboard - VPS Deployment Guide

Your codebase has been fully upgraded to **hardcode your specific environment variables**, meaning it is designed to be securely dropped onto any VPS and run instantly without fumbling with `.env` files or secret managers.

We have directly baked your Bot Token and Supabase credentials into `database.py`.

## 📦 What to Upload
Zip or upload the following core files to your VPS:
- `app.py`
- `bot_handlers.py`
- `bot_manager.py`
- `database.py`
- `requirements.txt`

*(You no longer need `manager.env` since the values are hardcoded in the scripts, but keep it somewhere safe just in case!)*

## 🛠️ Step 1: Install Dependencies
Connect to your VPS terminal, navigate to your bot folder, and run:
```bash
pip install -r requirements.txt
```

## 🌐 Step 2: Start the Engine
Since this is a unified ASGI application (FastAPI Dashboard + Bot Processor acting as a background thread), you run it entirely using Uvicorn.
Start the server on port `80` (or `8080`) using:
```bash
uvicorn app:app --host 0.0.0.0 --port 80
```

> **Note on background running**: If you want the bot to run even after you close your terminal, use a process manager like `screen`, `tmux`, or `pm2`:
> ```bash
> pm2 start "uvicorn app:app --host 0.0.0.0 --port 80" --name nexus-bot
> ```

## 🗄️ Step 3: Run the Database Migration
Make sure to paste and run the contents of `supabase_setup.sql` in your Supabase project's SQL Editor to make sure the database is capable of utilizing the new Approval Gate mechanisms.

---
### 🎉 You're Done!
Your premium remote control dashboard is now securely accessible from anywhere in the world by navigating to your VPS IP address in your browser!
