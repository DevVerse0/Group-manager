import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from database import db
import logging
import time as _time
import collections
import html
import threading
import re
from datetime import timezone

logger = logging.getLogger(__name__)

# ── Rate-limit tracker: {(chat_id, user_id): deque of timestamps} ──
_msg_timestamps = collections.defaultdict(collections.deque)
_SPAM_MAX    = 5    # max messages
_SPAM_WINDOW = 3.0  # seconds

# --- Performance Caches ---
_ensured_groups = set() # {chat_id}
_ensured_users  = set() # {user_id}

class _Cache:
    """Thread-safe TTL cache for tracking handled events"""
    def __init__(self, ttl=30):
        self._store = {}
        self._lock = threading.Lock()
        self._ttl = ttl

    def get(self, key):
        with self._lock:
            entry = self._store.get(key)
            if entry and entry[1] > _time.time():
                return entry[0]
            self._store.pop(key, None)
            return None

    def set(self, key, value, ttl=None):
        with self._lock:
            self._store[key] = (value, _time.time() + (ttl or self._ttl))

_handled_events = _Cache(ttl=30)


# ─────────────────────────────────────────────────────────────
# DURATION PARSING & SPAM DETECTION HELPERS
# ─────────────────────────────────────────────────────────────

def parse_duration(duration_str):
    """
    Parse duration string to seconds.
    Formats: 1m (minute), 1h (hour), 1d (day), 1w (week), 1mn (month), 1y (year)
    Returns: (seconds, display_str) or (None, error_msg)
    """
    duration_str = str(duration_str).strip().lower()
    import re
    match = re.match(r'^(\d+)([mhdwny]n?)$', duration_str)
    if not match:
        return None, "Invalid format. Use: 1m, 1h, 1d, 1w, 1mn, 1y"

    value = int(match.group(1))
    unit = match.group(2)

    conversions = {
        'm': (60, 'minute'),
        'h': (3600, 'hour'),
        'd': (86400, 'day'),
        'w': (604800, 'week'),
        'mn': (2592000, 'month'),
        'y': (31536000, 'year')
    }

    if unit not in conversions:
        return None, f"Unknown unit: {unit}. Use m/h/d/w/mn/y"

    seconds, display = conversions[unit]
    total_seconds = value * seconds
    display_str = f"{value}{unit}"
    return total_seconds, display_str

def detect_promotional_spam(text):
    """
    Detect promotional spam patterns:
    - High emoji concentration (10+ same emoji)
    - Emoji + invite links
    Returns: (is_spam, reason)
    """
    if not text:
        return False, ""

    # Count emoji patterns - look for repeated Unicode characters that are typically emojis
    emoji_pattern = r'[\U0001F300-\U0001F9FF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2000-\u206F]'
    emojis = re.findall(emoji_pattern, text)

    if len(emojis) == 0:
        return False, ""

    # Check for emoji flooding (10+ emojis)
    if len(emojis) >= 10:
        # Count unique emojis vs total
        unique_ratio = len(set(emojis)) / len(emojis)
        if unique_ratio < 0.5:  # Less than 50% unique = repetitive
            return True, "Emoji flooding detected"

    # Check for emoji + link combo (promotional pattern)
    has_link = 't.me/' in text.lower() or 'telegram.me/' in text.lower() or 'telegram.dog/' in text.lower()
    if has_link and len(emojis) >= 5:
        return True, "Promotional spam detected (emoji + link)"

    return False, ""


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def is_admin(bot, chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['administrator', 'creator']
    except Exception:
        return False


def is_owner(username):
    config = db.get_config()
    owner = config.get("owner_username", "")
    if not username or not owner:
        return False
    return username.lower().replace("@", "") == owner.lower()


def can_act_on(bot, chat_id, executor_id, executor_username, target_id, target_username):
    if is_owner(executor_username):
        return True
    if is_owner(target_username):
        return False
    if is_admin(bot, chat_id, target_id):
        return False
    return True


ACCESS_DENIED_MSG = "👑 Access Denied! I need Admin Powers (can_promote_members) to do this.\nPromote me to unlock my full potential and keep the chat safe! 🦾"
USER_NOT_ADMIN_MSG = "❌ <b>You're not an admin!</b>\n\nOnly group admins can use this command."


def require_admin(bot, message):
    # First check if BOT itself is admin in the group
    try:
        bot_id = bot.get_me().id
        if not is_admin(bot, message.chat.id, bot_id):
            bot.reply_to(message, ACCESS_DENIED_MSG)
            return False
    except Exception:
        pass
    # Then check if USER is admin or owner
    if not is_admin(bot, message.chat.id, message.from_user.id) and not is_owner(message.from_user.username):
        bot.reply_to(message, USER_NOT_ADMIN_MSG, parse_mode="HTML")
        return False
    return True


def get_target_user(message):
    """
    Robustly identifies a target user from a message.
    Priority:
    1. Reply to a message
    2. Explicit numeric ID as first argument
    3. Explicit @username (must be in database)
    4. Text mentions (for users without usernames)
    """
    # 1. Reply
    if message.reply_to_message:
        return message.reply_to_message.from_user

    # Split message text into parts
    parts = message.text.split()
    if len(parts) > 1:
        target = parts[1].strip()

        # 2. Check for numeric ID
        # Support both positive and negative (though users are usually positive)
        # also handle cases like 12345:6789 (group:user) if needed, but usually just user id
        if target.lstrip('-').isdigit():
            try:
                user_id = int(target)
                # Check if we have seen this user before to get their name
                user_data = db.get_user(user_id)
                class TempUser:
                    def __init__(self, uid, name):
                        self.id = uid
                        self.first_name = name
                return TempUser(user_id, user_data.get('name', 'Unknown') if user_data else 'User')
            except ValueError:
                pass

        # 3. Check for @username
        if target.startswith("@"):
            uname = target[1:].lower()
            # Efficient lookup
            user_data = db.get_user_by_username(uname)
            if user_data:
                class TempUser:
                    def __init__(self, uid, name, username):
                        self.id = int(uid)
                        self.first_name = name
                        self.username = username
                return TempUser(user_data['user_id'], user_data.get('name', uname), uname)

    # 4. Fallback: Parse Entities (Mentions) — extract @username from text
    if message.entities:
        for entity in message.entities:
            if entity.type == 'text_mention' and entity.user:
                return entity.user
            if entity.type == 'mention' and hasattr(entity, 'user') and entity.user:
                return entity.user

    return None


def build_antispam_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"antispam:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"antispam:off:{chat_id}"
        )
    )
    return markup


def build_antilink_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"antilink:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"antilink:off:{chat_id}"
        )
    )
    return markup


def build_approve_markup(chat_id, is_on):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(
            "✅ ON" if is_on else "🔘 ON",
            callback_data=f"approve_toggle:on:{chat_id}"
        ),
        InlineKeyboardButton(
            "🔘 OFF" if is_on else "❌ OFF",
            callback_data=f"approve_toggle:off:{chat_id}"
        )
    )
    return markup


# ─────────────────────────────────────────────────────────────
# HANDLER REGISTRATION
# ─────────────────────────────────────────────────────────────

