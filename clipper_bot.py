#!/usr/bin/env python3
"""
Clipper bot: (Refactored & UI Enhanced)
- Accepts text messages (YouTube links). Blocks files/photos.
- /help, /feedback, /donate commands.
- Features a single, self-updating inline menu for:
  - Clip length (5,10,20,30,Custom up to MAX_CLIP_SECONDS)
  - Number of clips (up to MAX_CLIPS)
  - Format/quality from available source formats.
- Supports custom range like "00H08M10S:00H09M20S" or "2:32-3:23" or "152-203".
- Shows a ⚡ spinner + percentage while working, deletes spinner when done.
- For files > 50MB uploads to GoFile and returns link (Telegram's limit is 50MB).
- Auto-deletes user messages and commands to keep chat clean.
"""
import os
import re
import shlex
import json
import time
import random
import shutil
import asyncio
import logging
import subprocess
from typing import Optional, Tuple, List, Dict, Any

import requests
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- 1. Config / Env ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
TMP_DIR = os.getenv("TMP_DIR", "tmp_clips")
GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "") or None
MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "180"))
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "5"))
# Telegram's file limit is 50MB, not 20MB.
TELEGRAM_FILE_LIMIT_MB = 49

# Safety
if MAX_CLIP_SECONDS > 10 * 60:  # Increase safety limit slightly
    MAX_CLIP_SECONDS = min(MAX_CLIP_SECONDS, 10 * 60)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("clipper")

# Spinner
SPINNER_FRAMES = ["⚡", "⚡️", "⚡⚡", "⚡️⚡️"]

# Make tmp dir
os.makedirs(TMP_DIR, exist_ok=True)


# ---- 2. Time & String Helpers ----

def clean_tmp():
    """Best-effort cleanup of TMP_DIR"""
    for f in os.listdir(TMP_DIR):
        try:
            path = os.path.join(TMP_DIR, f)
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"Failed to clean tmp file {f}: {e}")

def safe_filename(name: str):
    return re.sub(r"[^\w\-.]", "_", name)[:180]

def parse_time_to_seconds(t: str) -> Optional[int]:
    """
    Accepts many formats:
      - "00H08M10S"
      - "2:32" => 152
      - "2:32:10"
      - "152" => 152
    Returns seconds or None
    """
    t = t.strip().upper()
    # H M S pattern
    m = re.match(r'(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', t)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mm * 60 + s
    # colon format
    if ":" in t:
        parts = [int(p) for p in t.split(":") if p.strip().isdigit()]
        if len(parts) == 2:  # mm:ss
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:  # hh:mm:ss
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    # pure digits
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None

def parse_range(s: str) -> Optional[Tuple[int, int]]:
    """
    Accepts:
     - '00H08M10S:00H09M20S'
     - '2:32-3:23'
     - '152-203'
     - '2:32 - 3:23' etc
    Returns (start_sec, end_sec) or None
    """
    s = s.strip()
    parts = re.split(r'\s*[-–to:aws]+\s*', s, flags=re.I)
    
    if len(parts) == 2: # 'start-end' or 'start:end'
        a = parse_time_to_seconds(parts[0])
        b = parse_time_to_seconds(parts[1])
        if a is not None and b is not None and b > a:
            return a, b
            
    # Try to find two time-like strings
    tokens = re.findall(r'[\dHMS:]+', s, flags=re.I)
    if len(tokens) >= 2:
        a = parse_time_to_seconds(tokens[0])
        b = parse_time_to_seconds(tokens[-1]) # Use first and last
        if a is not None and b is not None and b > a:
            return a, b
            
    return None

def sec_to_hms(s: int) -> str:
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    return f"{h:02d}:{m:02d}:{ss:02d}"

