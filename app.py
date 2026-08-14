from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, Response, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import uvicorn
import os
import re
import asyncio
from typing import Optional

from database import db
from bot_manager import bot_manager
import telebot.types as tg_types


# ─────────────────────────────────────────────────────────────
# HELPER: parse Telegram message links
# ─────────────────────────────────────────────────────────────

def parse_telegram_link(link: str):
    """
    Parse a Telegram message link and return (chat_id, message_id).
    Supports:
      https://t.me/c/1234567890/42       → private supergroup
      https://t.me/groupusername/42      → public group
    Returns (None, None) on failure.
    """
    link = link.strip()
    # Private supergroup: t.me/c/<channel_id>/<msg_id>
    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', link)
    if m:
        chat_id = int('-100' + m.group(1))
        msg_id  = int(m.group(2))
        return chat_id, msg_id
    # Public group: t.me/<username>/<msg_id>
    m = re.match(r'https?://t\.me/([a-zA-Z]\w+)/(\d+)', link)
    if m:
        username = '@' + m.group(1)
        msg_id   = int(m.group(2))
        return username, msg_id
    return None, None


# ─────────────────────────────────────────────────────────────
# APP SETUP
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(application: FastAPI):
    import os
    env_token = os.environ.get("BOT_TOKEN", "").strip()

    if env_token:
        # On Render: env token present — auto-start the bot unconditionally
        config = db.get_config()
        # Persist the token to DB if not already saved (first deploy)
        if not config.get("bot_token", "").strip():
            db.update_config("bot_token", env_token)
        # Sync OWNER_USERNAME from env
        env_owner = os.environ.get("OWNER_USERNAME", "").strip().replace("@", "")
        if env_owner and not config.get("owner_username", "").strip():
            db.update_config("owner_username", env_owner)
        # Sync SUPPORT_CHANNEL from env
        env_support = os.environ.get("SUPPORT_CHANNEL", "").strip().replace("@", "")
        if env_support and not config.get("support_channel", "").strip():
            db.update_config("support_channel", env_support)
        db.update_config("is_running", True)
        bot_manager.start_bot()
        db.log_event("[STARTUP] Bot auto-started from ENV token")
    else:
        # Local: reset stale running flag so it doesn’t auto-start with no token
        if db.get_config().get("is_running", False):
            db.update_config("is_running", False)

    yield
    bot_manager.stop_bot()


app = FastAPI(lifespan=lifespan, title="GroupBot Dashboard", version="8.0")

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")


# ─────────────────────────────────────────────────────────────
# HEALTH / PING — Required by Render, prevents 405 on health checks
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    # Touch the database so a keep-alive ping keeps the database (e.g. Neon)
    # awake even when the bot has no message traffic.
    db_alive = False
    try:
        if db._check_conn():
            c = db.conn.execute("SELECT 1")
            if hasattr(c, "fetchone"):
                c.fetchone()
            db_alive = True
    except Exception:
        db_alive = False
    return {"status": "ok", "database": "connected" if db_alive else "unavailable"}

@app.get("/ping")
async def ping():
    return {"pong": True}


