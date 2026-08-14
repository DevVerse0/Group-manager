import threading
import telebot
from database import db
import logging
import time
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BotManager:
    def __init__(self):
        self.bot = None
        self.thread = None
        self._stop_event = threading.Event()
        self.scheduler_thread = None

    def start_bot(self):
        config = db.get_config()
        # Read token from env first, then fall back to DB
        token = os.environ.get("BOT_TOKEN", "").strip() or config.get("bot_token", "").strip()
        is_running = config.get("is_running", False)

        if not token:
            logger.warning("Bot token is not configured. Cannot start.")
            return False

        if not is_running:
            logger.info("Bot is set to stopped in config.")
            return False

        if self.thread and self.thread.is_alive():
            logger.info("Bot is already running.")
            return True

        try:
            self._stop_event.clear()
            self.bot = telebot.TeleBot(token, parse_mode=None)

            from bot_handlers import register_handlers
            register_handlers(self.bot)

            logger.info("Starting bot polling thread...")
            self.thread = threading.Thread(target=self._run_polling, daemon=True, name="BotPollingThread")
            self.thread.start()

            # Start auto-unmute/unban scheduler
            self.scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True, name="SchedulerThread")
            self.scheduler_thread.start()

            db.log_event("🚀 Bot engine started")
            return True
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            db.log_event(f"❌ Bot start error: {e}")
            self.bot = None
            return False

    def _run_polling(self):
        consecutive_conflicts = 0
        while not self._stop_event.is_set():
            try:
                logger.info("Polling started.")
                # We MUST include 'chat_member' in allowed_updates to receive status changes for all members in supergroups
                self.bot.infinity_polling(
                    timeout=20,
                    long_polling_timeout=15,
                    allowed_updates=['message', 'callback_query', 'chat_member', 'my_chat_member', 'chat_join_request']
                )
            except Exception as e:
                msg = str(e)
                if "409" in msg or "Conflict" in msg or "other getUpdates" in msg:
                    # Telegram allows exactly ONE getUpdates consumer per token.
                    consecutive_conflicts += 1
                    logger.error(
                        "Telegram polling conflict (409): another bot instance is already "
                        "receiving updates for this token. Only one process may call "
                        "getUpdates per token, so stop the other instance before this one "
                        "can run (check other terminal windows, the dashboard start/stop "
                        "toggle, or another deployed copy sharing BOT_TOKEN). "
                        f"Retrying in 30s. ({consecutive_conflicts} consecutive conflicts)"
                    )
                    if not self._stop_event.is_set():
                        time.sleep(30)
                    continue
                consecutive_conflicts = 0
                logger.error(f"Polling error: {e}")
                if not self._stop_event.is_set():
                    time.sleep(5)
        logger.info("Bot polling thread stopped.")

    def _run_scheduler(self):
        """Background scheduler for auto-unmute and auto-unban"""
        logger.info("Auto-unmute scheduler started.")
        while not self._stop_event.is_set():
            try:
                # Check for expired mutes
                expired_mutes = db.get_expired_mutes()
                for mute in expired_mutes:
                    try:
                        chat_id = int(mute['chat_id'])
                        user_id = int(mute['user_id'])

                        # Unmute in Telegram
                        self.bot.restrict_chat_member(
                            chat_id, user_id,
                            permissions=telebot.types.ChatPermissions(
                                can_send_messages=True,
                                can_send_audios=True,
                                can_send_documents=True,
                                can_send_photos=True,
                                can_send_videos=True,
                                can_send_video_notes=True,
                                can_send_voice_notes=True,
                                can_send_polls=True,
                                can_send_other_messages=True,
                                can_add_web_page_previews=True,
                            )
                        )
                        # Remove from database
                        db.unmute_user(chat_id, user_id)
                        logger.info(f"Auto-unmuted user {user_id} in chat {chat_id}")
                        db.log_event(f"🔊 Auto-unmute: {user_id} in {chat_id}")
                        try:
                            self.bot.send_message(chat_id, f"🔊 <b>Auto-Unmute:</b>\nUser <code>{user_id}</code> is now unmuted as their restriction time has expired.", parse_mode="HTML")
                        except Exception as e:
                            logger.error(f"Failed to send auto-unmute alert: {e}")
                    except Exception as e:
                        logger.error(f"Error auto-unmuting user: {e}")

                # Check for expired bans
                expired_bans = db.get_expired_bans()
                for ban in expired_bans:
                    try:
                        chat_id = int(ban['chat_id'])
                        user_id = int(ban['user_id'])

                        # Unban in Telegram
                        self.bot.unban_chat_member(chat_id, user_id)
                        # Remove from database
                        db.unban_user(chat_id, user_id)
                        logger.info(f"Auto-unbanned user {user_id} in chat {chat_id}")
                        db.log_event(f"🕊️ Auto-unban: {user_id} in {chat_id}")
                        try:
                            self.bot.send_message(chat_id, f"🕊️ <b>Auto-Unban:</b>\nUser <code>{user_id}</code> is now unbanned as their restriction time has expired.", parse_mode="HTML")
                        except Exception as e:
                            logger.error(f"Failed to send auto-unban alert: {e}")
                    except Exception as e:
                        logger.error(f"Error auto-unbanning user: {e}")

                # ── CAPTCHA: check pending captchas for kick/mute timeouts ──
                try:
                    from datetime import datetime, timedelta
                    import re as _re

                    def _parse_duration_secs(val):
                        """Parse duration string like '5m', '2h', '1d' to seconds."""
                        if not val:
                            return None
                        m = _re.match(r'^(\d+)([mhdwny]n?)$', str(val).strip().lower())
                        if not m:
                            return None
                        v, u = int(m.group(1)), m.group(2)
                        conv = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800, 'mn': 2592000, 'y': 31536000}
                        return v * conv.get(u, 0)

                    pending = db.get_all_pending_captchas()
                    now = datetime.utcnow()

                    for pc in pending:
                        try:
                            chat_id  = int(pc['chat_id'])
                            user_id  = int(pc['user_id'])
                            # Parse join_time stored as UTC string
                            join_str = pc.get('join_time', '')
                            try:
                                join_time = datetime.strptime(join_str, '%Y-%m-%d %H:%M:%S')
                            except:
                                continue
                            elapsed = (now - join_time).total_seconds()

                            group_data   = db.get_group(chat_id)
                            kick_enabled = group_data.get('captcha_kick', 0)
                            kick_time    = group_data.get('captcha_kick_time', '')
                            mute_time    = group_data.get('captcha_mute_time', '')

                            kick_secs = _parse_duration_secs(kick_time)
                            mute_secs = _parse_duration_secs(mute_time)

                            # Priority: kick > mute auto-unmute
                            if kick_enabled and kick_secs and elapsed >= kick_secs:
                                try:
                                    self.bot.ban_chat_member(chat_id, user_id)
                                    self.bot.unban_chat_member(chat_id, user_id)  # kick = ban then unban
                                    db.remove_pending_captcha(chat_id, user_id)
                                    db.log_event(f"👢 CAPTCHA kick: {user_id} from {chat_id} (didn't solve in time)")
                                    try:
                                        self.bot.send_message(
                                            chat_id,
                                            f"👢 <b>CAPTCHA Kick:</b> User <code>{user_id}</code> was kicked for not completing the CAPTCHA in time.",
                                            parse_mode="HTML"
                                        )
                                    except:
                                        pass
                                except Exception as e:
                                    logger.error(f"CAPTCHA kick failed for {user_id}: {e}")

                            elif mute_secs and elapsed >= mute_secs:
                                # Auto-unmute even if captcha not solved
                                try:
                                    self.bot.restrict_chat_member(
                                        chat_id, user_id,
                                        permissions=telebot.types.ChatPermissions(
                                            can_send_messages=True, can_send_audios=True,
                                            can_send_documents=True, can_send_photos=True,
                                            can_send_videos=True, can_send_video_notes=True,
                                            can_send_voice_notes=True, can_send_polls=True,
                                            can_send_other_messages=True, can_add_web_page_previews=True
                                        )
                                    )
                                    db.remove_pending_captcha(chat_id, user_id)
                                    db.log_event(f"🔊 CAPTCHA mute timeout auto-unmute: {user_id} in {chat_id}")
                                    try:
                                        self.bot.send_message(
                                            chat_id,
                                            f"🔊 <b>CAPTCHA Timeout:</b> User <code>{user_id}</code> was automatically unmuted after the CAPTCHA mute period expired.",
                                            parse_mode="HTML"
                                        )
                                    except:
                                        pass
                                except Exception as e:
                                    logger.error(f"CAPTCHA auto-unmute failed for {user_id}: {e}")
                        except Exception as e:
                            logger.error(f"CAPTCHA pending check error: {e}")
                except Exception as e:
                    logger.error(f"CAPTCHA scheduler error: {e}")

                # Run scheduler every 30 seconds
                time.sleep(30)
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                time.sleep(30)
        logger.info("Auto-unmute scheduler stopped.")

    def stop_bot(self):
        logger.info("Stopping bot...")
        self._stop_event.set()
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass

        if self.thread and self.thread.is_alive():
            # telebot's poller may be blocked inside a long-poll network call,
            # so give it enough time to fully release getUpdates before a restart.
            self.thread.join(timeout=15)

        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=5)

        self.bot = None
        self.thread = None
        self.scheduler_thread = None
        logger.info("Bot stopped.")
        return True

    def restart_bot(self):
        logger.info("Restarting bot...")
        self.stop_bot()
        time.sleep(1)
        return self.start_bot()


bot_manager = BotManager()

