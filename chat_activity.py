"""
Chat Activity / Rankings System — an independent module for the Group Manager bot.

Provides:
  * Message tracking (overall / daily / weekly) per group, persisted in SQLite
  * User milestones (100 / 500 / 1K / 1.5K / 2K / 2.5K / 3K) announced once
  * Daily group milestones announced once per day
  * `/rankings` (and `/top` alias) command with a dynamic dark/red leaderboard image
  * `/mystats` personal chat statistics and `/chatstats` group chat statistics
  * Overall / Today / Week modes + complete leaderboard with pagination
  * `/activity` admin panel to toggle tracking, milestones and leaderboard

This module does not modify any existing bot feature. It plugs into the bot
through `register_chat_handlers(bot)` and `track_group_message(bot, message)`.
"""

import html
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# MILESTONES
# ─────────────────────────────────────────────────────────────
MILESTONES = [100, 500, 1000, 1500, 2000, 2500, 3000]

# ─────────────────────────────────────────────────────────────
# TIMEZONE (env CHAT_ACTIVITY_TIMEZONE, default Asia/Dhaka; falls back to fixed UTC+6)
# ─────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.getenv("CHAT_ACTIVITY_TIMEZONE", "Asia/Dhaka").strip() or "Asia/Dhaka")
except Exception:
    _TZ = timezone(timedelta(hours=6))


# ─────────────────────────────────────────────────────────────
# GLOBAL MASTER SWITCH (env CHAT_ACTIVITY_ENABLED, default ON)
# ─────────────────────────────────────────────────────────────
def chat_activity_enabled():
    """Global enable/disable for the whole Chat Activity system."""
    return os.getenv("CHAT_ACTIVITY_ENABLED", "1").strip().lower() not in (
        "0", "false", "off", "no", "n", "disabled", "none",
    )


def now_local():
    return datetime.now(_TZ)


def get_today():
    return now_local().strftime("%Y-%m-%d")