def sec_to_human(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


# ---- 3. Blocking I/O Helpers (to be run in executor) ----

def run_subprocess_sync(cmd: List[str], timeout: int = 600):
    """Run subprocess and return stdout. Raise on error."""
    logger.info("Running cmd: %s", " ".join(shlex.quote(p) for p in cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8'
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "process failed")
        return proc.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Process timed out after {timeout}s")
    except Exception as e:
        logger.error(f"Subprocess failed: {e}")
        raise

def gofile_upload_sync(filepath: str, timeout=120) -> Optional[str]:
    """Uploads a file to GoFile. Blocking."""
    if not GOFILE_API_KEY:
        logger.warning("GOFILE_API_KEY not set. Cannot upload.")
        return None
    try:
        r_server = requests.get("https://api.gofile.io/getServer", timeout=10)
        r_server.raise_for_status()
        server = r_server.json().get("data", {}).get("server")
        if not server:
            logger.error("GoFile: Could not get server.")
            return None
        
        upload_url = f"https://{server}.gofile.io/uploadFile"
        headers = {"Authorization": f"Bearer {GOFILE_API_KEY}"}
        
        with open(filepath, "rb") as fh:
            files = {"file": (os.path.basename(filepath), fh)}
            resp = requests.post(upload_url, files=files, headers=headers, timeout=timeout)
        
        resp.raise_for_status()
        j = resp.json()
        
        if j.get("status") == "ok":
            return j["data"]["downloadPage"]
        else:
            logger.warning("GoFile upload failed: %s", j)
            return None
    except Exception as e:
        logger.exception("GoFile upload error: %s", e)
    return None

def get_video_info_sync(url: str, timeout=30) -> dict:
    """Gets video info using yt-dlp. Blocking."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best",
        "socket_timeout": timeout,
    }
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def list_qualities_sync(info: dict) -> List[Tuple[str, str, int]]:
    """
    Returns list of tuples (format_code, label, height) sorted by height desc.
    label example: "1080p (webm)"
    """
    fmts = info.get("formats", [])
    candidates = []
    seen = set()
    for f in fmts:
        # We need formats that have both video and audio, or are video-only
        # (yt-dlp will merge with best audio).
        if f.get("vcodec") == "none":
            continue
            
        fmt_id = f.get("format_id")
        height = f.get("height") or 0
        ext = f.get("ext") or "unknown"
        vcodec = f.get("vcodec", "unknown")
        
        # Create a more descriptive label
        label = f"{height}p" if height else f.get("format_note", "video")
        if "avc" in vcodec:
            label += " (mp4)"
        elif "vp9" in vcodec:
            label += " (webm)"
        else:
            label += f" ({ext})"
            
        key = (height, ext) # De-duplicate based on height and extension
        if key not in seen and fmt_id:
            candidates.append((fmt_id, label, height))
            seen.add(key)
            
    # Sort by height descending
    candidates.sort(key=lambda x: x[2], reverse=True)
    
    if not candidates:
        return [("best", "best", 0)]
        
    return [(c[0], c[1], c[2]) for c in candidates]


# ---- 4. Telegram UI Generation ----

async def generate_menu_message(context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, InlineKeyboardMarkup]:
    """Generates the text and keyboard for the main UI menu."""
    data = context.user_data
    state = data.get("state", {})
    
    # Get selections
    sel_dur_val = state.get("duration")
    sel_dur_range = state.get("custom_range")
    sel_count = state.get("count")
    sel_fmt = state.get("format")
    sel_fmt_label = state.get("format_label", sel_fmt)

    # --- Build Text ---
    text = f"*Video:* `{data.get('title', '...')}`\n"
    text += f"*Duration:* `{sec_to_human(data.get('duration', 0))}`\n\n"
    text += "*Your Selections:*\n"
    
    # Duration text
    if sel_dur_range:
        start, end = sel_dur_range
        text += f" • *Range:* `{sec_to_hms(start)} - {sec_to_hms(end)}` ({end-start}s)\n"
    elif sel_dur_val:
        text += f" • *Clip Length:* `{sel_dur_val}s`\n"
    else:
        text += f" • *Clip Length:* `(Not set)`\n"

    # Count text
    if sel_dur_range:
        text += f" • *Num Clips:* `1 (Custom Range)`\n"
        sel_count = 1 # Force count to 1 for custom range
        state["count"] = 1
    else:
        text += f" • *Num Clips:* `{sel_count or '(Not set)'}`\n"

    # Format text
    text += f" • *Quality:* `{sel_fmt_label or '(Not set)'}`\n"

    # --- Build Keyboard ---
    keyboard = []
    
    # Row 1: Duration
    dur_buttons = [
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 5 else ''}5s", callback_data="set:dur:5"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 10 else ''}10s", callback_data="set:dur:10"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 20 else ''}20s", callback_data="set:dur:20"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 30 else ''}30s", callback_data="set:dur:30"),
    ]
    keyboard.append(dur_buttons)
    
    # Row 2: Custom Range
    custom_label = f"✅ Custom Range" if sel_dur_range else "Custom Range"
    keyboard.append([InlineKeyboardButton(custom_label, callback_data="set:dur:custom")])
    
    # Row 3: Clip Count (disabled if custom range is set)
    if not sel_dur_range:
        max_clips_allowed = min(MAX_CLIPS, max(1, data.get('duration', 0) // 5))
        count_buttons = []
        for i in range(1, max_clips_allowed + 1):
            count_buttons.append(
                InlineKeyboardButton(f"{'✅ ' if sel_count == i else ''}{i}", callback_data=f"set:count:{i}")
            )
        keyboard.append(count_buttons)

    # Row 4+: Quality
    qualities = data.get("qualities", [])
    # Group qualities into rows of 2
    for i in range(0, len(qualities[:6]), 2):
        q_row = []
        for fmt_id, label, height in qualities[i:i+2]:
            q_row.append(
                InlineKeyboardButton(f"{'✅ ' if sel_fmt == fmt_id else ''}{label}", callback_data=f"set:fmt:{fmt_id}:{label}")
            )
        keyboard.append(q_row)

    # Final Row: Start / Download / Cancel
    final_row = [InlineKeyboardButton("❌ Cancel", callback_data="action:cancel")]
    
    # Check if ready to start
    if (sel_dur_val or sel_dur_range) and sel_count and sel_fmt:
        final_row.insert(0, InlineKeyboardButton("✨ START CLIPPING ✨", callback_data="action:start"))
    
    final_row.append(InlineKeyboardButton("💾 Full Video", callback_data="action:full"))
    keyboard.append(final_row)
    
    return text, InlineKeyboardMarkup(keyboard)

async def clear_menu(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Deletes the main menu and clears user_data state."""
    menu_msg_id = context.user_data.get("menu_msg_id")
    if menu_msg_id:
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=context._chat_id,
                message_id=menu_msg_id,
                reply_markup=None
            )
        except BadRequest:
            pass # Message might be deleted already
    context.user_data.clear()


# ---- 5. Telegram Handlers ----

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    await update.effective_user.send_message(
        "Send me a YouTube link (just paste) and I'll help you cut clips.\n"
        "Use /help for full instructions."
    )
    context.user_data.clear()

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    txt = (
        "*Clipper Bot Guide:*\n\n"
        "1. Paste a YouTube link.\n"
        "2. A menu will appear. Select your desired *clip length*, *number of clips*, and *quality*.\n"
        "3. Once all three are set, a `✨ START CLIPPING ✨` button will appear.\n\n"
        "*Custom Range:*\n"
        " • Select `Custom Range`.\n"
        " • The bot will ask you to send a range.\n"
        " • Reply with a range like `2:32-3:23`, `152-203`, or `00H08M10S-00H09M20S`.\n"
        " • This will automatically set *Num Clips* to 1.\n\n"
        "*Other Buttons:*\n"
        " • `💾 Full Video`: Downloads the entire video.\n"
        " • `❌ Cancel`: Cancels the current operation.\n\n"
        "*Commands:*\n"
        " /feedback <your message> - Send a message to the admin.\n"
        " /donate - Get donation info."
    )
    await update.effective_user.send_message(txt, parse_mode=ParseMode.MARKDOWN)

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    upi = "upi://pay?pn=MD%20SHAHNAWAJ&am=&mode=01&pa=md.3282-40@waaxis"
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=f"Thank you for considering a donation!\n\nUse this link:\n`{upi}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    feedback_text = msg.text.partition(' ')[2] # Get text after /feedback

    if not feedback_text:
        await msg.delete()
        await msg.reply_text("Please write your feedback after the command, e.g., `/feedback This bot is great!`")
        return

    body = f"Feedback from {user.id} (@{user.username or 'N/A'}):\n\n{feedback_text}"
    
    try:
        if ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=body)
        await msg.reply_text("Thanks, your feedback has been sent to the admin.")
    except Exception as e:
        logger.exception("Failed to forward feedback")
        await msg.reply_text("Sorry, failed to send feedback. Please try again later.")
    
    await asyncio.sleep(2)
    try:
        await msg.delete()
    except Exception:
        pass

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for all non-command text."""
    msg = update.message
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    
    # --- 1. Check if we are awaiting a custom range ---
    if context.user_data.get("await_custom_range"):
        await msg.delete() # Delete user's range message
        rng = parse_range(text)
        if not rng:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Couldn't parse range. Try `2:32-3:23` or `152-203`.",
                reply_to_message_id=context.user_data.get("menu_msg_id")
            )
            return # Keep awaiting

        start, end = rng
        length = end - start
        if length <= 0 or length > MAX_CLIP_SECONDS:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Invalid range. Max clip length is {MAX_CLIP_SECONDS}s.",
                reply_to_message_id=context.user_data.get("menu_msg_id")
            )
            return

        # Success! Save state and update menu
        context.user_data["await_custom_range"] = False
        state = context.user_data.setdefault("state", {})
        state["custom_range"] = (start, end)
        state["duration"] = None # Clear fixed duration
        state["count"] = 1 # Custom range is always 1 clip
        
        # Update the menu
        menu_text, keyboard = await generate_menu_message(context)
        await context.bot.edit_message_text(
            text=menu_text,
            chat_id=chat_id,
            message_id=context.user_data.get("menu_msg_id"),
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # --- 2. Check if it's a YouTube link ---
    is_youtube = "youtube.com" in text or "youtu.be" in text
    if not is_youtube:
        await msg.delete() # Not a link, not a range, delete it
        return

    # --- 3. Process YouTube Link ---
    await msg.delete()
    url = text.split()[0]
    
    # Clear any old state
    context.user_data.clear()
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"{random.choice(SPINNER_FRAMES)} Fetching video info...")
    context.user_data["menu_msg_id"] = status_msg.message_id
    
    try:
        # Run blocking I/O in executor
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, get_video_info_sync, url)
        
        title = info.get("title", "video")
        duration = int(info.get("duration") or 0)
        
        if duration == 0:
            raise ValueError("Could not get video duration (maybe it's a live stream?)")
            
        qualities = await loop.run_in_executor(None, list_qualities_sync, info)
        
        # Store all info in user_data
        context.user_data["video_url"] = url
        context.user_data["title"] = title
        context.user_data["duration"] = duration
        context.user_data["qualities"] = qualities
        context.user_data["state"] = {} # To store selections
        
        # Generate and show the full menu
        menu_text, keyboard = await generate_menu_message(context)
        await status_msg.edit_text(
            text=menu_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.exception("Failed to get video info")
        await status_msg.edit_text(f"❌ Failed to read video.\nError: {e}")
        context.user_data.clear()


async def files_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes any non-text, non-command messages."""
    try:
        await update.message.delete()
    except Exception:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses from the inline menu."""
    q = update.callback_query
    await q.answer()
    
    data = (q.data or "").split(":")
    action_type = data[0]
    action_key = data[1]
    
    state = context.user_data.setdefault("state", {})
    
    if action_type == "set":
        # --- Handle setting a value (dur, count, fmt) ---
        if action_key == "dur":
            val = data[2]
            if val == "custom":
                context.user_data["await_custom_range"] = True
                await q.edit_message_text(
                    f"{q.message.text}\n\n*Please send your custom range now* (e.g., `1:10-1:30`).",
                    reply_markup=q.message.reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            else:
                state["duration"] = int(val)
                state["custom_range"] = None # Clear custom range if fixed one is set
        
        elif action_key == "count":
            state["count"] = int(data[2])
            
        elif action_key == "fmt":
            state["format"] = data[2]
            state["format_label"#!/usr/bin/env python3
"""
Clipper bot: (Refactored & UI Enhanced)
- Accepts text messages (YouTube links). Blocks files/photos.
- /help, /feedback, /donate commands.
- Features a single, self-updating inline menu for:
  - Clip length (5,10,20,30,Custom up to MAX_CLIP_SECONDS)
  - Number of clips (up to MAX_CLIPS)
  - Format/quality from available source formats.