# ─────────────────────────────────────────────────────────────
# MAIN DASHBOARD
# ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, search: Optional[str] = None):
    config = db.get_config()
    stats  = db.get_all_stats()

    if search:
        users, groups = db.search_items(search)
    else:
        users  = db.get_all_users()
        groups = db.get_all_groups()

    context = {
        "request":        request,
        "config":         config,
        "tracking_config": db.get_tracking_config(),
        "stats":          stats,
        "users":          users,
        "groups":         groups,
        "search_query":   search,
        "logs":           db.get_recent_logs(50),
        "warnings_board": db.get_warnings_leaderboard(),
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


# ─────────────────────────────────────────────────────────────
# API — LIVE STATS
# ─────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def api_stats():
    try:
        stats  = db.get_all_stats()
        config = db.get_config()
        stats["is_running"] = config.get("is_running", False)
        stats["bot_alive"]  = False
        if bot_manager.bot and bot_manager.thread and bot_manager.thread.is_alive():
            stats["bot_alive"] = True
        return stats
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/logs")
async def api_logs(limit: int = 50):
    return db.get_recent_logs(limit)


@app.get("/api/groups")
async def api_groups():
    return db.get_all_groups()


@app.get("/api/users")
async def api_users():
    return JSONResponse(content=db.get_all_users())


@app.get("/api/warnings")
async def api_warnings():
    return JSONResponse(content=db.get_warnings_leaderboard())



# ─────────────────────────────────────────────────────────────
# API — BOT TOGGLE
# ─────────────────────────────────────────────────────────────

@app.get("/api/toggle")
async def toggle_bot_get():
    """Redirect GET /api/toggle to dashboard (prevents 405 on browser refresh)"""
    return RedirectResponse(url="/", status_code=302)

@app.post("/api/toggle")
async def toggle_bot():
    config     = db.get_config()
    new_status = not config.get("is_running", False)
    db.update_config("is_running", new_status)

    if new_status:
        bot_manager.start_bot()
        db.log_event("🚀 Bot launched via dashboard")
    else:
        bot_manager.stop_bot()
        db.log_event("🛑 Bot stopped via dashboard")

    return RedirectResponse(url="/", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — SETTINGS
# ─────────────────────────────────────────────────────────────

@app.post("/api/settings")
async def update_settings(
    bot_token:        str = Form(""),
    owner_username:   str = Form(""),
    support_channel:  str = Form(""),
):
    owner_username   = owner_username.strip().replace("@", "")
    support_channel  = support_channel.strip().replace("@", "")
    token            = bot_token.strip()

    if token:
        db.update_config("bot_token", token)
    if owner_username:
        db.update_config("owner_username", owner_username)
    if support_channel is not None:
        db.update_config("support_channel", support_channel)

    db.log_event(f"⚙️ Config updated: owner={owner_username} support={support_channel}")

    config = db.get_config()
    if config.get("is_running", False):
        bot_manager.restart_bot()

    return RedirectResponse(url="/?success=SettingsSaved", status_code=303)

@app.post("/api/settings_alerts")
async def update_settings_alerts(
    msg_after_ban: str = Form(""),
    msg_after_kick: str = Form(""),
    msg_after_mute: str = Form(""),
    msg_not_admin: str = Form(""),
):
    if msg_after_ban:
        db.update_config("msg_after_ban", msg_after_ban.strip())
    if msg_after_kick:
        db.update_config("msg_after_kick", msg_after_kick.strip())
    if msg_after_mute:
        db.update_config("msg_after_mute", msg_after_mute.strip())
    if msg_not_admin:
        db.update_config("msg_not_admin", msg_not_admin.strip())
        
    db.log_event("⚙️ Custom moderation alerts updated")
    
    # Reload bot handlers with new config implicitly (since db.get_config is called on demand in bot_handlers, or if not we should restart. Wait, config is fetched inside handlers so we don't necessarily need to restart. But it's safer to restart to apply it globally immediately if cached, but there's no cache. Let's not restart bot for message changes to avoid disruption, though we could just to be safe. We'll leave it without restart since the bot queries db live.)
    return RedirectResponse(url="/?success=AlertsSaved", status_code=303)

# ─────────────────────────────────────────────────────────────
# API — BROADCAST
# ─────────────────────────────────────────────────────────────

@app.post("/api/broadcast")
async def broadcast(
    message: str = Form(""),
    target:  str = Form("groups"),
):
    if not bot_manager.bot:
        return RedirectResponse(url="/?error=BotNotRunning", status_code=303)

    try:
        targets = list(
            db.get_all_users().keys() if target == "users"
            else db.get_all_groups().keys()
        )
        count = 0
        for tid in targets:
            try:
                bot_manager.bot.send_message(
                    int(tid),
                    f"📢 <b>BROADCAST</b>\n\n{message}",
                    parse_mode="HTML"
                )
                count += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass

        db.log_event(f"📢 Broadcast sent to {count} {target}")
        return RedirectResponse(url=f"/?success=BroadcastSentTo{count}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/?error=BroadcastFailed:{str(e).replace(' ','_')}", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — GROUP SCANNER
# ─────────────────────────────────────────────────────────────

@app.post("/api/scan_group")
async def scan_group(group_id: str = Form("")):
    if not bot_manager.bot:
        return RedirectResponse(url="/?error=BotNotRunning", status_code=303)
    try:
        raw_id = group_id.strip()
        # Support username resolve
        if raw_id.startswith("@") or not raw_id.replace("-", "").isdigit():
            chat = bot_manager.bot.get_chat(raw_id)
            chat_id = chat.id
        else:
            chat_id = int(raw_id)
            chat = bot_manager.bot.get_chat(chat_id)

        member_count = bot_manager.bot.get_chat_member_count(chat_id)
        bot_member   = bot_manager.bot.get_chat_member(chat_id, bot_manager.bot.get_me().id)

        db.ensure_group(chat_id, name=getattr(chat, 'title', 'Unknown'))
        db.update_group_setting(chat_id, 'member_count', member_count)

        info = (f"Title: {chat.title} | Type: {chat.type} | "
                f"Members: {member_count} | Bot Status: {bot_member.status}")
        db.log_event(f"🔍 Scanned: {chat.title} ({chat_id}) — {member_count} members")
        return RedirectResponse(url=f"/?success=ScanComplete:_{info.replace(' ', '_')}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/?error=ScanFailed:{str(e).replace(' ','_')}", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — MESSAGE LINK CONTROL (delete / pin / unpin)
# ─────────────────────────────────────────────────────────────

@app.post("/api/message_action")
async def message_action_api(
    message_link: str = Form(""),
    action:       str = Form(""),
):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot is not running"}, status_code=400)

    chat_id, msg_id = parse_telegram_link(message_link.strip())
    if chat_id is None:
        return JSONResponse({"ok": False, "error": "Invalid message link. Use https://t.me/c/... or https://t.me/username/..."}, status_code=400)

    try:
        if action == "delete":
            bot_manager.bot.delete_message(chat_id, msg_id)
            label = "deleted"
        elif action == "pin":
            bot_manager.bot.pin_chat_message(chat_id, msg_id)
            label = "pinned"
        elif action == "unpin":
            bot_manager.bot.unpin_chat_message(chat_id, msg_id)
            label = "unpinned"
        else:
            return JSONResponse({"ok": False, "error": "Invalid action. Use delete/pin/unpin"}, status_code=400)

        db.log_event(f"🔗 Remote {label}: {message_link}")
        return JSONResponse({"ok": True, "action": label})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — ANTISPAM TOGGLE PER GROUP (from website)
# ─────────────────────────────────────────────────────────────

@app.post("/api/antispam_toggle")
async def antispam_toggle_group(chat_id: str = Form("")):
    group = db.get_group(chat_id.strip())
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    # Ensure it's treated as 1/0 for integer columns
    current = group.get("antispam", 0)
    new_status = 1 if not current else 0
    db.update_group_setting(chat_id.strip(), "antispam", new_status)
    db.log_event(f"🛡️ Antispam toggled {'ON' if new_status else 'OFF'} for group {chat_id} via dashboard")
    return JSONResponse({"ok": True, "antispam": bool(new_status)})


# ─────────────────────────────────────────────────────────────
# API — SEND MESSAGE TO A SPECIFIC GROUP
# ─────────────────────────────────────────────────────────────

@app.post("/api/group_message")
async def group_message(
    chat_id: str = Form(""),
    message: str = Form(""),
):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot is not running"}, status_code=400)
    if not chat_id.strip() or not message.strip():
        return JSONResponse({"ok": False, "error": "chat_id and message are required"}, status_code=400)
    try:
        bot_manager.bot.send_message(int(chat_id.strip()), message.strip(), parse_mode="HTML")
        db.log_event(f"📨 Direct message sent to group {chat_id}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — INTELLIGENCE SYNC
# ─────────────────────────────────────────────────────────────

@app.post("/api/sync_data")
async def sync_data(background_tasks: BackgroundTasks):
    if not bot_manager.bot:
        return RedirectResponse(url="/?error=BotNotRunning", status_code=303)
    background_tasks.add_task(perform_sync)
    return RedirectResponse(url="/?success=SyncStartedInBackground", status_code=303)


async def perform_sync():
    try:
        groups = list(db.get_all_groups().keys())
        for gid in groups:
            try:
                chat         = bot_manager.bot.get_chat(int(gid))
                member_count = bot_manager.bot.get_chat_member_count(int(gid))
                db.ensure_group(gid, name=getattr(chat, 'title', None))
                db.update_group_setting(gid, 'member_count', member_count)
                db.log_event(f"✅ Synced: {getattr(chat, 'title', gid)}")
            except Exception as e:
                db.log_event(f"⚠️ Sync failed ({gid}): {str(e)[:60]}")
            await asyncio.sleep(0.5)
        db.log_event(f"🧠 Sync complete — {len(groups)} groups")
    except Exception as e:
        import logging
        logging.error(f"Sync Error: {e}")


# ─────────────────────────────────────────────────────────────
# API — MANUAL ADD
# ─────────────────────────────────────────────────────────────

@app.post("/api/manual_add")
async def manual_add(
    type: str = Form(""),
    id:   str = Form(""),
    name: str = Form(""),
):
    try:
        int(id.strip())
    except ValueError:
        return RedirectResponse(url="/?error=InvalidIDFormat", status_code=303)

    try:
        if type == "user":
            db.ensure_user(id.strip(), name=name or "Unknown")
            db.log_event(f"➕ Manually added user {id} ({name})")
        elif type == "group":
            db.ensure_group(id.strip(), name=name or "Unknown Group")
            db.log_event(f"➕ Manually added group {id} ({name})")
        else:
            return RedirectResponse(url="/?error=InvalidType", status_code=303)
        return RedirectResponse(url="/?success=AddedManually", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/?error=AddFailed:{str(e).replace(' ','_')}", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — DELETE ENTRY
# ─────────────────────────────────────────────────────────────

@app.post("/api/delete_entry")
async def delete_entry(
    type: str = Form(""),
    id:   str = Form(""),
):
    try:
        if type == "user":
            db.delete_user(id)
            db.log_event(f"🗑️ Deleted user {id}")
        elif type == "group":
            db.delete_group(id)
            db.log_event(f"🗑️ Deleted group {id}")
        return RedirectResponse(url="/?success=DeletedSuccessfully", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/?error=DeleteFailed:{str(e).replace(' ','_')}", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — RESET WARNINGS
# ─────────────────────────────────────────────────────────────

@app.post("/api/reset_warnings")
async def reset_warnings(user_id: str = Form("")):
    try:
        db.reset_warnings(user_id.strip())
        db.log_event(f"🔄 Warnings reset for user {user_id}")
        return RedirectResponse(url="/?success=WarningsReset", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/?error=ResetFailed:{str(e).replace(' ','_')}", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — CLEAR LOGS
# ─────────────────────────────────────────────────────────────

@app.post("/api/clear_logs")
async def clear_logs():
    db.clear_logs()
    db.log_event("🧹 Logs cleared by admin")
    return RedirectResponse(url="/?success=LogsCleared", status_code=303)


# ─────────────────────────────────────────────────────────────
# API — REMOTE USER ACTION (ban/kick/mute/unmute/warn/promote/demote/gban/ungban/rep)
# ─────────────────────────────────────────────────────────────

@app.post("/api/remote_action")
async def remote_action(
    group_id:  str = Form(""),
    user_id:   str = Form(""),
    target_id: str = Form(""),
    action:    str = Form(""),
):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)

    # Support both user_id (dashboard selector) and target_id (user registry forms)
    raw_uid = user_id.strip() or target_id.strip()
    try:
        target_id_val = int(raw_uid)
        chat_id = int(group_id.strip()) if group_id.strip() else 0
        if action not in ("gban", "ungban", "repup", "repdown") and chat_id == 0:
            return JSONResponse({"ok": False, "error": "Group ID required"}, status_code=400)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid IDs"}, status_code=400)

    try:
        if action == "ban":
            bot_manager.bot.ban_chat_member(chat_id, target_id_val)
            label = "banned"
            db.log_event(f"🔨 Remote ban: user {target_id_val} in group {chat_id}")
        elif action == "unban":
            bot_manager.bot.unban_chat_member(chat_id, target_id_val, only_if_banned=True)
            label = "unbanned"
            db.log_event(f"🕊️ Remote unban: user {target_id_val} in group {chat_id}")
        elif action == "kick":
            bot_manager.bot.ban_chat_member(chat_id, target_id_val)
            bot_manager.bot.unban_chat_member(chat_id, target_id_val)
            label = "kicked"
            db.log_event(f"👢 Remote kick: user {target_id_val} in group {chat_id}")
        elif action == "mute":
            bot_manager.bot.restrict_chat_member(
                chat_id, target_id_val,
                permissions=tg_types.ChatPermissions(can_send_messages=False)
            )
            label = "muted"
            db.log_event(f"🔇 Remote mute: user {target_id_val} in group {chat_id}")
        elif action == "unmute":
            bot_manager.bot.restrict_chat_member(
                chat_id, target_id_val,
                permissions=tg_types.ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                )
            )
            label = "unmuted"
            db.log_event(f"🔊 Remote unmute: user {target_id_val} in group {chat_id}")
        elif action == "warn":
            warnings = db.add_warning(target_id_val)
            label    = f"warned ({warnings}/3)"
            if warnings >= 3:
                bot_manager.bot.ban_chat_member(chat_id, target_id_val)
                db.reset_warnings(target_id_val)
                label = "banned (3 warnings)"
            db.log_event(f"⚠️ Remote warn: user {target_id_val} in group {chat_id}")
        elif action == "promote":
            bot_manager.bot.promote_chat_member(chat_id, target_id_val,
                can_change_info=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_invite_users=True, can_restrict_members=True,
                can_pin_messages=True, can_promote_members=False)
            label = "promoted to admin"
            db.log_event(f"⏫ Remote promote: user {target_id_val} in group {chat_id}")
        elif action == "demote":
            bot_manager.bot.promote_chat_member(chat_id, target_id_val,
                can_change_info=False, can_post_messages=False, can_edit_messages=False,
                can_delete_messages=False, can_invite_users=False, can_restrict_members=False,
                can_pin_messages=False, can_promote_members=False)
            label = "demoted from admin"
            db.log_event(f"⏬ Remote demote: user {target_id_val} in group {chat_id}")
        elif action == "gban":
            db.global_ban_user(target_id_val, reason="Global banned via Dashboard")
            label = "global banned across all tracked groups"
            db.log_event(f"🔨 Global Ban: user {target_id_val}")
        elif action == "ungban":
            db.global_unban_user(target_id_val)
            label = "global unbanned"
            db.log_event(f"🕊️ Global Unban: user {target_id_val}")
        elif action == "repup":
            rep = db.update_reputation(target_id_val, 1)
            label = f"reputation increased to {rep}"
            db.log_event(f"⭐ Reputation (+1): user {target_id_val}")
        elif action == "repdown":
            rep = db.update_reputation(target_id_val, -1)
            label = f"reputation decreased to {rep}"
            db.log_event(f"📉 Reputation (-1): user {target_id_val}")
        else:
            return JSONResponse({"ok": False, "error": "Invalid action"}, status_code=400)

        return JSONResponse({"ok": True, "action": label})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — ANTI-LINK TOGGLE
# ─────────────────────────────────────────────────────────────

@app.post("/api/antilink_toggle")
async def antilink_toggle_group(chat_id: str = Form("")):
    group = db.get_group(chat_id.strip())
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    current = group.get("antispam_auto_delete_links", 0)
    new_status = 1 if not current else 0
    db.update_group_setting(chat_id.strip(), "antispam_auto_delete_links", new_status)
    db.log_event(f"🛡️ Anti-Link toggled {'ON' if new_status else 'OFF'} for group {chat_id} via dashboard")
    return JSONResponse({"ok": True, "antilink": bool(new_status)})


# ─────────────────────────────────────────────────────────────
# API — ACTIVE MODERATION LIST & REVOCATION (Unmute/Unban)
# ─────────────────────────────────────────────────────────────

@app.get("/api/active_moderation")
async def active_moderation():
    mutes = db.get_all_active_mutes()
    bans = db.get_all_active_bans()
    return JSONResponse({"mutes": mutes, "bans": bans})


@app.post("/api/unmute")
async def unmute_user_api(chat_id: str = Form(""), user_id: str = Form("")):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        cid = int(chat_id.strip())
        uid = int(user_id.strip())
        bot_manager.bot.restrict_chat_member(
            cid, uid,
            permissions=tg_types.ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True
            )
        )
        db.unmute_user(cid, uid)
        db.log_event(f"🔊 Remotely unmuted user {uid} in group {cid} via dashboard")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/unban")
async def unban_user_api(chat_id: str = Form(""), user_id: str = Form("")):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        cid = int(chat_id.strip())
        uid = int(user_id.strip())
        bot_manager.bot.unban_chat_member(cid, uid)
        db.unban_user(cid, uid)
        db.log_event(f"🕊️ Remotely unbanned user {uid} in group {cid} via dashboard")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — FILTERS & BAD WORDS MANAGEMENT
# ─────────────────────────────────────────────────────────────

@app.post("/api/add_bad_word")
async def add_bad_word_api(chat_id: str = Form(""), word: str = Form("")):
    cid = chat_id.strip()
    w = word.strip().lower()
    if not cid or not w:
        return JSONResponse({"ok": False, "error": "chat_id and word are required"}, status_code=400)
    group = db.get_group(cid)
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    bad_words = group.get("bad_words", [])
    if w not in bad_words:
        bad_words.append(w)
        db.update_group_setting(cid, "bad_words", bad_words)
        db.log_event(f"🤬 Added bad word '{w}' to group {cid} via dashboard")
    return JSONResponse({"ok": True, "bad_words": bad_words})


@app.post("/api/delete_bad_word")
async def delete_bad_word_api(chat_id: str = Form(""), word: str = Form("")):
    cid = chat_id.strip()
    w = word.strip().lower()
    if not cid or not w:
        return JSONResponse({"ok": False, "error": "chat_id and word are required"}, status_code=400)
    group = db.get_group(cid)
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    bad_words = group.get("bad_words", [])
    if w in bad_words:
        bad_words.remove(w)
        db.update_group_setting(cid, "bad_words", bad_words)
        db.log_event(f"🤬 Removed bad word '{w}' from group {cid} via dashboard")
    return JSONResponse({"ok": True, "bad_words": bad_words})


@app.post("/api/add_filter")
async def add_filter_api(chat_id: str = Form(""), trigger: str = Form(""), text: str = Form("")):
    cid = chat_id.strip()
    trig = trigger.strip().lower()
    txt = text.strip()
    if not cid or not trig or not txt:
        return JSONResponse({"ok": False, "error": "chat_id, trigger and text are required"}, status_code=400)
    group = db.get_group(cid)
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    filters = group.get("filters", {})
    filters[trig] = {"type": "text", "text": txt}
    db.update_group_setting(cid, "filters", filters)
    db.log_event(f"🔍 Added auto-reply filter '{trig}' to group {cid} via dashboard")
    return JSONResponse({"ok": True, "filters": filters})


@app.post("/api/delete_filter")
async def delete_filter_api(chat_id: str = Form(""), trigger: str = Form("")):
    cid = chat_id.strip()
    trig = trigger.strip().lower()
    if not cid or not trig:
        return JSONResponse({"ok": False, "error": "chat_id and trigger are required"}, status_code=400)
    group = db.get_group(cid)
    if not group:
        return JSONResponse({"ok": False, "error": "Group not found"}, status_code=404)
    filters = group.get("filters", {})
    if trig in filters:
        del filters[trig]
        db.update_group_setting(cid, "filters", filters)
        db.log_event(f"🔍 Removed auto-reply filter '{trig}' from group {cid} via dashboard")
    return JSONResponse({"ok": True, "filters": filters})


# ─────────────────────────────────────────────────────────────
# API — GROUP LOCK / UNLOCK
# ─────────────────────────────────────────────────────────────

@app.post("/api/group_lock")
async def group_lock(
    chat_id: str = Form(""),
    action:  str = Form("lock"),
):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        cid = int(chat_id.strip())
        if action == "lock":
            bot_manager.bot.set_chat_permissions(cid, tg_types.ChatPermissions(can_send_messages=False))
            db.log_event(f"🔒 Remote lock: group {cid}")
            return JSONResponse({"ok": True, "action": "locked"})
        elif action == "unlock":
            bot_manager.bot.set_chat_permissions(cid, tg_types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True
            ))
            db.log_event(f"🔓 Remote unlock: group {cid}")
            return JSONResponse({"ok": True, "action": "unlocked"})
        else:
            return JSONResponse({"ok": False, "error": "Use lock or unlock"}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — ADVANCED GROUP MANAGEMENT (Title, Description, Links)
# ─────────────────────────────────────────────────────────────

@app.post("/api/edit_group_title")
async def edit_group_title(chat_id: str = Form(""), title: str = Form("")):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        bot_manager.bot.set_chat_title(int(chat_id.strip()), title.strip())
        db.log_event(f"📝 Group title updated for {chat_id}: {title}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/edit_group_description")
async def edit_group_description(chat_id: str = Form(""), description: str = Form("")):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        bot_manager.bot.set_chat_description(int(chat_id.strip()), description.strip())
        db.log_event(f"📝 Group description updated for {chat_id}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/api/get_group_link")
async def get_group_link(chat_id: str):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        chat = bot_manager.bot.get_chat(int(chat_id.strip()))
        link = chat.invite_link or "No public link"
        return JSONResponse({"ok": True, "link": link})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.post("/api/create_group_link")
async def create_group_link(chat_id: str = Form("")):
    if not bot_manager.bot:
        return JSONResponse({"ok": False, "error": "Bot not running"}, status_code=400)
    try:
        link = bot_manager.bot.export_chat_invite_link(int(chat_id.strip()))
        db.log_event(f"🔗 New invite link created for {chat_id}")
        return JSONResponse({"ok": True, "link": link})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────────────────────
# API — DATABASE DOWNLOAD (manager.db)
# ─────────────────────────────────────────────────────────────

@app.get("/api/download_database")
async def download_database(ext: str = ""):
    """Download the manager.db SQLite database file (or its shm/wal files)"""
    try:
        from pathlib import Path
        
        # Prevent path traversal or invalid extensions
        if ext not in ["", "shm", "wal"]:
            ext = ""
            
        filename = f"manager.db-{ext}" if ext else "manager.db"
        db_file = Path(filename)

        if not db_file.exists():
            return JSONResponse(
                {"ok": False, "error": f"{filename} not found (this is normal if WAL isn't active)"},
                status_code=404
            )

        # Return the database file as a downloadable attachment
        return Response(
            content=db_file.read_bytes(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        db.log_event(f"❌ Database download error: {str(e)}")
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=500
        )


# ─────────────────────────────────────────────────────────────
# API — TRACKING SYSTEM
# ─────────────────────────────────────────────────────────────

@app.get("/api/tracking/status")
async def tracking_status():
    return JSONResponse(db.get_tracking_config())

@app.post("/api/tracking/destination")
async def set_tracking_destination(dest_id: str = Form("")):
    db.set_tracking_destination(dest_id.strip())
    db.log_event(f"👁️ Tracking destination set to {dest_id}")
    return RedirectResponse(url="/?success=TrackingDestinationUpdated", status_code=303)

@app.post("/api/tracking/add_user")
async def add_tracked_user(user_id: str = Form("")):
    uid = user_id.strip()
    if uid:
        db.add_tracked_user(uid)
        db.log_event(f"👁️ Started tracking user: {uid}")
    return RedirectResponse(url="/?success=UserTracked", status_code=303)

@app.post("/api/tracking/remove_user")
async def remove_tracked_user(user_id: str = Form("")):
    uid = user_id.strip()
    if uid:
        db.remove_tracked_user(uid)
        db.log_event(f"👁️ Stopped tracking user: {uid}")
    return RedirectResponse(url="/?success=UserTrackingStopped", status_code=303)

@app.post("/api/tracking/add_group")
async def add_tracked_group(group_id: str = Form("")):
    gid = group_id.strip()
    if gid:
        db.add_tracked_group(gid)
        db.log_event(f"👁️ Started tracking group: {gid}")
    return RedirectResponse(url="/?success=GroupTracked", status_code=303)

@app.post("/api/tracking/remove_group")
async def remove_tracked_group(group_id: str = Form("")):
    gid = group_id.strip()
    if gid:
        db.remove_tracked_group(gid)
        db.log_event(f"👁️ Stopped tracking group: {gid}")
    return RedirectResponse(url="/?success=GroupTrackingStopped", status_code=303)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