def get_week_start(date=None):
    """Week boundary is Monday. Returns 'YYYY-MM-DD'."""
    d = date or now_local().date()
    if isinstance(d, datetime):
        d = d.date()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def format_num(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def get_display_name(user):
    fname = getattr(user, "first_name", None) or ""
    lname = getattr(user, "last_name", None) or ""
    name = f"{fname} {lname}".strip()
    return name or "Unknown"


# ─────────────────────────────────────────────────────────────
# ADMIN CHECK HELPERS (self-contained, no circular imports)
# ─────────────────────────────────────────────────────────────
def _is_admin(bot, chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _is_owner(username):
    try:
        config = db.get_config()
        owner = config.get("owner_username", "")
        return bool(username and owner and username.lower().replace("@", "") == owner.lower())
    except Exception:
        return False


def _reply_not_admin(bot, obj):
    try:
        msg = db.get_config().get("msg_not_admin", "❌ You must be an admin to use this.")
        if hasattr(obj, "data"):
            bot.answer_callback_query(obj.id, msg, show_alert=True)
        else:
            bot.reply_to(obj, msg)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# TEXT LEADERBOARD BUILDERS
# ─────────────────────────────────────────────────────────────
_MODE_LABEL = {"overall": "Overall", "today": "Today", "week": "Week"}


def build_text_leaderboard(mode, entries, total, full=False):
    label = _MODE_LABEL.get(mode, "Overall")
    sep = "—" if full else "•"
    header = "📊 <b>GROUP LEADERBOARD</b>" if full else "📈 <b>LEADERBOARD</b>"
    if mode != "overall":
        header += f" <i>({label})</i>"
    lines = [header, ""]
    if not entries:
        lines.append("📊 <b>No chat statistics available yet.</b>")
        lines.append("💬 Start chatting to build the leaderboard!")
    else:
        for i, e in enumerate(entries, 1):
            name = html.escape(str(e.get("display_name") or "Unknown"))
            lines.append(f"{i}. 👤 {name} {sep} {format_num(e.get('total_messages'))}")
    lines.append("")
    lines.append(f"💌 <b>Total messages: {format_num(total)}</b>")
    return "\n".join(lines)


def _truncate_text(text, limit=30):
    """Safely truncate a display name (handles unicode without splitting surrogate pairs)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_my_stats_text(display_name, username, stats):
    """Personal chat statistics for the requesting user (current group only)."""
    name = _truncate_text(display_name or "Unknown", 30)
    handle = f" (@{username})" if username else ""
    rank = stats.get("rank")
    rank_text = f"#{rank}" if rank else "Unranked"
    return "\n".join([
        "👤 <b>YOUR CHAT STATS</b>",
        "",
        f"<b>{html.escape(name)}</b>{html.escape(handle)}",
        f"💬 Total Messages: <b>{format_num(stats.get('total_messages'))}</b>",
        f"📅 Today: <b>{format_num(stats.get('today_messages'))}</b>",
        f"📆 This Week: <b>{format_num(stats.get('week_messages'))}</b>",
        f"🏆 Group Rank: <b>{rank_text}</b>",
    ])


def build_chat_stats_text(stats):
    """Compact group-level chat statistics summary."""
    top = stats.get("top_chatter")
    if top:
        if top.get("username"):
            top_label = f"@{top['username']}"
        else:
            top_label = f"<b>{html.escape(_truncate_text(top.get('display_name') or 'Unknown', 30))}</b>"
    else:
        top_label = "None yet"
    return "\n".join([
        "📊 <b>GROUP CHAT STATS</b>",
        "",
        f"💬 Total Messages: <b>{format_num(stats.get('total_messages'))}</b>",
        f"📅 Today: <b>{format_num(stats.get('today_messages'))}</b>",
        f"📆 This Week: <b>{format_num(stats.get('week_messages'))}</b>",
        f"👥 Active Today: <b>{format_num(stats.get('active_today'))}</b>",
        f"🏆 Top Chatter: <b>{top_label}</b>",
    ])


def build_mode_markup(mode, chat_id):
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton(
            "🔘 Overall ✅" if mode == "overall" else "🔘 Overall",
            callback_data=f"rank:mode:overall:{chat_id}",
        )
    )
    mk.row(
        InlineKeyboardButton(
            "🔘 Today ✅" if mode == "today" else "🔘 Today",
            callback_data=f"rank:mode:today:{chat_id}",
        ),
        InlineKeyboardButton(
            "🔘 Week ✅" if mode == "week" else "🔘 Week",
            callback_data=f"rank:mode:week:{chat_id}",
        ),
    )
    mk.row(
        InlineKeyboardButton(
            "📊 View complete leaderboard ↗",
            callback_data=f"rank:full:{mode}:{chat_id}:1",
        )
    )
    return mk


def build_full_markup(mode, chat_id, page, total_pages):
    mk = InlineKeyboardMarkup()
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"rank:full:{mode}:{chat_id}:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"rank:full:{mode}:{chat_id}:{page + 1}"))
    if nav:
        mk.row(*nav)
    mk.row(InlineKeyboardButton("🔙 Back to Top 10", callback_data=f"rank:back:{mode}:{chat_id}"))
    return mk


# ─────────────────────────────────────────────────────────────
# LEADERBOARD IMAGE GENERATION (Pillow, dark/red gaming style)
#
# Unicode-aware: user names are rendered character-by-character with a
# script-aware font fallback chain (Latin, Bangla, Devanagari, Arabic,
# Hebrew, Thai, CJK, emoji, ...), so Bangla/emoji/unicode names render
# instead of showing tofu boxes. The image height auto-fits the number
# of ranked users so there is never a large empty area below the rows.
# ─────────────────────────────────────────────────────────────
_IMG_FONT_CACHE = {}
_FONT_PATHS_CACHE = {}
_GLYPH_CACHE = {}

# family -> (regular filename patterns, bold filename patterns)
# Patterns are matched (case-insensitively) against font file names in
# the common system font directories.
_FONT_FAMILY_PATTERNS = {
    "latin": (
        ["arial", "segoeui", "tahoma", "calibri", "seguisym", "verdana",
         "dejavusans", "notosans", "freesans", "liberationsans", "arialuni"],
        ["arialbd", "segoeuib", "tahomabd", "calibrib", "verdanab",
         "dejavusans-bold", "notosans-bold", "freesansbold",
         "liberationsans-bold", "arialuni"],
    ),
    "arabic": (
        ["tahoma", "arial", "segoeui", "notosansarabic", "notonaskharabic", "amiri"],
        ["tahomabd", "arialbd", "segoeuib", "notosansarabic-bold", "amiri-bold"],
    ),
    "hebrew": (
        ["tahoma", "arial", "segoeui", "notosanshebrew", "notoserifhebrew"],
        ["tahomabd", "arialbd", "segoeuib", "notosanshebrew-bold", "notoserifhebrew-bold"],
    ),
    "bengali": (
        ["nirmala", "vrinda", "notosansbengali", "notoserifbengali", "bangla"],
        ["nirmalab", "vrindab", "notosansbengali-bold", "notoserifbengali-bold"],
    ),
    "devanagari": (
        ["nirmala", "mangal", "kokila", "notosansdevanagari", "notoserifdevanagari"],
        ["nirmalab", "mangalb", "kokilab", "notosansdevanagari-bold", "notoserifdevanagari-bold"],
    ),
    "gujarati": (
        ["nirmala", "shruti", "notosansgujarati"],
        ["nirmalab", "shrutib", "notosansgujarati-bold"],
    ),
    "gurmukhi": (
        ["nirmala", "raavi", "notosansgurmukhi"],
        ["nirmalab", "raavib", "notosansgurmukhi-bold"],
    ),
    "tamil": (
        ["nirmala", "latha", "notosanstamil", "notoseriftamil"],
        ["nirmalab", "lathab", "notosanstamil-bold", "notoseriftamil-bold"],
    ),
    "telugu": (
        ["nirmala", "gautami", "notosanstelugu"],
        ["nirmalab", "gautamib", "notosanstelugu-bold"],
    ),
    "kannada": (
        ["nirmala", "tunga", "notosanskannada"],
        ["nirmalab", "tungab", "notosanskannada-bold"],
    ),
    "malayalam": (
        ["nirmala", "kartika", "notosansmalayalam"],
        ["nirmalab", "kartikab", "notosansmalayalam-bold"],
    ),
    "sinhala": (
        ["nirmala", "iskoolapota", "notosanssinhala"],
        ["nirmalab", "iskoolapotab", "notosanssinhala-bold"],
    ),
    "oriya": (
        ["nirmala", "kalinga", "notosansoriya"],
        ["nirmalab", "kalingab", "notosansoriya-bold"],
    ),
    "thai": (
        ["leelawadee", "tahoma", "notosansthai", "garuda", "loma"],
        ["leelawadeebold", "tahomabd", "notosansthai-bold", "garudabold", "lomabold"],
    ),
    "lao": (
        ["lao", "notosanslao", "phetsarath"],
        ["laobold", "notosanslao-bold", "phetsarathbold"],
    ),
    "tibetan": (
        ["kailasa", "microsoft himalaya", "notosanstibetan"],
        ["kailasabold", "notosanstibetan-bold"],
    ),
    "myanmar": (
        ["myanmar", "notosansmyanmar", "padauk"],
        ["myanmar", "notosansmyanmar-bold", "padaukbold"],
    ),
    "khmer": (
        ["khmer", "daunpenh", "notosanskhmer"],
        ["khmer", "daunpenhb", "notosanskhmer-bold"],
    ),
    "cjk": (
        ["msyh", "simhei", "simsun", "notosanscjk", "notosanssc", "notoserifcjk",
         "wqyzenhei", "source han"],
        ["msyhbd", "simheib", "simsunb", "notosanscjk-bold", "notosanssc-bold",
         "wqyzenheibold"],
    ),
    "hangul": (
        ["malgun", "batang", "gulim", "notosanskr", "notosanscjk", "nanum"],
        ["malgunbd", "batangbold", "gulimbold", "notosanskr-bold", "nanumbold"],
    ),
    "emoji": (
        ["seguiemj", "notocoloremoji", "notoemoji", "symbola"],
        ["seguiemj", "notocoloremoji"],
    ),
}


def _font_dirs():
    dirs = []
    if os.name == "nt":
        dirs.append(r"C:\Windows\Fonts")
    else:
        for base in (
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ):
            if os.path.isdir(base):
                dirs.append(base)
    return [d for d in dirs if d and os.path.isdir(d)]


def _get_font_paths(family, bold=False):
    """Return all matching font file paths for a family, in priority order."""
    key = (family, bold)
    cached = _FONT_PATHS_CACHE.get(key)
    if cached is not None:
        return cached
    patterns = _FONT_FAMILY_PATTERNS.get(family, _FONT_FAMILY_PATTERNS["latin"])
    pats = patterns[1] if bold else patterns[0]
    found = []
    seen = set()
    for d in _font_dirs():
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for fname in sorted(names):
            low = fname.lower()
            if not low.endswith((".ttf", ".otf", ".ttc", ".otc")):
                continue
            if any(p in low for p in pats):
                path = os.path.join(d, fname)
                if path not in seen:
                    seen.add(path)
                    found.append(path)
    if not found and bold:
        found = _get_font_paths(family, False)
    _FONT_PATHS_CACHE[key] = found
    return found


def _get_font_from_path(path, size):
    key = ("path", path, size)
    if key in _IMG_FONT_CACHE:
        return _IMG_FONT_CACHE[key]
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(path, size)
    except Exception:
        font = None
    _IMG_FONT_CACHE[key] = font
    return font


def _get_font(size, bold=False):
    """Primary Latin font for a size (title/rank/count/bar text)."""
    key = ("latin_base", size, bold)
    if key in _IMG_FONT_CACHE:
        return _IMG_FONT_CACHE[key]
    try:
        from PIL import ImageFont
    except Exception:
        _IMG_FONT_CACHE[key] = None
        return None
    for path in _get_font_paths("latin", bold):
        font = _get_font_from_path(path, size)
        if font is not None:
            _IMG_FONT_CACHE[key] = font
            return font
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    _IMG_FONT_CACHE[key] = font
    return font


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001F0FF"  # mahjong / dominoes / playing cards
    "\U0001F100-\U0001F1FF"  # enclosed alphanumerics + regional indicators
    "\U0001F200-\U0001F2FF"  # enclosed ideographic + misc
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended
    "\U00002600-\U000026FF"  # misc symbols (weather etc.)
    "\U00002700-\U000027BF"  # dingbats
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "\U0000FE0F"             # variation selector 16
    "]"
)


def _script_family(char):
    """Map a character to the font family that covers its script."""
    cp = ord(char)
    if cp < 0x02B0:
        return "latin"  # ASCII + Latin-1 + basic punctuation/symbols
    if (0x0370 <= cp <= 0x03FF or 0x0400 <= cp <= 0x052F
            or 0x1E00 <= cp <= 0x1EFF or 0x2C60 <= cp <= 0x2C7F):
        return "latin"  # Greek, Cyrillic, Latin Extended
    if 0x0590 <= cp <= 0x05FF:
        return "hebrew"
    if (0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x08A0 <= cp <= 0x08FF
            or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF):
        return "arabic"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0980 <= cp <= 0x09FF:
        return "bengali"
    if 0x0A00 <= cp <= 0x0A7F:
        return "gurmukhi"
    if 0x0A80 <= cp <= 0x0AFF:
        return "gujarati"
    if 0x0B00 <= cp <= 0x0B7F:
        return "oriya"
    if 0x0B80 <= cp <= 0x0BFF:
        return "tamil"
    if 0x0C00 <= cp <= 0x0C7F:
        return "telugu"
    if 0x0C80 <= cp <= 0x0CFF:
        return "kannada"
    if 0x0D00 <= cp <= 0x0D7F:
        return "malayalam"
    if 0x0D80 <= cp <= 0x0DFF:
        return "sinhala"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    if 0x0E80 <= cp <= 0x0EFF:
        return "lao"
    if 0x0F00 <= cp <= 0x0FFF:
        return "tibetan"
    if 0x1000 <= cp <= 0x109F:
        return "myanmar"
    if 0x1780 <= cp <= 0x17FF:
        return "khmer"
    if 0x1100 <= cp <= 0x11FF or 0xAC00 <= cp <= 0xD7AF:
        return "hangul"
    if (0x2E80 <= cp <= 0x303F or 0x3040 <= cp <= 0x30FF or 0x3400 <= cp <= 0x4DBF
            or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FFFF):
        return "cjk"
    return "latin"


def _char_has_glyph(font, char):
    """Best-effort check that a font can actually render the character
    (i.e. it is not mapped to the .notdef / tofu box glyph)."""
    key = (id(font), char)
    if key in _GLYPH_CACHE:
        return _GLYPH_CACHE[key]
    res = True
    try:
        bbox = font.getmask(char).getbbox()
        notdef = font.getmask("\ufffd").getbbox()
        if bbox is None:
            res = True  # whitespace / zero-width
        elif notdef is None:
            res = True
        else:
            res = bbox != notdef
    except Exception:
        res = False
    _GLYPH_CACHE[key] = res
    return res


def _font_for_char(char, size, bold=False):
    """Pick the best available font able to render `char` (or None)."""
    if _EMOJI_RE.match(char):
        families = ("emoji", "latin")
    else:
        family = _script_family(char)
        families = (family, "latin") if family != "latin" else ("latin",)
    for family in families:
        for path in _get_font_paths(family, bold):
            font = _get_font_from_path(path, size)
            if font is not None and _char_has_glyph(font, char):
                return font
    return None


def _make_runs(text, size, bold):
    """Split text into (chunk, font) runs, dropping unsupported characters."""
    runs = []
    for ch in text or "":
        font = _font_for_char(ch, size, bold)
        if font is None:
            continue
        if runs and runs[-1][1] is font:
            runs[-1] = (runs[-1][0] + ch, font)
        else:
            runs.append((ch, font))
    return runs


def _runs_width(d, runs):
    w = 0.0
    for text, font in runs:
        try:
            w += d.textlength(text, font=font)
        except Exception:
            w += len(text) * 12
    return w


def _draw_runs(d, xy, runs, fill):
    x, y = xy
    for text, font in runs:
        try:
            d.text((x, y), text, font=font, fill=fill)
        except Exception:
            pass
        try:
            x += d.textlength(text, font=font)
        except Exception:
            x += len(text) * 12


def _truncate_runs(d, text, size, bold, max_w):
    """Build font runs for `text`, truncated to fit `max_w` px (with '...')."""
    runs = _make_runs(text, size, bold)
    if _runs_width(d, runs) <= max_w:
        return runs
    while runs and _runs_width(d, runs) + 8 > max_w:
        last_text, last_font = runs[-1]
        if len(last_text) > 1:
            runs[-1] = (last_text[:-1], last_font)
        else:
            runs.pop()
    if runs:
        dot_font = runs[-1][1]
        if _runs_width(d, runs) + d.textlength("…", font=dot_font) <= max_w:
            runs.append(("…", dot_font))
    return runs


def clean_name_for_image(name):
    if not name:
        return "Unknown"
    # Remove control chars, zero-width joiners, bidi marks, variation selectors.
    name = re.sub(r"[\x00-\x1f\x7f\u200d\u200c\u200e\u200f\ufe0f]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def generate_leaderboard_image(entries, mode_label="OVERALL", group_title=""):
    """Render the dark/red leaderboard PNG and return its bytes.

    The height auto-fits the number of ranked users (max 10) so there is no
    large empty area below the last row. Returns None when there is nothing
    to display (no ranked users), letting the caller fall back to a
    text-only leaderboard.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        raise RuntimeError(f"Pillow is not available: {e}")

    top_entries = [e for e in (entries or []) if int(e.get("total_messages") or 0) > 0][:10]
    if not top_entries:
        return None

    W = 900
    PAD = 28
    BG = (17, 17, 21)
    CARD = (28, 28, 36)
    ACCENT = (235, 57, 87)
    HI = (238, 238, 244)
    LO = (142, 142, 158)
    BAR_BG = (43, 43, 54)
    MEDAL = {1: (255, 208, 64), 2: (206, 213, 224), 3: (206, 127, 52)}

    n = len(top_entries)
    row_h = 74
    head_h = 158
    H = head_h + n * row_h + 34

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    title_font = _get_font(46, bold=True)
    sub_font = _get_font(19, bold=True)
    name_font = _get_font(24, bold=True)
    cnt_font = _get_font(24, bold=True)

    # ── Header: title + underline + "MODE • Group name" ──
    tw = d.textlength("LEADERBOARD", font=title_font or None)
    d.text(((W - tw) / 2, 24), "LEADERBOARD", font=title_font, fill=ACCENT)
    d.rounded_rectangle([W / 2 - 120, 92, W / 2 + 120, 100], radius=4, fill=ACCENT)

    gt = clean_name_for_image(group_title)
    sub_text = f"{mode_label}  •  {gt}" if gt else mode_label
    sub_runs = _truncate_runs(d, sub_text, 19, True, W - 2 * PAD)
    sub_w = _runs_width(d, sub_runs)
    _draw_runs(d, ((W - sub_w) / 2, 112), sub_runs, LO)

    # ── Row geometry ──
    rank_w = d.textlength("10.", font=cnt_font or None) + 18
    cnt_max_w = d.textlength("100,000", font=cnt_font or None) + 10
    name_x = PAD + rank_w
    content_right = W - PAD
    name_max_w = content_right - name_x - cnt_max_w

    top = head_h
    max_count = max((int(e.get("total_messages") or 0) for e in top_entries), default=0) or 1

    for i, e in enumerate(top_entries):
        y = top + i * row_h
        rank = i + 1
        count = int(e.get("total_messages") or 0)

        # Card background
        d.rounded_rectangle([PAD, y + 2, W - PAD, y + row_h - 8], radius=14, fill=CARD)

        # Rank number (gold/silver/bronze for top 3)
        rank_color = MEDAL.get(rank, HI)
        d.text((PAD + 18, y + 10), f"{rank}.", font=cnt_font, fill=rank_color)

        # Name — unicode runs, truncated to the available width
        name_runs = _truncate_runs(d, clean_name_for_image(e.get("display_name")), 24, True, name_max_w)
        if not name_runs:
            name_runs = _make_runs("Unknown", 24, True)
        _draw_runs(d, (name_x, y + 10), name_runs, HI)

        # Message count — right aligned
        cnt_str = format_num(count)
        cw = d.textlength(cnt_str, font=cnt_font or None)
        d.text((content_right - cw, y + 10), cnt_str, font=cnt_font, fill=HI)

        # Proportional progress bar (longest = #1)
        by = y + 46
        bh = 6
        bx = name_x
        bw = content_right - bx
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=BAR_BG)
        fill_w = max(4, int(bw * (count / max_count)))
        if fill_w > bw:
            fill_w = bw
        d.rounded_rectangle([bx, by, bx + fill_w, by + bh], radius=bh // 2,
                            fill=(MEDAL[1] if rank == 1 else ACCENT))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# MILESTONE ANNOUNCEMENTS
# ─────────────────────────────────────────────────────────────
def announce_user_milestone(bot, chat_id, display_name, milestone):
    name = html.escape(str(display_name or "User"))
    text = (
        f"🎉 <b>Chat Milestone!</b>\n\n"
        f"👤 {name} has reached <b>{format_num(milestone)} messages</b> in this group! 💬"
    )
    bot.send_message(chat_id, text, parse_mode="HTML")


def announce_group_milestone(bot, chat_id, milestone):
    text = (
        f"🔥 <b>Group Chat Milestone!</b>\n\n"
        f"💬 This group has reached <b>{format_num(milestone)} messages today!</b>"
    )
    bot.send_message(chat_id, text, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────
# MESSAGE TRACKING ENTRY POINT
# ─────────────────────────────────────────────────────────────
def track_group_message(bot, message):
    """Record a single valid group message. Never raises (failures are logged)."""
    try:
        chat = message.chat
        user = message.from_user
        if chat is None or user is None:
            return
        if chat.type not in ("group", "supergroup"):
            return
        if getattr(user, "is_bot", False):
            return
        if not chat_activity_enabled():
            return

        group = db.get_group(chat.id)
        if not group or not group.get("chat_tracking", 1):
            return

        today = get_today()
        week = get_week_start()
        name = get_display_name(user)
        username = getattr(user, "username", None)

        user_ms = list(MILESTONES) if group.get("user_milestones", 1) else []
        group_ms = list(MILESTONES) if group.get("group_milestones", 1) else []

        result = db.track_chat_message(chat.id, user.id, name, username, today, week, user_ms, group_ms)

        for m in result.get("user_milestones", []):
            try:
                announce_user_milestone(bot, chat.id, name, m)
            except Exception as e:
                logger.error(f"User milestone announce failed: {e}")

        for m in result.get("group_milestones", []):
            try:
                announce_group_milestone(bot, chat.id, m)
            except Exception as e:
                logger.error(f"Group milestone announce failed: {e}")
    except Exception as e:
        logger.error(f"Chat activity tracking error: {e}")


# ─────────────────────────────────────────────────────────────
# LEADERBOARD DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────
_PAGE_SIZE = 10


def _ref_key(mode):
    if mode == "today":
        return get_today()
    if mode == "week":
        return get_week_start()
    return None


def show_leaderboard(bot, chat_id, mode, chat_title=None):
    """Send a new leaderboard message (image + text + buttons)."""
    ref_key = _ref_key(mode)
    entries = db.get_chat_rankings(chat_id, mode=mode, ref_key=ref_key, limit=10, offset=0)
    total = db.get_chat_total_messages(chat_id, mode=mode, ref_key=ref_key)
    text = build_text_leaderboard(mode, entries, total)
    markup = build_mode_markup(mode, chat_id)

    img = None
    try:
        img = generate_leaderboard_image(entries, _MODE_LABEL.get(mode, "Overall").upper(), chat_title)
    except Exception as e:
        logger.warning(f"Leaderboard image generation failed: {e}")

    if img:
        try:
            bio = io.BytesIO(img)
            bio.name = "leaderboard.png"
            bot.send_photo(chat_id, bio, caption=text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception as e:
            logger.warning(f"Leaderboard image send failed, falling back to text: {e}")

    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


def edit_leaderboard(bot, call, mode, chat_id):
    """Update the existing leaderboard message with a fresh image + caption."""
    ref_key = _ref_key(mode)
    entries = db.get_chat_rankings(chat_id, mode=mode, ref_key=ref_key, limit=10, offset=0)
    total = db.get_chat_total_messages(chat_id, mode=mode, ref_key=ref_key)
    text = build_text_leaderboard(mode, entries, total)
    markup = build_mode_markup(mode, chat_id)

    chat_title = getattr(call.message.chat, "title", None)
    img = None
    try:
        img = generate_leaderboard_image(entries, _MODE_LABEL.get(mode, "Overall").upper(), chat_title)
    except Exception as e:
        logger.warning(f"Leaderboard image generation failed (edit): {e}")

    if img and getattr(call.message, "photo", None):
        try:
            from telebot.types import InputMediaPhoto
            bio = io.BytesIO(img)
            bio.name = "leaderboard.png"
            media = InputMediaPhoto(bio, caption=text, parse_mode="HTML")
            bot.edit_message_media(media, chat_id=chat_id, message_id=call.message.message_id, reply_markup=markup)
            bot.answer_callback_query(call.id)
            return
        except Exception as e:
            logger.warning(f"edit_message_media failed, editing caption only: {e}")

    try:
        bot.edit_message_caption(
            caption=text, chat_id=chat_id, message_id=call.message.message_id,
            reply_markup=markup, parse_mode="HTML",
        )
    except Exception:
        try:
            bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=call.message.message_id,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            pass
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


def show_full_leaderboard(bot, call, mode, chat_id, page):
    """Complete leaderboard with pagination (edits the existing message)."""
    ref_key = _ref_key(mode)
    total_users = db.count_chat_rankings(chat_id, mode=mode, ref_key=ref_key)
    total_pages = max(1, -(-total_users // _PAGE_SIZE))
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    offset = (page - 1) * _PAGE_SIZE
    entries = db.get_chat_rankings(chat_id, mode=mode, ref_key=ref_key, limit=_PAGE_SIZE, offset=offset)
    total_msgs = db.get_chat_total_messages(chat_id, mode=mode, ref_key=ref_key)
    text = build_text_leaderboard(mode, entries, total_msgs, full=True)
    text += f"\n\n<i>Page {page}/{total_pages}</i>"
    markup = build_full_markup(mode, chat_id, page, total_pages)
    try:
        bot.edit_message_caption(
            caption=text, chat_id=chat_id, message_id=call.message.message_id,
            reply_markup=markup, parse_mode="HTML",
        )
    except Exception:
        try:
            bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=call.message.message_id,
                reply_markup=markup, parse_mode="HTML",
            )
        except Exception:
            pass
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# ACTIVITY SETTINGS PANEL
# ─────────────────────────────────────────────────────────────
_ACTIVITY_FLAGS = [
    ("chat_tracking", "👁️ Chat tracking"),
    ("user_milestones", "🎉 User milestones"),
    ("group_milestones", "🔥 Group milestones"),
    ("leaderboard", "📊 Leaderboard"),
]


def build_activity_panel(chat_id, group):
    text = "📊 <b>Chat Activity System</b>\n\n"
    for key, label in _ACTIVITY_FLAGS:
        val = bool(group.get(key, 1))
        icon = "✅" if val else "❌"
        text += f"{icon} <b>{label}:</b> {'ON' if val else 'OFF'}\n"
    text += "\n<i>Use the buttons below to toggle each option.</i>"

    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("👁️ Chat Tracking", callback_data=f"chatcfg:toggle:chat_tracking:{chat_id}"),
        InlineKeyboardButton("🎉 User Milestones", callback_data=f"chatcfg:toggle:user_milestones:{chat_id}"),
    )
    mk.row(
        InlineKeyboardButton("🔥 Group Milestones", callback_data=f"chatcfg:toggle:group_milestones:{chat_id}"),
        InlineKeyboardButton("📊 Leaderboard", callback_data=f"chatcfg:toggle:leaderboard:{chat_id}"),
    )
    mk.row(InlineKeyboardButton("🔄 Refresh", callback_data=f"chatcfg:refresh:{chat_id}"))
    return text, mk


# ─────────────────────────────────────────────────────────────
# HANDLER REGISTRATION
# ─────────────────────────────────────────────────────────────
def register_chat_handlers(bot):

    # ── /rankings + /top (alias) — both open the same leaderboard ──
    def _open_leaderboard(message):
        if message.chat.type not in ("group", "supergroup"):
            try:
                bot.reply_to(message, "📊 This command can only be used in groups.")
            except Exception:
                pass
            return
        if not chat_activity_enabled():
            try:
                bot.reply_to(message, "📊 The chat activity system is currently disabled globally.")
            except Exception:
                pass
            return
        try:
            group = db.get_group(message.chat.id)
            if group and not group.get("leaderboard", 1):
                bot.reply_to(message, "📊 The leaderboard is disabled in this group by an administrator.")
                return
            show_leaderboard(bot, message.chat.id, "overall", chat_title=getattr(message.chat, "title", None))
        except Exception as e:
            logger.error(f"/rankings error: {e}")
            try:
                bot.reply_to(message, "❌ Could not generate the leaderboard. Please try again later.")
            except Exception:
                pass

    @bot.message_handler(commands=["rankings", "top"])
    def cmd_rankings(message):
        _open_leaderboard(message)

    # ── /mystats (personal chat stats, current group only) ──
    @bot.message_handler(commands=["mystats"])
    def cmd_mystats(message):
        if message.chat.type not in ("group", "supergroup"):
            try:
                bot.reply_to(message, "📊 This command can only be used in groups.")
            except Exception:
                pass
            return
        if not chat_activity_enabled():
            try:
                bot.reply_to(message, "📊 The chat activity system is currently disabled globally.")
            except Exception:
                pass
            return
        try:
            user = message.from_user
            if user is None:
                bot.reply_to(message, "❌ Could not identify you. Please try again.")
                return
            stats = db.get_user_chat_stats(
                message.chat.id,
                user.id,
                get_today(),
                get_week_start(),
            )
            text = build_my_stats_text(get_display_name(user), getattr(user, "username", None), stats)
            bot.reply_to(message, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"/mystats error: {e}")
            try:
                bot.reply_to(message, "❌ Could not load your statistics. Please try again later.")
            except Exception:
                pass

    # ── /chatstats (group-wide chat stats) ──
    @bot.message_handler(commands=["chatstats"])
    def cmd_chatstats(message):
        if message.chat.type not in ("group", "supergroup"):
            try:
                bot.reply_to(message, "📊 This command can only be used in groups.")
            except Exception:
                pass
            return
        if not chat_activity_enabled():
            try:
                bot.reply_to(message, "📊 The chat activity system is currently disabled globally.")
            except Exception:
                pass
            return
        try:
            stats = db.get_chat_group_stats(message.chat.id, get_today(), get_week_start())
            text = build_chat_stats_text(stats)
            bot.reply_to(message, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"/chatstats error: {e}")
            try:
                bot.reply_to(message, "❌ Could not load the group statistics. Please try again later.")
            except Exception:
                pass

    # ── /activity (admin settings) ──
    @bot.message_handler(commands=["activity"])
    def cmd_activity(message):
        if message.chat.type not in ("group", "supergroup"):
            return
        if not chat_activity_enabled():
            try:
                bot.reply_to(message, "📊 The chat activity system is currently disabled globally.")
            except Exception:
                pass
            return
        if not _is_admin(bot, message.chat.id, message.from_user.id) and not _is_owner(message.from_user.username):
            return _reply_not_admin(bot, message)
        group = db.get_group(message.chat.id)
        text, mk = build_activity_panel(message.chat.id, group)
        try:
            bot.reply_to(message, text, reply_markup=mk, parse_mode="HTML")
        except Exception as e:
            logger.error(f"/activity error: {e}")

    # ── /resetactivity (admin reset of a group's chat stats) ──
    @bot.message_handler(commands=["resetactivity"])
    def cmd_resetactivity(message):
        if message.chat.type not in ("group", "supergroup"):
            return
        if not chat_activity_enabled():
            try:
                bot.reply_to(message, "📊 The chat activity system is currently disabled globally.")
            except Exception:
                pass
            return
        if not _is_admin(bot, message.chat.id, message.from_user.id) and not _is_owner(message.from_user.username):
            return _reply_not_admin(bot, message)
        try:
            db.reset_chat_user_stats(message.chat.id)
            bot.reply_to(message, "🔄 Chat activity statistics for this group have been reset.")
        except Exception as e:
            logger.error(f"/resetactivity error: {e}")
            bot.reply_to(message, "❌ Failed to reset statistics.")

    # ── Leaderboard inline buttons ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("rank:"))
    def rankings_callback(call):
        if not chat_activity_enabled():
            bot.answer_callback_query(call.id, "📊 Chat activity is disabled.")
            return
        parts = call.data.split(":")
        try:
            action = parts[1]
            if action == "mode":
                mode = parts[2]
                chat_id = int(parts[3])
            elif action == "full":
                mode = parts[2]
                chat_id = int(parts[3])
                page = int(parts[4])
            elif action == "back":
                mode = parts[2]
                chat_id = int(parts[3])
            else:
                bot.answer_callback_query(call.id, "❌ Invalid data.")
                return
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "❌ Invalid data.")
            return

        # Safety: the interaction must stay tied to the message's own group
        if int(call.message.chat.id) != int(chat_id):
            bot.answer_callback_query(call.id, "❌ Not allowed for this group.")
            return

        try:
            if action == "mode":
                edit_leaderboard(bot, call, mode, chat_id)
            elif action == "full":
                show_full_leaderboard(bot, call, mode, chat_id, page)
            elif action == "back":
                edit_leaderboard(bot, call, mode, chat_id)
        except Exception as e:
            logger.error(f"rankings callback error: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Failed to update. Please run /rankings again.", show_alert=True)
            except Exception:
                pass

    # ── Activity settings inline buttons ──
    @bot.callback_query_handler(func=lambda call: call.data.startswith("chatcfg:"))
    def activity_callback(call):
        if not chat_activity_enabled():
            bot.answer_callback_query(call.id, "📊 Chat activity is disabled.")
            return
        parts = call.data.split(":")
        try:
            chat_id = int(parts[-1])
        except (ValueError, IndexError):
            bot.answer_callback_query(call.id, "❌ Invalid data.")
            return
        if int(call.message.chat.id) != int(chat_id):
            bot.answer_callback_query(call.id, "❌ Not allowed for this group.")
            return
        if not _is_admin(bot, chat_id, call.from_user.id) and not _is_owner(call.from_user.username):
            return _reply_not_admin(bot, call)
        try:
            if parts[1] == "toggle":
                key = parts[2]
                group = db.get_group(chat_id)
                new_val = 0 if bool(group.get(key, 1)) else 1
                db.update_group_setting(chat_id, key, new_val)
                bot.answer_callback_query(call.id, "✅ Updated.")
            else:
                bot.answer_callback_query(call.id, "🔄 Refreshed.")
            group = db.get_group(chat_id)
            text, mk = build_activity_panel(chat_id, group)
            try:
                bot.edit_message_text(
                    text=text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                    reply_markup=mk, parse_mode="HTML",
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"activity callback error: {e}")
            try:
                bot.answer_callback_query(call.id, "❌ Failed to update.", show_alert=True)
            except Exception:
                pass