- Supports custom range like "00H08M10S:00H09M20S" or "2:32-3:23" or "152-203".
- Shows a ⚡ spinner + percentage while working, deletes spinner when done.
- For files > 50MB uploads to GoFile and returns link (Telegram's limit is 50MB).
- Auto-deletes user messages and commands to keep chat clean.
"""
import os
import re
import shlex
import json
import time
import random
import shutil
import asyncio
import logging
import subprocess
from typing import Optional, Tuple, List, Dict, Any

import requests
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- 1. Config / Env ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
TMP_DIR = os.getenv("TMP_DIR", "tmp_clips")
GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "") or None
MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "180"))
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "5"))
# Telegram's file limit is 50MB, not 20MB.
TELEGRAM_FILE_LIMIT_MB = 49

# Safety
if MAX_CLIP_SECONDS > 10 * 60:  # Increase safety limit slightly
    MAX_CLIP_SECONDS = min(MAX_CLIP_SECONDS, 10 * 60)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("clipper")

# Spinner
SPINNER_FRAMES = ["⚡", "⚡️", "⚡⚡", "⚡️⚡️"]

# Make tmp dir
os.makedirs(TMP_DIR, exist_ok=True)


# ---- 2. Time & String Helpers ----

def clean_tmp():
    """Best-effort cleanup of TMP_DIR"""
    for f in os.listdir(TMP_DIR):
        try:
            path = os.path.join(TMP_DIR, f)
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            logger.warning(f"Failed to clean tmp file {f}: {e}")

def safe_filename(name: str):
    return re.sub(r"[^\w\-.]", "_", name)[:180]

def parse_time_to_seconds(t: str) -> Optional[int]:
    """
    Accepts many formats:
      - "00H08M10S"
      - "2:32" => 152
      - "2:32:10"
      - "152" => 152
    Returns seconds or None
    """
    t = t.strip().upper()
    # H M S pattern
    m = re.match(r'(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', t)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mm * 60 + s
    # colon format
    if ":" in t:
        parts = [int(p) for p in t.split(":") if p.strip().isdigit()]
        if len(parts) == 2:  # mm:ss
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:  # hh:mm:ss
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    # pure digits
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None

def parse_range(s: str) -> Optional[Tuple[int, int]]:
    """
    Accepts:
     - '00H08M10S:00H09M20S'
     - '2:32-3:23'
     - '152-203'
     - '2:32 - 3:23' etc
    Returns (start_sec, end_sec) or None
    """
    s = s.strip()
    parts = re.split(r'\s*[-–to:aws]+\s*', s, flags=re.I)
    
    if len(parts) == 2: # 'start-end' or 'start:end'
        a = parse_time_to_seconds(parts[0])
        b = parse_time_to_seconds(parts[1])
        if a is not None and b is not None and b > a:
            return a, b
            
    # Try to find two time-like strings
    tokens = re.findall(r'[\dHMS:]+', s, flags=re.I)
    if len(tokens) >= 2:
        a = parse_time_to_seconds(tokens[0])
        b = parse_time_to_seconds(tokens[-1]) # Use first and last
        if a is not None and b is not None and b > a:
            return a, b
            
    return None

def sec_to_hms(s: int) -> str:
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    return f"{h:02d}:{m:02d}:{ss:02d}"

def sec_to_human(s: int) -> str:
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


# ---- 3. Blocking I/O Helpers (to be run in executor) ----

def run_subprocess_sync(cmd: List[str], timeout: int = 600):
    """Run subprocess and return stdout. Raise on error."""
    logger.info("Running cmd: %s", " ".join(shlex.quote(p) for p in cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding='utf-8'
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or "process failed")
        return proc.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Process timed out after {timeout}s")
    except Exception as e:
        logger.error(f"Subprocess failed: {e}")
        raise

def gofile_upload_sync(filepath: str, timeout=120) -> Optional[str]:
    """Uploads a file to GoFile. Blocking."""
    if not GOFILE_API_KEY:
        logger.warning("GOFILE_API_KEY not set. Cannot upload.")
        return None
    try:
        r_server = requests.get("https://api.gofile.io/getServer", timeout=10)
        r_server.raise_for_status()
        server = r_server.json().get("data", {}).get("server")
        if not server:
            logger.error("GoFile: Could not get server.")
            return None
        
        upload_url = f"https://{server}.gofile.io/uploadFile"
        headers = {"Authorization": f"Bearer {GOFILE_API_KEY}"}
        
        with open(filepath, "rb") as fh:
            files = {"file": (os.path.basename(filepath), fh)}
            resp = requests.post(upload_url, files=files, headers=headers, timeout=timeout)
        
        resp.raise_for_status()
        j = resp.json()
        
        if j.get("status") == "ok":
            return j["data"]["downloadPage"]
        else:
            logger.warning("GoFile upload failed: %s", j)
            return None
    except Exception as e:
        logger.exception("GoFile upload error: %s", e)
    return None

def get_video_info_sync(url: str, timeout=30) -> dict:
    """Gets video info using yt-dlp. Blocking."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best",
        "socket_timeout": timeout,
    }
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def list_qualities_sync(info: dict) -> List[Tuple[str, str, int]]:
    """
    Returns list of tuples (format_code, label, height) sorted by height desc.
    label example: "1080p (webm)"
    """
    fmts = info.get("formats", [])
    candidates = []
    seen = set()
    for f in fmts:
        # We need formats that have both video and audio, or are video-only
        # (yt-dlp will merge with best audio).
        if f.get("vcodec") == "none":
            continue
            
        fmt_id = f.get("format_id")
        height = f.get("height") or 0
        ext = f.get("ext") or "unknown"
        vcodec = f.get("vcodec", "unknown")
        
        # Create a more descriptive label
        label = f"{height}p" if height else f.get("format_note", "video")
        if "avc" in vcodec:
            label += " (mp4)"
        elif "vp9" in vcodec:
            label += " (webm)"
        else:
            label += f" ({ext})"
            
        key = (height, ext) # De-duplicate based on height and extension
        if key not in seen and fmt_id:
            candidates.append((fmt_id, label, height))
            seen.add(key)
            
    # Sort by height descending
    candidates.sort(key=lambda x: x[2], reverse=True)
    
    if not candidates:
        return [("best", "best", 0)]
        
    return [(c[0], c[1], c[2]) for c in candidates]


