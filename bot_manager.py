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
            self.thread.join(timeout=8)

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