def register_handlers(bot):

    def _handle_join(chat_id, member, chat):
        # ── DEDUPLICATION CHECK ──
        event_key = f"join:{chat_id}:{member.id}"
        if _handled_events.get(event_key):
            return
        _handled_events.set(event_key, True, ttl=15)

        try:
            bot_id  = bot.get_me().id
            
            # Record user in database immediately — is_join=True increments join_count
            db.ensure_user(
                member.id, 
                name=f"{member.first_name or ''} {member.last_name or ''}".strip(), 
                username=member.username,
                chat_id=chat_id,
                chat_name=getattr(chat, 'title', None),
                is_join=True
            )

            if member.id == bot_id:
                db.ensure_group(chat_id, name=getattr(chat, 'title', None))
                db.log_event(f"✅ Bot added to group: {getattr(chat, 'title', 'Chat')} ({chat_id})")
                return

            group_data = db.get_group(chat_id)
            
            # --- APPROVAL MODE CHECK ---
            if group_data.get("approve_mode", False):
                if not db.is_user_approved(chat_id, member.id):
                    # They are unapproved, restrict them immediately
                    try:
                        bot.restrict_chat_member(
                            chat_id, member.id,
                            permissions=telebot.types.ChatPermissions(can_send_messages=False)
                        )
                    except Exception as e:
                        db.log_event(f"⚠️ Approval restrict failed: {e}")

                    # Build Approval Keyboard Regardless
                    markup = InlineKeyboardMarkup()
                    markup.row(
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_user:{chat_id}:{member.id}"),
                        InlineKeyboardButton("❌ Decline", callback_data=f"decline_user:{chat_id}:{member.id}")
                    )
                    
                    # Generate visible mentions for admins so they get heavily notified
                    try:
                        admins = bot.get_chat_administrators(chat_id)
                        tags = []
                        for ad in admins:
                            if not ad.user.is_bot:
                                name = html.escape(ad.user.first_name)
                                tags.append(f"<a href='tg://user?id={ad.user.id}'>@{ad.user.username}</a>" if ad.user.username else f"<a href='tg://user?id={ad.user.id}'>{name}</a>")
                        admin_mentions = "\n\n🔔 <b>Please Review:</b> " + " ".join(tags) if tags else ""
                    except:
                        admin_mentions = ""

                    try:
                        safe_name = html.escape(member.first_name or "User")
                        safe_uname = f"@{html.escape(member.username)}" if member.username else "<i>None</i>"
                        bot.send_message(
                            chat_id,
                            f"🛡️ <b>New Join Request!</b>\n\n"
                            f"👤 <b>Name:</b> {safe_name}\n"
                            f"🆔 <b>ID:</b> <code>{member.id}</code>\n"
                            f"🔗 <b>Username:</b> {safe_uname}\n\n"
                            f"This user is <b>MUTED</b> until approved by an administrator.{admin_mentions}",
                            reply_markup=markup,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        db.log_event(f"⚠️ Approval message failed: {e}")
                    
                    return # DO NOT SEND WELCOME MESSAGE YET
            # Default missing settings instead of returning early
            welcome_text    = group_data.get("welcome_message") or "Welcome, {name}! 👋"
            welcome_type    = group_data.get("welcome_type") or "text"
            welcome_file_id = group_data.get("welcome_file_id", "")
            
            # --- PERSONALIZATION ENGINE (ROBUST HTML ESCAPING) ---
            fname      = html.escape(str(member.first_name or "User"))
            name_html  = f"<b>{fname}</b>"
            # Usernames don't usually have special chars, but for safety we escape them too
            uname      = html.escape(str(member.username)) if member.username else "No Username"
            username   = f"@{uname}" if member.username else "No Username"
            g_title    = getattr(chat, 'title', None) or "this group"
            group_name = html.escape(str(g_title))
            from datetime import datetime
            date_now   = datetime.now().strftime("%Y-%m-%d")

            try:
                # Replace placeholders with properly escaped values
                text = (welcome_text
                        .replace("{name}", name_html)
                        .replace("{id}", str(member.id))
                        .replace("{username}", username)
                        .replace("{group}", group_name)
                        .replace("{date}", date_now))
            except Exception as e:
                text = f"Welcome, {name_html}!" # minimal fallback if replace fails

            try:
                if welcome_type == "photo" and welcome_file_id:
                    bot.send_photo(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
                elif welcome_type == "gif" and welcome_file_id:
                    bot.send_animation(chat_id, welcome_file_id, caption=text, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                db.log_event(f"⚠️ Welcome Error in {chat_id}: {e}")
                # Ultimate fallback: try sending without HTML parse mode or just text
                try: bot.send_message(chat_id, text, parse_mode="HTML")
                except: 
                    try: bot.send_message(chat_id, f"Welcome {member.first_name}!")
                    except: pass
        except Exception as e:
            db.log_event(f"🚨 Join Logic Error: {e}")

    def _handle_leave(chat_id, member, chat):
        # ── DEDUPLICATION CHECK ──
        event_key = f"leave:{chat_id}:{member.id}"
        if _handled_events.get(event_key):
            return
        _handled_events.set(event_key, True, ttl=15)

        try:
            bot_id  = bot.get_me().id
            
            # Update user info one last time (not a join, just a profile sync)
            db.ensure_user(
                member.id, 
                name=f"{member.first_name or ''} {member.last_name or ''}".strip(), 
                username=member.username,
                chat_id=chat_id,
                chat_name=getattr(chat, 'title', None)
            )

            if member.id == bot_id:
                db.delete_group(chat_id)
                db.log_event(f"❌ Bot removed from group: {chat_id}")
                return

            group_data = db.get_group(chat_id)
            # Default missing settings instead of returning early
            leave_text    = group_data.get("leave_message") or "Goodbye {name}!"
            leave_type    = group_data.get("leave_type") or "text"
            leave_file_id = group_data.get("leave_file_id", "")
            
            # --- PERSONALIZATION ENGINE (ROBUST HTML ESCAPING) ---
            fname      = html.escape(str(member.first_name or "User"))
            name_html  = f"<b>{fname}</b>"
            uname      = html.escape(str(member.username)) if member.username else "No Username"
            username   = f"@{uname}" if member.username else "No Username"
            g_title    = getattr(chat, 'title', None) or "this group"
            group_name = html.escape(str(g_title))
            from datetime import datetime
            date_now   = datetime.now().strftime("%Y-%m-%d")

            try:
                text = (leave_text
                        .replace("{name}", name_html)
                        .replace("{id}", str(member.id))
                        .replace("{username}", username)
                        .replace("{group}", group_name)
                        .replace("{date}", date_now))
            except Exception:
                text = f"Goodbye {name_html}!"

            try:
                if leave_type == "photo" and leave_file_id:
                    bot.send_photo(chat_id, leave_file_id, caption=text, parse_mode="HTML")
                elif leave_type == "gif" and leave_file_id:
                    bot.send_animation(chat_id, leave_file_id, caption=text, parse_mode="HTML")
                else:
                    bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                 db.log_event(f"⚠️ Leave Error in {chat_id}: {e}")
                 try: bot.send_message(chat_id, text, parse_mode="HTML")
                 except: pass
        except Exception as e:
            db.log_event(f"🚨 Leave Logic Error: {e}")

    # ── Group tracking & Welcome (Supergroups) ──
    @bot.chat_member_handler()
    def track_member_status(chat_member_update):
        chat_id = chat_member_update.chat.id
        IN_GROUP  = ['member', 'administrator', 'creator', 'restricted']
        OUT_GROUP = ['left', 'kicked']

        if chat_member_update.old_chat_member.status in OUT_GROUP and \
           chat_member_update.new_chat_member.status in IN_GROUP:
            _handle_join(chat_id, chat_member_update.new_chat_member.user, chat_member_update.chat)
        
        elif chat_member_update.old_chat_member.status in IN_GROUP and \
             chat_member_update.new_chat_member.status in OUT_GROUP:
            _handle_leave(chat_id, chat_member_update.old_chat_member.user, chat_member_update.chat)

    # ── Service Messages fallback (Basic Groups) ──
    @bot.message_handler(content_types=['new_chat_members', 'left_chat_member'])
    def service_message_handler(message):
        if message.new_chat_members:
            for member in message.new_chat_members:
                _handle_join(message.chat.id, member, message.chat)
        elif message.left_chat_member:
            _handle_leave(message.chat.id, message.left_chat_member, message.chat)


    def _broadcast_group(link, owner_id):
        """Send group invite to all users in database."""
        users = db.get_all_users()
        sent = 0
        failed = 0
        for uid in users:
            try:
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("🚀 Join Main Group", url=link))
                bot.send_message(
                    int(uid),
                    f"<b>🚀 Join Our Main Group!</b>\n\n"
                    f"Click the button below to join the main group:\n"
                    f"{link}",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                sent += 1
            except Exception:
                failed += 1
            _time.sleep(0.05)  # avoid rate limits
        try:
            bot.send_message(
                owner_id,
                f"✅ <b>Broadcast Complete!</b>\n"
                f"📨 Sent: {sent}\n"
                f"❌ Failed: {failed}",
                parse_mode="HTML"
            )
        except:
            pass

    # ── /setgroup ──
    @bot.message_handler(commands=['setgroup'])
    def cmd_setgroup(message):
        if message.chat.type != 'private':
            return bot.reply_to(message, "❌ This command only works in private chat.")
        if not is_owner(message.from_user.username):
            return bot.reply_to(message, "❌ Only the bot owner can use this command.")
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "❌ Usage: <code>/setgroup https://t.me/YourGroup</code>\n\nProvide your group's invite link.", parse_mode="HTML")
        link = parts[1].strip()
        if not link.startswith("https://t.me/"):
            return bot.reply_to(message, "❌ Invalid link. Must start with <code>https://t.me/</code>", parse_mode="HTML")
        db.update_config("main_group_link", link)
        bot.reply_to(message, f"✅ <b>Main Group Link Set!</b>\n\nNow inviting all {len(db.get_all_users())} users to join...\n\n{link}", parse_mode="HTML")
        threading.Thread(target=_broadcast_group, args=(link, message.from_user.id), daemon=True).start()

    # ── /start ──
    @bot.message_handler(commands=['start'])
    def cmd_start(message):
        uname = getattr(message.from_user, 'username', None)
        db.ensure_user(message.from_user.id, name=message.from_user.first_name, username=uname)
        db.log_event(f"User {message.from_user.first_name} ({message.from_user.id}) sent /start")
        if message.chat.type in ['group', 'supergroup']:
            db.ensure_group(message.chat.id, name=getattr(message.chat, 'title', None))

        config          = db.get_config()
        owner           = config.get("owner_username", "OwnerUser123")
        support_channel = config.get("support_channel", "").strip().replace("@", "")
        main_group_link = config.get("main_group_link", "").strip()
        bot_username    = bot.get_me().username

        markup = InlineKeyboardMarkup()

        # ── Row 1: Join Main Group (if set) ──
        if main_group_link:
            markup.row(
                InlineKeyboardButton(
                    "🚀 Join Our Main Group",
                    url=main_group_link
                )
            )

        # ── Row 2: Add Me to Your Group ──
        markup.row(
            InlineKeyboardButton(
                "➕ Add Me to Your Group",
                url=f"https://t.me/{bot_username}?startgroup=true"
            )
        )

        # ── Row 3: Commands & Help + Support Channel ──
        row3 = [InlineKeyboardButton("📜 Commands & Help", callback_data="show_help")]
        if support_channel:
            row3.append(InlineKeyboardButton("📢 Support Channel", url=f"https://t.me/{support_channel}"))
        markup.row(*row3)

        # ── Row 4: Contact Owner ──
        markup.row(InlineKeyboardButton("👤 Contact Owner", url=f"https://t.me/{owner}"))

        first = message.from_user.first_name
        text = (
            f"<b>✨ Hey {first}! Welcome to the Ultimate Group Manager</b>\n\n"
            "I'm your <b>premium all-in-one</b> Telegram group moderation bot. "
            "Add me to your group and take full control of your community.\n\n"
            "🛡️ <b>Core Features:</b>\n"
            "  ⚡ <b>Anti-Spam</b> — Flood control + invite link blocking\n"
            "  🤬 <b>Bad Words Filter</b> — Auto-delete + 3-strike warn/ban\n"
            "  🎉 <b>Smart Welcome</b> — Greet with photo, GIF or custom text\n"
            "  🔨 <b>Full Moderation</b> — Ban, kick, mute, warn, promote\n"
            "  📌 <b>Admin Tools</b> — Pin, lock, filters, rules & more\n"
            "  🌍 <b>Web Dashboard</b> — Remote control from anywhere\n\n"
            "<i>👇 Join the main group, add me to your group, and start managing!</i>"
        )
        try:
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            try:
                bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")
            except Exception:
                pass

    @bot.callback_query_handler(func=lambda call: call.data == "show_help")
    def help_callback(call):
        cmd_help(call.message)
        bot.answer_callback_query(call.id)

    # ── /help ──
    @bot.message_handler(commands=['help'])
    def cmd_help(message):
        text = ("<b>🛠️ Bot Commands List</b>\n\n"
                "<b>🛡️ Moderation:</b>\n"
                "• /ban - Permanent ban\n"
                "• /unban - Revoke ban\n"
                "• /tmute @user 1h [reason] - <b>Timed mute (NEW)</b>\n"
                "• /tban @user 1d [reason] - <b>Timed ban (NEW)</b>\n"
                "• /mutelist - Show active mutes <b>(NEW)</b>\n"
                "• /banlist - Show active bans <b>(NEW)</b>\n"
                "• /kick - Remove user (can rejoin)\n"
                "• /mute - Silence user\n"
                "• /unmute - Restore chat\n"
                "• /warn - Formal warning (3 = ban)\n\n"
                "<b>⏰ Duration Formats:</b>\n"
                "1m (minute), 1h (hour), 1d (day), 1w (week), 1mn (month), 1y (year)\n\n"
                "<b>👮 Admin Tools:</b>\n"
                "• /lock - Lock group (Admins only)\n"
                "• /unlock - Unlock group\n"
                "• /promote - Give admin rights\n"
                "• /demote - Remove admin rights\n"
                "• /settitle - Change group name\n"
                "• /setdesc - Change group description\n"
                "• /pin - Pin message\n"
                "• /unpin - Unpin all\n"
                "• /del - Delete message\n"
                "• /report - Alert group admins\n"
                "• /link - Get group invite link\n\n"
                "<b>🤖 Automation & Auto-Mod:</b>\n"
                "• /setwelcome - Set greeting (reply to text/photo/GIF)\n"
                "• /setleave - Set goodbye (reply to text/photo/GIF)\n"
                "• /setrules - Set group rules\n"
                "• /rules - Show group rules\n"
                "• /addfilter - Create auto-reply\n"
                "• /removefilter - Delete auto-reply\n"
                "• /filters - List active filters\n"
                "• /addbadword - Add word to auto-mod\n"
                "• /delbadword - Remove auto-mod word\n"
                "• /antispam - Toggle Anti-Spam (links & promo spam blocked)\n\n"
                "<b>📊 Information:</b>\n"
                "• /info - User profile & status\n"
                "• /admins - List all group admins\n"
                "• /send - Bot sends a message\n"
                "• /start - Bot introduction\n\n"
                "<b>💡 Examples:</b>\n"
                "/tmute @spammer 1h spam\n"
                "/tban @baduser 3d violation\n"
                "/warn → reply to user message")

        if message.chat.type == 'private':
            bot.send_message(message.chat.id, text, parse_mode="HTML")
        else:
            bot.reply_to(message, "📥 Help sent to your private messages!")
            try:
                bot.send_message(message.from_user.id, text, parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Please start me in private chat first so I can DM you the help menu.")

    # ── /info ──
    @bot.message_handler(commands=['info'])
    def cmd_info(message):
        db.ensure_user(message.from_user.id, name=message.from_user.first_name)
        target = get_target_user(message)

        if not target:
            target_user = message.from_user
        else:
            target_user = target

        user_data = db.get_user(target_user.id)
        warnings = user_data.get("warnings", 0) if user_data else 0
        role = "Member"
        status_text = "N/A"

        if message.chat.type in ['group', 'supergroup']:
            try:
                member_info = bot.get_chat_member(message.chat.id, target_user.id)
                if member_info.status == 'creator':
                    role = "👑 Owner / Creator"
                elif member_info.status == 'administrator':
                    role = "🛡️ Administrator"
                elif member_info.status == 'restricted':
                    role = "⚠️ Restricted User"
                if hasattr(target_user, 'username') and target_user.username:
                    status_text = f"@{target_user.username}"
            except Exception:
                pass

        if is_owner(getattr(target_user, 'username', None)):
            role = "⭐ Global Owner"

        text = (f"<b>👤 User Intelligence</b>\n\n"
                f"<b>📛 Name:</b> {target_user.first_name}\n"
                f"<b>🆔 ID:</b> <code>{target_user.id}</code>\n"
                f"<b>🎭 Role:</b> {role}\n"
                f"<b>🌐 Username:</b> {status_text}\n"
                f"<b>⚠️ Warnings:</b> {warnings}/3\n"
                f"<b>📅 First Seen:</b> {'Recorded' if user_data else 'New Arrival'}")
        bot.reply_to(message, text, parse_mode="HTML")

    # ── /admins ──
    @bot.message_handler(commands=['admins'])
    def cmd_admins(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        try:
            admins = bot.get_chat_administrators(message.chat.id)
            text = f"<b>🛡️ Administrators in {message.chat.title}</b>\n\n"
            for admin in admins:
                if admin.user.is_bot:
                    continue
                symbol = "👑" if admin.status == 'creator' else "🛡️"
                text += f"{symbol} {admin.user.first_name} (<code>{admin.user.id}</code>)\n"
            bot.reply_to(message, text, parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to fetch admin list: {e}")

    # ── /ban ──
    @bot.message_handler(commands=['ban'])
    def cmd_ban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to ban.")
        
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")
        try:
            bot.ban_chat_member(message.chat.id, t_id)
            db.log_event(f"Admin {message.from_user.id} banned {t_id} in {message.chat.id}")
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🔨 User <b>{name}</b> (<code>{t_id}</code>) has been <b>banned</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to ban: {str(e)}")

    # ── /kick ──
    @bot.message_handler(commands=['kick'])
    def cmd_kick(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to kick.")
        
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
            return
        try:
            bot.ban_chat_member(message.chat.id, t_id)
            bot.unban_chat_member(message.chat.id, t_id)
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"Booted 👢 User <b>{name}</b> (<code>{t_id}</code>) has been <b>kicked</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to kick: {str(e)}")

    # ── /mute ──
    @bot.message_handler(commands=['mute'])
    def cmd_mute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ <b>User not found in database or invalid.</b>\n\n"
                                        "✅ <b>Reply:</b> User er message e reply kore /mute likhun\n"
                                        "✅ <b>ID:</b> /mute 123456789\n"
                                        "✅ <b>Known User:</b> @username (user age message pathale)", parse_mode="HTML")
        
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return
        try:
            bot.restrict_chat_member(
                message.chat.id, t_id,
                permissions=telebot.types.ChatPermissions(can_send_messages=False)
            )
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🔇 User <b>{name}</b> (<code>{t_id}</code>) has been <b>muted</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /unmute ──
    @bot.message_handler(commands=['unmute'])
    def cmd_unmute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        
        t_id = target.id
        try:
            bot.restrict_chat_member(
                message.chat.id, t_id,
                permissions=telebot.types.ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_documents=True,
                    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                    can_send_voice_notes=True, can_send_polls=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                )
            )
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🔊 User <b>{name}</b> (<code>{t_id}</code>) has been <b>unmuted</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /unban ──
    @bot.message_handler(commands=['unban'])
    def cmd_unban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ <b>Identify User:</b> Reply to their message, type their 🆔, or Use their 👤 @username (if seen before).", parse_mode="HTML")

        t_id = target.id
        try:
            bot.unban_chat_member(message.chat.id, t_id, only_if_banned=True)
            db.unban_user(message.chat.id, t_id)
            db.log_event(f"Admin {message.from_user.id} unbanned {t_id} in {message.chat.id}")
            # Try to get a name for better feedback
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"🕊️ User <b>{name}</b> (<code>{t_id}</code>) has been <b>unbanned</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ <b>Unban Failed:</b>\n<code>{str(e)}</code>", parse_mode="HTML")

    # ── /tmute — TIME-BASED MUTE ──
    @bot.message_handler(commands=['tmute'])
    def cmd_tmute(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        parts = message.text.split()
        is_reply = message.reply_to_message is not None
        min_len = 2 if is_reply else 3

        if len(parts) < min_len:
            usage = (
                "💬 <b>Usage (Reply):</b> <code>/tmute 1h [reason]</code>\n"
                "💬 <b>Usage (No Reply):</b> <code>/tmute @user 1h [reason]</code>\n"
                "⏰ <b>Formats:</b> 1m, 1h, 1d, 1w, 1mn, 1y\n"
                "💡 <b>Examples:</b>\n"
                "  /tmute @user 1h spam\n"
                "  /tmute 30m duplicate posting"
            )
            return bot.reply_to(message, usage, parse_mode="HTML")

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to mute.")

        if is_reply:
            duration_str = parts[1]
            reason = " ".join(parts[2:]) if len(parts) > 2 else "No reason"
        else:
            duration_str = parts[2]
            reason = " ".join(parts[3:]) if len(parts) > 3 else "No reason"

        seconds, display = parse_duration(duration_str)
        if seconds is None:
            return bot.reply_to(message, f"❌ {display}")

        t_id = target.id
        name = getattr(target, 'first_name', str(t_id))

        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")

        try:
            # Delete the offending message if replied to
            if is_reply:
                try:
                    bot.delete_message(message.chat.id, message.reply_to_message.message_id)
                except Exception:
                    pass

            # Mute in Telegram natively
            until_date = int(_time.time() + seconds)
            bot.restrict_chat_member(
                message.chat.id, t_id,
                until_date=until_date,
                permissions=telebot.types.ChatPermissions(can_send_messages=False)
            )

            # ✅ Send alert IMMEDIATELY — before any DB ops that could fail
            try:
                bot.send_message(message.chat.id, f"🔇 {name} has been muted for {display}.")
            except Exception as alert_err:
                logger.error(f"tmute alert send failed: {alert_err}")

            # Log in database (non-critical — failure won't prevent alert)
            try:
                db.mute_user(message.chat.id, t_id, seconds, reason, message.from_user.first_name)
                db.log_infraction(message.chat.id, t_id, "tmute", seconds, reason, message.from_user.id)
            except Exception as db_err:
                logger.error(f"tmute DB log error: {db_err}")

        except Exception as e:
            logger.error(f"tmute failed: {e}")
            try:
                bot.send_message(message.chat.id, f"❌ Mute failed: {str(e)}")
            except Exception:
                pass

    # ── /tban — TIME-BASED BAN ──
    @bot.message_handler(commands=['tban'])
    def cmd_tban(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        parts = message.text.split()
        is_reply = message.reply_to_message is not None
        min_len = 2 if is_reply else 3

        if len(parts) < min_len:
            usage = (
                "⛔ <b>Usage (Reply):</b> <code>/tban 1d [reason]</code>\n"
                "⛔ <b>Usage (No Reply):</b> <code>/tban @user 1d [reason]</code>\n"
                "⏰ <b>Formats:</b> 1m, 1h, 1d, 1w, 1mn, 1y\n"
                "💡 <b>Examples:</b>\n"
                "  /tban @user 1w advertising\n"
                "  /tban 12h flood"
            )
            return bot.reply_to(message, usage, parse_mode="HTML")

        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username to ban.")

        if is_reply:
            duration_str = parts[1]
            reason = " ".join(parts[2:]) if len(parts) > 2 else "No reason"
        else:
            duration_str = parts[2]
            reason = " ".join(parts[3:]) if len(parts) > 3 else "No reason"

        seconds, display = parse_duration(duration_str)
        if seconds is None:
            return bot.reply_to(message, f"❌ {display}")

        t_id = target.id
        name = getattr(target, 'first_name', str(t_id))

        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot perform this action on this user (Admin/Owner protection).")

        try:
            # Delete the offending message if replied to
            if is_reply:
                try:
                    bot.delete_message(message.chat.id, message.reply_to_message.message_id)
                except Exception:
                    pass

            # Ban in Telegram natively
            until_date = int(_time.time() + seconds)
            bot.ban_chat_member(message.chat.id, t_id, until_date=until_date)

            # ✅ Send alert IMMEDIATELY — before any DB ops that could fail
            try:
                bot.send_message(message.chat.id, f"⛔ {name} has been banned for {display}.")
            except Exception as alert_err:
                logger.error(f"tban alert send failed: {alert_err}")

            # Log in database (non-critical — failure won't prevent alert)
            try:
                db.ban_user(message.chat.id, t_id, seconds, reason, message.from_user.first_name)
                db.log_infraction(message.chat.id, t_id, "tban", seconds, reason, message.from_user.id)
            except Exception as db_err:
                logger.error(f"tban DB log error: {db_err}")

        except Exception as e:
            logger.error(f"tban failed: {e}")
            try:
                bot.send_message(message.chat.id, f"❌ Ban failed: {str(e)}")
            except Exception:
                pass


    # ── /mutelist — SHOW ACTIVE MUTES ──
    @bot.message_handler(commands=['mutelist'])
    def cmd_mutelist(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        mutes = db.get_mutes(message.chat.id)
        if not mutes:
            return bot.reply_to(message, "✅ No active mutes in this group.")

        text = "🔇 <b>Active Mutes:</b>\n\n"
        for m in mutes[:10]:  # Show first 10
            user_id = m.get('user_id')
            unmute_time = m.get('unmute_at', 'Unknown')
            reason = m.get('reason', 'No reason')
            text += f"👤 <code>{user_id}</code> - {reason}\n   Until: {unmute_time}\n"

        bot.reply_to(message, text, parse_mode="HTML")

    # ── /banlist — SHOW ACTIVE BANS ──
    @bot.message_handler(commands=['banlist'])
    def cmd_banlist(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        bans = db.get_bans(message.chat.id)
        if not bans:
            return bot.reply_to(message, "✅ No active bans in this group.")

        text = "⛔ <b>Active Bans:</b>\n\n"
        for b in bans[:10]:  # Show first 10
            user_id = b.get('user_id')
            unban_time = b.get('unban_at', 'Permanent')
            reason = b.get('reason', 'No reason')
            text += f"👤 <code>{user_id}</code> - {reason}\n   Until: {unban_time}\n"

        bot.reply_to(message, text, parse_mode="HTML")

    # ── /warn ──
    @bot.message_handler(commands=['warn'])
    def cmd_warn(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return
        
        name = getattr(target, 'first_name', 'Unknown')
        warnings = db.add_warning(t_id, name)
        if warnings >= 3:
            try:
                bot.ban_chat_member(message.chat.id, t_id)
                db.reset_warnings(t_id)
                bot.reply_to(message, f"⚠️ User <b>{name}</b> (<code>{t_id}</code>) reached 3 warnings and was <b>banned</b>.", parse_mode="HTML")
            except Exception as e:
                bot.reply_to(message, f"⚠️ 3 warnings reached, but I couldn't ban them: {e}")
        else:
            bot.reply_to(message, f"⚠️ User <b>{name}</b> (<code>{t_id}</code>) warned. (<b>{warnings}/3</b>)", parse_mode="HTML")

    # ── /del ──
    @bot.message_handler(commands=['del'])
    def cmd_del(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        if not message.reply_to_message:
            return bot.reply_to(message, "Reply to a message to delete it.")
        try:
            bot.delete_message(message.chat.id, message.reply_to_message.message_id)
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

    # ── /promote ──
    @bot.message_handler(commands=['promote'])
    def cmd_promote(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        t_id = target.id
        t_uname = getattr(target, 'username', None)
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, t_uname):
            return bot.reply_to(message, "⚠️ Cannot promote this user (already an admin or owner protected).")
        try:
            bot.promote_chat_member(message.chat.id, t_id,
                can_change_info=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_invite_users=True, can_restrict_members=True,
                can_pin_messages=True, can_promote_members=False)
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"⏫ User <b>{name}</b> (<code>{t_id}</code>) promoted to <b>Admin</b>!", parse_mode="HTML")
        except Exception as e:
            if "RIGHT_FORBIDDEN" in str(e):
                bot.reply_to(message, "❌ <b>RIGHT_FORBIDDEN</b>\n\nThe bot needs the <b>\"Add new admins\"</b> permission to promote users.\n\n"
                    "👤 Go to Group Settings → Administrators → select the bot → enable <b>\"Add new admins\"</b>.",
                    parse_mode="HTML")
            else:
                bot.reply_to(message, f"Failed to promote: {e}")

    # ── /demote ──
    @bot.message_handler(commands=['demote'])
    def cmd_demote(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        target = get_target_user(message)
        if not target:
            return bot.reply_to(message, "❌ Reply to a user or provide their ID/@username.")
        t_id = target.id
        if not can_act_on(bot, message.chat.id, message.from_user.id, message.from_user.username, t_id, getattr(target, 'username', None)):
            return bot.reply_to(message, "⚠️ Cannot demote this user.")
        try:
            bot.promote_chat_member(message.chat.id, t_id,
                can_change_info=False, can_post_messages=False, can_edit_messages=False,
                can_delete_messages=False, can_invite_users=False, can_restrict_members=False,
                can_pin_messages=False, can_promote_members=False)
            name = getattr(target, 'first_name', str(t_id))
            bot.reply_to(message, f"⏬ User <b>{name}</b> (<code>{t_id}</code>) has been <b>demoted</b>.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to demote: {e}")

    # ── /pin ──
    @bot.message_handler(commands=['pin'])
    def cmd_pin(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        if not message.reply_to_message:
            return bot.reply_to(message, "Reply to a message to pin it.")
        try:
            bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
            bot.reply_to(message, "📌 Message pinned!")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /unpin ──
    @bot.message_handler(commands=['unpin'])
    def cmd_unpin(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        try:
            bot.unpin_all_chat_messages(message.chat.id)
            bot.reply_to(message, "📌 All messages unpinned!")
        except Exception as e:
            bot.reply_to(message, str(e))

    # ── /report ──
    @bot.message_handler(commands=['report'])
    def cmd_report(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not message.reply_to_message:
            return bot.reply_to(message, "Reply to a message to report it to admins.")
        try:
            admins = bot.get_chat_administrators(message.chat.id)
            chat_link_id = abs(message.chat.id) % (10 ** 10)
            for admin in admins:
                if not admin.user.is_bot:
                    try:
                        bot.send_message(
                            admin.user.id,
                            f"🚨 <b>Report from {message.chat.title}</b>\n\n"
                            f"Reported by: {message.from_user.first_name}\n"
                            f"Message: <a href='https://t.me/c/{chat_link_id}/{message.reply_to_message.message_id}'>View Message</a>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            bot.reply_to(message, "🚨 Admins have been notified.")
        except Exception:
            pass

    # ── /setwelcome — UPGRADED (reply-to-set: text / photo+caption / GIF+caption) ──
    @bot.message_handler(commands=['setwelcome'])
    def cmd_setwelcome(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        # ── Reply-based method ──
        if message.reply_to_message:
            m = message.reply_to_message
            if m.photo:
                file_id = m.photo[-1].file_id
                caption = m.caption or "Welcome, {name}! 👋"
                db.update_group_setting(message.chat.id, "welcome_type", "photo")
                db.update_group_setting(message.chat.id, "welcome_file_id", file_id)
                db.update_group_setting(message.chat.id, "welcome_message", caption)
                bot.reply_to(message,
                    "✅ <b>Welcome image set!</b>\n\n"
                    "New members will be greeted with your photo + caption.\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return

            elif m.animation:
                file_id = m.animation.file_id
                caption = m.caption or "Welcome, {name}! 👋"
                db.update_group_setting(message.chat.id, "welcome_type", "gif")
                db.update_group_setting(message.chat.id, "welcome_file_id", file_id)
                db.update_group_setting(message.chat.id, "welcome_message", caption)
                bot.reply_to(message,
                    "✅ <b>Welcome GIF set!</b>\n\n"
                    "New members will be greeted with your animated GIF + caption.\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return

            elif m.text:
                db.update_group_setting(message.chat.id, "welcome_type", "text")
                db.update_group_setting(message.chat.id, "welcome_file_id", "")
                db.update_group_setting(message.chat.id, "welcome_message", m.text)
                bot.reply_to(message,
                    "✅ <b>Welcome text set!</b>\n"
                    "<i>Use {name} or {id} for personalization.</i>",
                    parse_mode="HTML")
                return
            else:
                bot.reply_to(message,
                    "❌ Unsupported type. Reply to a <b>text</b>, <b>photo+caption</b>, or <b>GIF+caption</b>.",
                    parse_mode="HTML")
                return

        # ── Inline text method: /setwelcome <text> ──
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "welcome_type", "text")
            db.update_group_setting(message.chat.id, "welcome_file_id", "")
            db.update_group_setting(message.chat.id, "welcome_message", parts[1])
            bot.reply_to(message,
                "✅ <b>Welcome message updated!</b>\n<i>Use {name} or {id} to personalize.</i>",
                parse_mode="HTML")
        # ── Show current setting ──
        group_data = db.get_group(message.chat.id)
        current_msg = group_data.get("welcome_message", "Welcome, {name}! 👋")
        current_type = group_data.get("welcome_type", "text")
        
        bot.reply_to(message,
            f"💡 <b>Current Welcome Message ({current_type}):</b>\n\n"
            f"<code>{html.escape(current_msg)}</code>\n\n"
            "📌 <b>How to set:</b>\n"
            "1️⃣ <b>Text:</b> <code>/setwelcome Hello {name}!</code>\n"
            "2️⃣ <b>Image + Caption:</b> Send a photo, then <b>reply</b> with <code>/setwelcome</code>\n"
            "3️⃣ <b>GIF + Caption:</b> Send a GIF, then <b>reply</b> with <code>/setwelcome</code>\n\n"
            "📌 <b>Placeholders:</b>\n"
            "• <code>{name}</code> - Full name\n"
            "• <code>{username}</code> - @username\n"
            "• <code>{date}</code> - Today's date\n"
            "• <code>{group}</code> - Group title\n"
            "• <code>{id}</code> - User ID",
            parse_mode="HTML")


    # ── /setleave ──
    @bot.message_handler(commands=['setleave'])
    def cmd_setleave(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        # ── Reply method for photos/GIFs/text ──
        if message.reply_to_message:
            m = message.reply_to_message
            if m.photo:
                file_id = m.photo[-1].file_id
                caption = m.caption or "Goodbye, {name}!"
                db.update_group_setting(message.chat.id, "leave_type", "photo")
                db.update_group_setting(message.chat.id, "leave_file_id", file_id)
                db.update_group_setting(message.chat.id, "leave_message", caption)
                bot.reply_to(message, "✅ <b>Leave photo set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return

            elif m.animation:
                file_id = m.animation.file_id
                caption = m.caption or "Goodbye, {name}!"
                db.update_group_setting(message.chat.id, "leave_type", "gif")
                db.update_group_setting(message.chat.id, "leave_file_id", file_id)
                db.update_group_setting(message.chat.id, "leave_message", caption)
                bot.reply_to(message, "✅ <b>Leave GIF set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return

            elif m.text:
                db.update_group_setting(message.chat.id, "leave_type", "text")
                db.update_group_setting(message.chat.id, "leave_file_id", "")
                db.update_group_setting(message.chat.id, "leave_message", m.text)
                bot.reply_to(message, "✅ <b>Leave text set!</b>\n<i>Use {name} or {id} for personalization.</i>", parse_mode="HTML")
                return
            else:
                bot.reply_to(message, "❌ Unsupported type. Reply to a <b>text</b>, <b>photo+caption</b>, or <b>GIF+caption</b>.", parse_mode="HTML")
                return

        # ── Inline text method: /setleave <text> ──
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "leave_type", "text")
            db.update_group_setting(message.chat.id, "leave_file_id", "")
            db.update_group_setting(message.chat.id, "leave_message", parts[1])
            bot.reply_to(message, "✅ <b>Leave message updated!</b>\n<i>Use {name} or {id} to personalize.</i>", parse_mode="HTML")
        # ── Show current setting ──
        group_data = db.get_group(message.chat.id)
        current_msg = group_data.get("leave_message", "Goodbye {name}!")
        current_type = group_data.get("leave_type", "text")

        bot.reply_to(message,
            f"💡 <b>Current Leave Message ({current_type}):</b>\n\n"
            f"<code>{html.escape(current_msg)}</code>\n\n"
            "📌 <b>How to set:</b>\n"
            "1️⃣ <b>Text:</b> <code>/setleave Goodbye {name}!</code>\n"
            "2️⃣ <b>Image + Caption:</b> Send a photo, then <b>reply</b> with <code>/setleave</code>\n"
            "3️⃣ <b>GIF + Caption:</b> Send a GIF, then <b>reply</b> with <code>/setleave</code>\n\n"
            "📌 <b>Placeholders:</b>\n"
            "• <code>{name}</code> - Full name\n"
            "• <code>{username}</code> - @username\n"
            "• <code>{date}</code> - Today's date\n"
            "• <code>{group}</code> - Group title\n"
            "• <code>{id}</code> - User ID",
            parse_mode="HTML")


    # ── /setrules ──
    @bot.message_handler(commands=['setrules'])
    def cmd_setrules(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            db.update_group_setting(message.chat.id, "rules", parts[1])
            bot.reply_to(message, "✅ Rules updated!")

    # ── /rules ──
    @bot.message_handler(commands=['rules'])
    def cmd_rules(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        if group:
            bot.reply_to(message, group.get("rules", "No rules set."))

    # ── /addfilter ──
    @bot.message_handler(commands=['addfilter'])
    def cmd_addfilter(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /addfilter <keyword> (reply to media/text)")
        keyword = parts[1].strip()
        if not message.reply_to_message:
            return bot.reply_to(message, "You must reply to the content you want to set as the filter response.")
        m = message.reply_to_message
        filter_data = {}
        if m.text:
            filter_data = {"type": "text", "text": m.text}
        elif m.photo:
            filter_data = {"type": "photo", "file_id": m.photo[-1].file_id, "caption": m.caption or ""}
        elif m.sticker:
            filter_data = {"type": "sticker", "file_id": m.sticker.file_id}
        elif m.animation:
            filter_data = {"type": "gif", "file_id": m.animation.file_id}
        else:
            return bot.reply_to(message, "Unsupported media type.")
        db.add_filter(message.chat.id, keyword, filter_data)
        bot.reply_to(message, f"✅ Filter '<code>{keyword}</code>' added!", parse_mode="HTML")

    # ── /removefilter ──
    @bot.message_handler(commands=['removefilter'])
    def cmd_removefilter(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /removefilter <keyword>")
        db.remove_filter(message.chat.id, parts[1].strip())
        bot.reply_to(message, f"✅ Filter '<code>{parts[1].strip()}</code>' removed.", parse_mode="HTML")

    # ── /filters ──
    @bot.message_handler(commands=['filters'])
    def cmd_list_filters(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        group = db.get_group(message.chat.id)
        if not group or not group.get("filters"):
            return bot.reply_to(message, "No filters defined for this group.")
        fts = group["filters"].keys()
        text = "<b>🔍 Active Group Filters:</b>\n\n" + "\n".join([f"• <code>{f}</code>" for f in fts])
        bot.reply_to(message, text, parse_mode="HTML")

    # ── /lock ──
    @bot.message_handler(commands=['lock'])
    def cmd_lock(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        try:
            bot.set_chat_permissions(message.chat.id, telebot.types.ChatPermissions(can_send_messages=False))
            bot.reply_to(message, "🔒 <b>Group Locked!</b> Regular members can no longer send messages.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to lock: {e}")

    # ── /unlock ──
    @bot.message_handler(commands=['unlock'])
    def cmd_unlock(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        try:
            bot.set_chat_permissions(message.chat.id, telebot.types.ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
            bot.reply_to(message, "🔓 <b>Group Unlocked!</b> Regular members can now speak.", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"Failed to unlock: {e}")

    # ── /link ──
    @bot.message_handler(commands=['link'])
    def cmd_link(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        try:
            invite_link = bot.export_chat_invite_link(message.chat.id)
            bot.reply_to(message, f"🔗 <b>Invite Link:</b>\n{invite_link}", parse_mode="HTML")
        except Exception:
            bot.reply_to(message, "⚠️ Failed to fetch link. Ensure I have the 'Invite Users' admin permission.")

    # ── /addbadword ──
    @bot.message_handler(commands=['addbadword'])
    def cmd_addbadword(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            word = parts[1].strip().lower()
            group = db.get_group(message.chat.id)
            bad_words = group.get("bad_words", [])
            if word not in bad_words:
                bad_words.append(word)
                db.update_group_setting(message.chat.id, "bad_words", bad_words)
            bot.reply_to(message, f"🔇 <b>Auto-Mod:</b> Added '<code>{word}</code>' to the filter list.", parse_mode="HTML")
        else:
            bot.reply_to(message, "Format: /addbadword <word>")

    # ── /delbadword ──
    @bot.message_handler(commands=['delbadword'])
    def cmd_delbadword(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            word = parts[1].strip().lower()
            group = db.get_group(message.chat.id)
            bad_words = group.get("bad_words", [])
            if word in bad_words:
                bad_words.remove(word)
                db.update_group_setting(message.chat.id, "bad_words", bad_words)
                bot.reply_to(message, f"✅ Removed '<code>{word}</code>' from the filter.", parse_mode="HTML")
            else:
                bot.reply_to(message, "That word is not in the filter.")
        else:
            bot.reply_to(message, "Format: /delbadword <word>")

    # ── /approve ──
    @bot.message_handler(commands=['approve'])
    def cmd_approve(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        group = db.get_group(message.chat.id)
        current = group.get("approve_mode", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_approve_markup(message.chat.id, current)
        text = (f"🛡️ <b>Approval Gate Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>When ON:</b>\n"
                f"• New joining users are immediately restricted (muted).\n"
                f"• Admins are tagged with an interactive Approve/Decline menu.\n"
                f"• User remains muted until manual approval.\n\n"
                f"<i>Toggle status below:</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── /antispam — UPGRADED with inline buttons ──
    @bot.message_handler(commands=['antispam'])
    def cmd_antispam(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        group = db.get_group(message.chat.id)
        current = group.get("antispam", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_antispam_markup(message.chat.id, current)
        text = (f"🛡️ <b>Anti-Spam Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🔗 Block Telegram invite links\n"
                f"• ⚡ Rate limiting (max {_SPAM_MAX} msgs / {int(_SPAM_WINDOW)}s)\n"
                f"• Violations: warn → 3 warns → auto-ban\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── Antispam inline callback ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("antispam:"))
    def antispam_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except (ValueError, AttributeError):
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            bot.answer_callback_query(call.id, ACCESS_DENIED_MSG, show_alert=True)
            return

        new_status = 1 if action == "on" else 0
        db.update_group_setting(chat_id, "antispam", new_status)
        db.log_event(f"🛡️ Anti-Spam {'ON' if new_status else 'OFF'} in chat {chat_id} by {call.from_user.first_name}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_antispam_markup(chat_id, bool(new_status))
        text = (f"🛡️ <b>Anti-Spam Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🔗 Block Telegram invite links\n"
                f"• ⚡ Rate limiting (max {_SPAM_MAX} msgs / {int(_SPAM_WINDOW)}s)\n"
                f"• Violations: warn → 3 warns → auto-ban\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Anti-Spam {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── /antilink ──
    @bot.message_handler(commands=['antilink'])
    def cmd_antilink(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        group = db.get_group(message.chat.id)
        current = group.get("antispam_auto_delete_links", False) if group else False
        status = "🟢 <b>ACTIVE</b>" if current else "🔴 <b>INACTIVE</b>"
        markup = build_antilink_markup(message.chat.id, current)
        text = (f"🔗 <b>Anti-Link Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🚫 Block all links (hyperlinks, URLs, invite links)\n"
                f"• Deletes matching messages instantly\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        bot.reply_to(message, text, reply_markup=markup, parse_mode="HTML")

    # ── Antilink inline callback ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("antilink:"))
    def antilink_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except (ValueError, AttributeError):
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            bot.answer_callback_query(call.id, ACCESS_DENIED_MSG, show_alert=True)
            return

        new_status = (action == "on")
        db.update_group_setting(chat_id, "antispam_auto_delete_links", 1 if new_status else 0)
        db.log_event(f"🔗 Anti-Link {'ON' if new_status else 'OFF'} in chat {chat_id} by {call.from_user.first_name}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_antilink_markup(chat_id, new_status)
        text = (f"🔗 <b>Anti-Link Control Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>Protections enabled when ON:</b>\n"
                f"• 🚫 Block all links (hyperlinks, URLs, invite links)\n"
                f"• Deletes matching messages instantly\n\n"
                f"<i>Use the buttons below to toggle.</i>")
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Anti-Link {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── Approve Mode Toggle Inline Callbacks ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("approve_toggle:"))
    def approve_toggle_callback(call):
        try:
            _, action, chat_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            bot.answer_callback_query(call.id, ACCESS_DENIED_MSG, show_alert=True)
            return

        new_status = (action == "on")
        db.update_group_setting(chat_id, "approve_mode", new_status)
        db.log_event(f"🛡️ Approval Mode toggled {'ON' if new_status else 'OFF'} in {chat_id}")

        status = "🟢 <b>ACTIVE</b>" if new_status else "🔴 <b>INACTIVE</b>"
        markup = build_approve_markup(chat_id, new_status)
        text = (f"🛡️ <b>Approval Gate Panel</b>\n\n"
                f"Status: {status}\n\n"
                f"<b>When ON:</b>\n"
                f"• New joining users are immediately restricted (muted).\n"
                f"• Admins are tagged with an interactive Approve/Decline menu.\n"
                f"• User remains muted until manual approval.\n\n"
                f"<i>Toggle status below:</i>")
        try:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Approval Gate {'Enabled ✅' if new_status else 'Disabled ❌'}")

    # ── Approval Member Accept/Decline Inline Callbacks ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("approve_user:") or call.data.startswith("decline_user:"))
    def approval_callback(call):
        try:
            action, chat_id_str, user_id_str = call.data.split(":")
            chat_id = int(chat_id_str)
            target_user_id = int(user_id_str)
        except Exception:
            bot.answer_callback_query(call.id, "❌ Invalid data")
            return

        if not is_admin(bot, chat_id, call.from_user.id) and not is_owner(call.from_user.username):
            bot.answer_callback_query(call.id, ACCESS_DENIED_MSG, show_alert=True)
            return

        if action == "approve_user":
            db.approve_user(chat_id, target_user_id, approved_by=call.from_user.first_name)
            try:
                bot.restrict_chat_member(
                    chat_id, target_user_id,
                    permissions=telebot.types.ChatPermissions(
                        can_send_messages=True, can_send_media_messages=True,
                        can_send_other_messages=True, can_add_web_page_previews=True
                    )
                )
                bot.edit_message_text(
                    chat_id=chat_id, message_id=call.message.message_id,
                    text=f"✅ User ID <code>{target_user_id}</code> was <b>APPROVED</b> by {call.from_user.first_name}.",
                    parse_mode="HTML"
                )
                # Send a notification to the user letting them know they can chat
                bot.send_message(
                    chat_id, 
                    f"🎉 <a href='tg://user?id={target_user_id}'>User</a>, you have been <b>approved</b> by an admin! You can now participate in the chat.", 
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.answer_callback_query(call.id, f"Failed: {e}")
                return
            bot.answer_callback_query(call.id, "User Approved ✅")
        elif action == "decline_user":
            try:
                bot.ban_chat_member(chat_id, target_user_id)
                bot.edit_message_text(
                    chat_id=chat_id, message_id=call.message.message_id,
                    text=f"❌ User ID <code>{target_user_id}</code> was <b>DECLINED</b> by {call.from_user.first_name} and has been banned.",
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.answer_callback_query(call.id, f"Failed to ban: {e}")
                return
            bot.answer_callback_query(call.id, "User Declined ❌")

    # ── /id ──
    @bot.message_handler(commands=['id'])
    def cmd_id(message):
        target = get_target_user(message)
        msgs = [f"💬 <b>Chat ID:</b> <code>{message.chat.id}</code>"]
        
        user_id = target.id if target else message.from_user.id
        user_name = getattr(target, 'first_name', '') if target else message.from_user.first_name
        msgs.append(f"👤 <b>User ID ({user_name}):</b> <code>{user_id}</code>")
        
        bot.reply_to(message, "\n".join(msgs), parse_mode="HTML")

    # ── /settitle ──
    @bot.message_handler(commands=['settitle'])
    def cmd_settitle(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            try:
                bot.set_chat_title(message.chat.id, parts[1])
                bot.reply_to(message, "✅ <b>Group Title Updated!</b>", parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Failed. Ensure I have 'Change Group Info' admin rights.")
        else:
            bot.reply_to(message, "Format: /settitle <New Title>")

    # ── /setdesc ──
    @bot.message_handler(commands=['setdesc'])
    def cmd_setdesc(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) > 1:
            try:
                bot.set_chat_description(message.chat.id, parts[1])
                bot.reply_to(message, "✅ <b>Group Description Updated!</b>", parse_mode="HTML")
            except Exception:
                bot.reply_to(message, "⚠️ Failed. Ensure I have 'Change Group Info' admin rights.")
        else:
            bot.reply_to(message, "Format: /setdesc <New Description>")

    # ── /send ──
    @bot.message_handler(commands=['send'])
    def cmd_send_msg(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            return bot.reply_to(message, "Format: /send <message>")
        try:
            bot.send_message(message.chat.id, parts[1])
            bot.delete_message(message.chat.id, message.message_id)
        except Exception as e:
            bot.reply_to(message, f"Error: {e}")

    # ── /testdb — Check Database Status ──
    @bot.message_handler(commands=['testdb'])
    def cmd_testdb(message):
        if message.chat.type not in ['group', 'supergroup']:
            return
        if not require_admin(bot, message):
            return

        try:
            stats = db.get_all_stats()
            total_users = stats.get('total_users', 0)
            total_groups = stats.get('total_groups', 0)
            db_status = "✅ Connected" if db.conn else "❌ Disconnected"

            this_group = db.get_group(message.chat.id)
            group_saved = "✅ Yes" if this_group and this_group.get('chat_id') else "❌ No"

            text = (
                f"<b>🗄️ Database Status</b>\n\n"
                f"<b>DB Connection:</b> {db_status}\n"
                f"<b>Total Users:</b> {total_users}\n"
                f"<b>Total Groups:</b> {total_groups}\n"
                f"<b>This Group in DB:</b> {group_saved}\n"
                f"<b>DB Path:</b> <code>manager.db</code>\n\n"
                f"<i>Group e kono message pathano por user count barche kina check koren.</i>"
            )
            bot.reply_to(message, text, parse_mode="HTML")
        except Exception as e:
            bot.reply_to(message, f"❌ DB Check Failed: <code>{e}</code>", parse_mode="HTML")

    # ── Register bot commands ──
    try:
        commands = [
            telebot.types.BotCommand("start",        "Bot introduction"),
            telebot.types.BotCommand("help",         "Full command list"),
            telebot.types.BotCommand("info",         "User profile & stats"),
            telebot.types.BotCommand("ban",          "Permanently ban a user"),
            telebot.types.BotCommand("unban",        "Unban a user / revoke ban"),
            telebot.types.BotCommand("tmute",        "Time-based mute (e.g., /tmute @user 1h)"),
            telebot.types.BotCommand("tban",         "Time-based ban (e.g., /tban @user 1d)"),
            telebot.types.BotCommand("mutelist",     "Show active mutes in group"),
            telebot.types.BotCommand("banlist",      "Show active bans in group"),
            telebot.types.BotCommand("kick",         "Remove user from group"),
            telebot.types.BotCommand("mute",         "Restrict user from talking"),
            telebot.types.BotCommand("unmute",       "Restore talking privileges"),
            telebot.types.BotCommand("warn",         "Issue a formal warning"),
            telebot.types.BotCommand("lock",         "Lock the group"),
            telebot.types.BotCommand("unlock",       "Unlock the group"),
            telebot.types.BotCommand("promote",      "Promote to Administrator"),
            telebot.types.BotCommand("demote",       "Remove Administrator rights"),
            telebot.types.BotCommand("link",         "Fetch group invite link"),
            telebot.types.BotCommand("settitle",     "Change Group Title"),
            telebot.types.BotCommand("setdesc",      "Change Group Description"),
            telebot.types.BotCommand("addbadword",   "Add word to Auto-Mod filter"),
            telebot.types.BotCommand("delbadword",   "Remove word from filter"),
            telebot.types.BotCommand("antispam",     "Toggle Anti-Spam (inline buttons)"),
            telebot.types.BotCommand("antilink",     "Toggle Anti-Link (inline buttons)"),
            telebot.types.BotCommand("pin",          "Pin a message"),
            telebot.types.BotCommand("unpin",        "Unpin all messages"),
            telebot.types.BotCommand("del",          "Delete a replied message"),
            telebot.types.BotCommand("rules",        "Show group rules"),
            telebot.types.BotCommand("setrules",     "Set group rules"),
            telebot.types.BotCommand("setwelcome",   "Set welcome (reply to text/photo/GIF)"),
            telebot.types.BotCommand("setleave",     "Set goodbye (reply to text/photo/GIF)"),
            telebot.types.BotCommand("addfilter",    "Add auto-reply filter"),
            telebot.types.BotCommand("removefilter", "Remove auto-reply filter"),
            telebot.types.BotCommand("filters",      "List active filters"),
            telebot.types.BotCommand("admins",       "List all group admins"),
            telebot.types.BotCommand("send",         "Bot sends a custom message"),
            telebot.types.BotCommand("report",       "Alert admins about a message"),
            telebot.types.BotCommand("id",           "Get User/Chat IDs (for Dashboard)"),
            telebot.types.BotCommand("approve",      "Toggle approve mode on/off"),
            telebot.types.BotCommand("testdb",      "Check database storage status"),
        ]
        bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"Failed to register commands: {e}")

    # ── CATCH-ALL: rate-limit spam + link spam + bad words + filters ──
    @bot.message_handler(
        func=lambda m: True,
        content_types=['text', 'photo', 'video', 'sticker', 'animation', 'document', 'voice', 'audio']
    )
    def all_messages(message):
        if message.chat.type not in ['group', 'supergroup']:
            return

        # Track Group in DB
        try:
            db.ensure_group(message.chat.id, name=message.chat.title)
            db.add_message_count(message.chat.id)
        except Exception as e:
            logger.error(f"❌ DB group track failed: {e}")

        # Track User in DB (Captures Profile + Msg Count + Location)
        try:
            uname = getattr(message.from_user, 'username', None)
            fname = message.from_user.first_name or ""
            lname = message.from_user.last_name or ""
            full_name = f"{fname} {lname}".strip() or "User"

            db.ensure_user(
                message.from_user.id,
                name=full_name,
                username=uname,
                chat_id=message.chat.id,
                chat_name=message.chat.title,
                increment_msg=True
            )
        except Exception as e:
            logger.error(f"❌ DB ensure_user failed for {message.from_user.id}: {e}")

        group = db.get_group(message.chat.id)
        # If group is still empty/None after lookup, use default settings
        if not group:
            group = {"antispam": False, "bad_words": [], "filters": {}}

        user_is_admin = is_admin(bot, message.chat.id, message.from_user.id)
        user_is_owner = is_owner(message.from_user.username)

        # ── ⚡ Rate-limit anti-spam (applies to ALL message types if enabled) ──
        antispam_active = group.get("antispam", False)
        if antispam_active and not user_is_admin and not user_is_owner:
            now = _time.time()
            key = (message.chat.id, message.from_user.id)
            dq  = _msg_timestamps[key]
            dq.append(now)
            # Evict timestamps outside window
            while dq and dq[0] < now - _SPAM_WINDOW:
                dq.popleft()

            if len(dq) > _SPAM_MAX:
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                    warnings = db.add_warning(message.from_user.id, message.from_user.first_name)
                    dq.clear()  # reset counter after action
                    if warnings >= 3:
                        bot.ban_chat_member(message.chat.id, message.from_user.id)
                        db.reset_warnings(message.from_user.id)
                        bot.send_message(
                            message.chat.id,
                            f"⚡ <b>{message.from_user.first_name}</b> was <b>banned</b> for spamming "
                            f"(3 flood warnings triggered).",
                            parse_mode="HTML"
                        )
                        db.log_event(f"⚡ Spam-ban: {message.from_user.id} in {message.chat.id}")
                    else:
                        bot.send_message(
                            message.chat.id,
                            f"⚡ <b>{message.from_user.first_name}</b>, slow down! "
                            f"Flood detected. Warning <b>{warnings}/3</b>",
                            parse_mode="HTML"
                        )
                except Exception:
                    pass
                return

        # ── Text checks (handles both regular text and media captions) ──
        content = (message.text or message.caption or "").strip()
        if not content:
            return

        text_lower = content.lower()

        # ── 🔗 Link / Invite Link Protection ──
        antilink_active = group.get("antispam_auto_delete_links", False)
        if not user_is_admin and not user_is_owner:
            has_link = False
            if message.entities:
                for ent in message.entities:
                    if ent.type in ('url', 'text_link'):
                        has_link = True
                        break
            if not has_link and getattr(message, 'caption_entities', None):
                for ent in message.caption_entities:
                    if ent.type in ('url', 'text_link'):
                        has_link = True
                        break
            if not has_link:
                # regex/substring fallback check
                if ("http://" in text_lower or "https://" in text_lower or "www." in text_lower or "t.me/" in text_lower or "telegram.me/" in text_lower or "joinchat" in text_lower):
                    has_link = True

            if has_link:
                # If anti-link is ON, block all links
                if antilink_active:
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                        bot.send_message(
                            message.chat.id,
                            f"🚫 <b>Anti-Link</b>: {message.from_user.first_name}, "
                            f"links are <b>not allowed</b> in this group.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    db.log_event(f"🔗 Link blocked: user {message.from_user.id} in {message.chat.id}")
                    return

                # If anti-link is OFF but anti-spam is ON, block only Telegram invite links
                elif antispam_active:
                    is_invite = ("t.me/" in text_lower or "telegram.me/" in text_lower or "telegram.dog/" in text_lower or "joinchat" in text_lower)
                    if is_invite:
                        try:
                            bot.delete_message(message.chat.id, message.message_id)
                        except Exception:
                            pass
                        # Warn the user for sending invite links
                        warnings = db.add_warning(message.from_user.id, message.from_user.first_name)
                        try:
                            if warnings >= 3:
                                bot.ban_chat_member(message.chat.id, message.from_user.id)
                                db.reset_warnings(message.from_user.id)
                                bot.send_message(
                                    message.chat.id,
                                    f"🚫 <b>{message.from_user.first_name}</b> was <b>banned</b> for repeatedly "
                                    f"sending Telegram invite links (3 warnings).",
                                    parse_mode="HTML"
                                )
                                db.log_event(f"🔗 Invite link ban: user {message.from_user.id} in {message.chat.id}")
                            else:
                                bot.send_message(
                                    message.chat.id,
                                    f"🚫 <b>Anti-Spam</b>: <b>{message.from_user.first_name}</b>, "
                                    f"Telegram invite links are <b>not allowed</b> here. "
                                    f"Warning <b>{warnings}/3</b>",
                                    parse_mode="HTML"
                                )
                                db.log_event(f"🔗 Invite link blocked: user {message.from_user.id} in {message.chat.id} (warn {warnings}/3)")
                        except Exception:
                            pass
                        return

            # Check for promotional spam (emoji flooding + links) - only if antispam is active
            if antispam_active:
                is_promo, reason = detect_promotional_spam(content)
                if is_promo:
                    try:
                        bot.delete_message(message.chat.id, message.message_id)
                    except Exception:
                        pass
                    # Warn the user for promotional spam
                    warnings = db.add_warning(message.from_user.id, message.from_user.first_name)
                    try:
                        if warnings >= 3:
                            bot.ban_chat_member(message.chat.id, message.from_user.id)
                            db.reset_warnings(message.from_user.id)
                            bot.send_message(
                                message.chat.id,
                                f"🚫 <b>{message.from_user.first_name}</b> was <b>banned</b> for "
                                f"promotional spam (3 warnings).",
                                parse_mode="HTML"
                            )
                            db.log_event(f"📊 Promo spam ban: {message.from_user.id} in {message.chat.id}")
                        else:
                            bot.send_message(
                                message.chat.id,
                                f"🚫 <b>Anti-Spam</b>: <b>{message.from_user.first_name}</b>, {reason}. "
                                f"Warning <b>{warnings}/3</b>",
                                parse_mode="HTML"
                            )
                            db.log_event(f"📊 Promo spam blocked: {reason} by {message.from_user.id} (warn {warnings}/3)")
                    except Exception:
                        pass
                    return

        # ── 🤬 Bad words — delete for ALL users, warn + potential ban for non-admins ──
        bad_words = group.get("bad_words", [])
        for bw in bad_words:
            # Use regex for word boundaries to avoid matching substrings (e.g. 'ass' in 'assume')
            pattern = rf"\b{re.escape(bw)}\b"
            if re.search(pattern, text_lower):
                # Always delete the message
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                except Exception:
                    pass

                # Only warn non-admins / non-owners
                if not user_is_admin and not user_is_owner:
                    try:
                        warnings = db.add_warning(message.from_user.id, message.from_user.first_name)
                        if warnings >= 3:
                            bot.ban_chat_member(message.chat.id, message.from_user.id)
                            db.reset_warnings(message.from_user.id)
                            bot.send_message(
                                message.chat.id,
                                f"⚠️ <b>{message.from_user.first_name}</b> was <b>banned</b> "
                                f"for reaching 3 bad-language warnings.",
                                parse_mode="HTML"
                            )
                        else:
                            bot.send_message(
                                message.chat.id,
                                f"⚠️ <b>{message.from_user.first_name}</b>, watch your language! "
                                f"Warning <b>{warnings}/3</b>",
                                parse_mode="HTML"
                            )
                    except Exception:
                        pass
                return  # stop processing after bad word match

        # ── 🔍 Auto-reply filters ──
        filters = group.get("filters", {})
        for trigger, f_data in filters.items():
            pattern = rf"\b{re.escape(trigger)}\b"
            if re.search(pattern, text_lower):
                ftype = f_data.get("type")
                try:
                    if ftype == "text":
                        bot.reply_to(message, f_data.get("text"))
                    elif ftype == "photo":
                        bot.send_photo(message.chat.id, f_data.get("file_id"),
                                       caption=f_data.get("caption"),
                                       reply_to_message_id=message.message_id)
                    elif ftype == "sticker":
                        bot.send_sticker(message.chat.id, f_data.get("file_id"),
                                         reply_to_message_id=message.message_id)
                    elif ftype == "gif":
                        bot.send_animation(message.chat.id, f_data.get("file_id"),
                                           reply_to_message_id=message.message_id)
                except Exception:
                    pass
                break