# ---- 4. Telegram UI Generation ----

async def generate_menu_message(context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, InlineKeyboardMarkup]:
    """Generates the text and keyboard for the main UI menu."""
    data = context.user_data
    state = data.get("state", {})
    
    # Get selections
    sel_dur_val = state.get("duration")
    sel_dur_range = state.get("custom_range")
    sel_count = state.get("count")
    sel_fmt = state.get("format")
    sel_fmt_label = state.get("format_label", sel_fmt)

    # --- Build Text ---
    text = f"*Video:* `{data.get('title', '...')}`\n"
    text += f"*Duration:* `{sec_to_human(data.get('duration', 0))}`\n\n"
    text += "*Your Selections:*\n"
    
    # Duration text
    if sel_dur_range:
        start, end = sel_dur_range
        text += f" • *Range:* `{sec_to_hms(start)} - {sec_to_hms(end)}` ({end-start}s)\n"
    elif sel_dur_val:
        text += f" • *Clip Length:* `{sel_dur_val}s`\n"
    else:
        text += f" • *Clip Length:* `(Not set)`\n"

    # Count text
    if sel_dur_range:
        text += f" • *Num Clips:* `1 (Custom Range)`\n"
        sel_count = 1 # Force count to 1 for custom range
        state["count"] = 1
    else:
        text += f" • *Num Clips:* `{sel_count or '(Not set)'}`\n"

    # Format text
    text += f" • *Quality:* `{sel_fmt_label or '(Not set)'}`\n"

    # --- Build Keyboard ---
    keyboard = []
    
    # Row 1: Duration
    dur_buttons = [
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 5 else ''}5s", callback_data="set:dur:5"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 10 else ''}10s", callback_data="set:dur:10"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 20 else ''}20s", callback_data="set:dur:20"),
        InlineKeyboardButton(f"{'✅ ' if sel_dur_val == 30 else ''}30s", callback_data="set:dur:30"),
    ]
    keyboard.append(dur_buttons)
    
    # Row 2: Custom Range
    custom_label = f"✅ Custom Range" if sel_dur_range else "Custom Range"
    keyboard.append([InlineKeyboardButton(custom_label, callback_data="set:dur:custom")])
    
    # Row 3: Clip Count (disabled if custom range is set)
    if not sel_dur_range:
        max_clips_allowed = min(MAX_CLIPS, max(1, data.get('duration', 0) // 5))
        count_buttons = []
        for i in range(1, max_clips_allowed + 1):
            count_buttons.append(
                InlineKeyboardButton(f"{'✅ ' if sel_count == i else ''}{i}", callback_data=f"set:count:{i}")
            )
        keyboard.append(count_buttons)

    # Row 4+: Quality
    qualities = data.get("qualities", [])
    # Group qualities into rows of 2
    for i in range(0, len(qualities[:6]), 2):
        q_row = []
        for fmt_id, label, height in qualities[i:i+2]:
            q_row.append(
                InlineKeyboardButton(f"{'✅ ' if sel_fmt == fmt_id else ''}{label}", callback_data=f"set:fmt:{fmt_id}:{label}")
            )
        keyboard.append(q_row)

    # Final Row: Start / Download / Cancel
    final_row = [InlineKeyboardButton("❌ Cancel", callback_data="action:cancel")]
    
    # Check if ready to start
    if (sel_dur_val or sel_dur_range) and sel_count and sel_fmt:
        final_row.insert(0, InlineKeyboardButton("✨ START CLIPPING ✨", callback_data="action:start"))
    
    final_row.append(InlineKeyboardButton("💾 Full Video", callback_data="action:full"))
    keyboard.append(final_row)
    
    return text, InlineKeyboardMarkup(keyboard)

async def clear_menu(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Deletes the main menu and clears user_data state."""
    menu_msg_id = context.user_data.get("menu_msg_id")
    if menu_msg_id:
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=context._chat_id,
                message_id=menu_msg_id,
                reply_markup=None
            )
        except BadRequest:
            pass # Message might be deleted already
    context.user_data.clear()


# ---- 5. Telegram Handlers ----

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    await update.effective_user.send_message(
        "Send me a YouTube link (just paste) and I'll help you cut clips.\n"
        "Use /help for full instructions."
    )
    context.user_data.clear()

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    txt = (
        "*Clipper Bot Guide:*\n\n"
        "1. Paste a YouTube link.\n"
        "2. A menu will appear. Select your desired *clip length*, *number of clips*, and *quality*.\n"
        "3. Once all three are set, a `✨ START CLIPPING ✨` button will appear.\n\n"
        "*Custom Range:*\n"
        " • Select `Custom Range`.\n"
        " • The bot will ask you to send a range.\n"
        " • Reply with a range like `2:32-3:23`, `152-203`, or `00H08M10S-00H09M20S`.\n"
        " • This will automatically set *Num Clips* to 1.\n\n"
        "*Other Buttons:*\n"
        " • `💾 Full Video`: Downloads the entire video.\n"
        " • `❌ Cancel`: Cancels the current operation.\n\n"
        "*Commands:*\n"
        " /feedback <your message> - Send a message to the admin.\n"
        " /donate - Get donation info."
    )
    await update.effective_user.send_message(txt, parse_mode=ParseMode.MARKDOWN)

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    upi = "upi://pay?pn=MD%20SHAHNAWAJ&am=&mode=01&pa=md.3282-40@waaxis"
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text=f"Thank you for considering a donation!\n\nUse this link:\n`{upi}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    feedback_text = msg.text.partition(' ')[2] # Get text after /feedback

    if not feedback_text:
        await msg.delete()
        await msg.reply_text("Please write your feedback after the command, e.g., `/feedback This bot is great!`")
        return

    body = f"Feedback from {user.id} (@{user.username or 'N/A'}):\n\n{feedback_text}"
    
    try:
        if ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=body)
        await msg.reply_text("Thanks, your feedback has been sent to the admin.")
    except Exception as e:
        logger.exception("Failed to forward feedback")
        await msg.reply_text("Sorry, failed to send feedback. Please try again later.")
    
    await asyncio.sleep(2)
    try:
        await msg.delete()
    except Exception:
        pass

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for all non-command text."""
    msg = update.message
    chat_id = msg.chat_id
    text = (msg.text or "").strip()
    
    # --- 1. Check if we are awaiting a custom range ---
    if context.user_data.get("await_custom_range"):
        await msg.delete() # Delete user's range message
        rng = parse_range(text)
        if not rng:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Couldn't parse range. Try `2:32-3:23` or `152-203`.",
                reply_to_message_id=context.user_data.get("menu_msg_id")
            )
            return # Keep awaiting

        start, end = rng
        length = end - start
        if length <= 0 or length > MAX_CLIP_SECONDS:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Invalid range. Max clip length is {MAX_CLIP_SECONDS}s.",
                reply_to_message_id=context.user_data.get("menu_msg_id")
            )
            return

        # Success! Save state and update menu
        context.user_data["await_custom_range"] = False
        state = context.user_data.setdefault("state", {})
        state["custom_range"] = (start, end)
        state["duration"] = None # Clear fixed duration
        state["count"] = 1 # Custom range is always 1 clip
        
        # Update the menu
        menu_text, keyboard = await generate_menu_message(context)
        await context.bot.edit_message_text(
            text=menu_text,
            chat_id=chat_id,
            message_id=context.user_data.get("menu_msg_id"),
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # --- 2. Check if it's a YouTube link ---
    is_youtube = "youtube.com" in text or "youtu.be" in text
    if not is_youtube:
        await msg.delete() # Not a link, not a range, delete it
        return

    # --- 3. Process YouTube Link ---
    await msg.delete()
    url = text.split()[0]
    
    # Clear any old state
    context.user_data.clear()
    
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"{random.choice(SPINNER_FRAMES)} Fetching video info...")
    context.user_data["menu_msg_id"] = status_msg.message_id
    
    try:
        # Run blocking I/O in executor
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, get_video_info_sync, url)
        
        title = info.get("title", "video")
        duration = int(info.get("duration") or 0)
        
        if duration == 0:
            raise ValueError("Could not get video duration (maybe it's a live stream?)")
            
        qualities = await loop.run_in_executor(None, list_qualities_sync, info)
        
        # Store all info in user_data
        context.user_data["video_url"] = url
        context.user_data["title"] = title
        context.user_data["duration"] = duration
        context.user_data["qualities"] = qualities
        context.user_data["state"] = {} # To store selections
        
        # Generate and show the full menu
        menu_text, keyboard = await generate_menu_message(context)
        await status_msg.edit_text(
            text=menu_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.exception("Failed to get video info")
        await status_msg.edit_text(f"❌ Failed to read video.\nError: {e}")
        context.user_data.clear()


async def files_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes any non-text, non-command messages."""
    try:
        await update.message.delete()
    except Exception:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses from the inline menu."""
    q = update.callback_query
    await q.answer()
    
    data = (q.data or "").split(":")
    action_type = data[0]
    action_key = data[1]
    
    state = context.user_data.setdefault("state", {})
    
    if action_type == "set":
        # --- Handle setting a value (dur, count, fmt) ---
        if action_key == "dur":
            val = data[2]
            if val == "custom":
                context.user_data["await_custom_range"] = True
                await q.edit_message_text(
                    f"{q.message.text}\n\n*Please send your custom range now* (e.g., `1:10-1:30`).",
                    reply_markup=q.message.reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            else:
                state["duration"] = int(val)
                state["custom_range"] = None # Clear custom range if fixed one is set
        
        elif action_key == "count":
            state["count"] = int(data[2])
            
        elif action_key == "fmt":
            state["format"] = data[2]
            state["format_label"
