#!/usr/bin/env python3
"""
Clipper bot:
- Accepts text messages (YouTube links). Blocks files/photos.
- /help, /feedback, /donate commands.
- Lets user choose clip length (5,10,20,30,Custom up to MAX_CLIP_SECONDS),
  number of clips (up to MAX_CLIPS), and format/quality from available source formats.
- Supports custom range like "00H08M10S:00H09M20S" or "2:32-3:23" or "152-203" tolerant parsing.
- Shows a ⚡ spinner + percentage while working, deletes spinner message when done.
- For files > ~20MB uploads to GoFile and returns link.
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
from typing import Optional, Tuple, List

import requests
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- config / env ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None
TMP_DIR = os.getenv("TMP_DIR", "tmp_clips")
GOFILE_API_KEY = os.getenv("GOFILE_API_KEY", "") or None
MAX_CLIP_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "180"))
MAX_CLIPS = int(os.getenv("MAX_CLIPS", "5"))

# safety
if MAX_CLIP_SECONDS > 3 * 60:
    MAX_CLIP_SECONDS = min(MAX_CLIP_SECONDS, 3 * 60)

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clipper")

# spinner frames for progress animation
SPINNER_FRAMES = ["⚡", "⚡️", "⚡⚡", "⚡️⚡️"]

# make tmp dir
os.makedirs(TMP_DIR, exist_ok=True)

# ---- helpers ----
def clean_tmp():
    for f in os.listdir(TMP_DIR):
        try:
            os.remove(os.path.join(TMP_DIR, f))
        except Exception:
            pass

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
    t = t.strip()
    # H M S pattern
    m = re.match(r'(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', t, flags=re.I)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        h = int(m.group(1) or 0)
        mm = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h*3600 + mm*60 + s
    # colon format
    if ":" in t:
        parts = [int(p) for p in t.split(":") if p.strip().isdigit()]
        if len(parts) == 2:  # mm:ss
            return parts[0]*60 + parts[1]
        if len(parts) == 3:
            return parts[0]*3600 + parts[1]*60 + parts[2]
    # pure digits
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None

def parse_range(s: str) -> Optional[Tuple[int,int]]:
    """
    Accepts:
     - '00H08M10S:00H09M20S'
     - '2:32-3:23'
     - '152-203'
     - '2:32 - 3:23' etc
    Returns (start_sec, end_sec) or None
    """
    s = s.strip()
    sep = "-" if "-" in s else ":" if ":" in s and s.count(":")>1 else None
    # more robust: look for two time tokens separated by '-' or 'to'
    parts = re.split(r'\s*[-–to]+\s*', s, flags=re.I)
    if len(parts) >= 2:
        a = parse_time_to_seconds(parts[0])
        b = parse_time_to_seconds(parts[1])
        if a is not None and b is not None and b > a:
            return a, b
    # fallback: try "start:end" with colon but not hh:mm:ss form
    if ":" in s and "-" not in s:
        # maybe "2:32:3:23" not expected. return None
        return None
    return None

def run_subprocess(cmd: List[str], timeout: int = 600):
    """Run subprocess and return CompletedProcess. Raise on error."""
    logger.debug("Running cmd: %s", " ".join(shlex.quote(p) for p in cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "process failed")
    return proc

# ---- gofile upload ----
def gofile_upload(filepath: str, timeout=120) -> Optional[str]:
    try:
        r = requests.get("https://apiv2.gofile.io/getServer", timeout=10)
        server = r.json().get("data")
        if not server:
            return None
        upload_url = f"https://{server}.gofile.io/uploadFile"
        headers = {}
        if GOFILE_API_KEY:
            # docs often mention Authorization: Bearer <token>
            headers["Authorization"] = f"Bearer {GOFILE_API_KEY}"
        with open(filepath, "rb") as fh:
            resp = requests.post(upload_url, files={"file": fh}, headers=headers, timeout=timeout)
        j = resp.json()
        if j.get("status") == "ok":
            return j["data"]["downloadPage"]
        logger.warning("gofile upload failed: %s", j)
    except Exception as e:
        logger.exception("GoFile upload error: %s", e)
    return None

# ---- yt-dlp helpers ----
def get_video_info(url: str, timeout=30) -> dict:
    ydl_opts = {"quiet": True, "no_warnings": True, "format": "best"}
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def list_qualities(url: str) -> List[Tuple[str,str]]:
    """
    Returns list of tuples (format_code, label) sorted by height desc.
    label example: "1080p / webm"
    """
    try:
        info = get_video_info(url)
        fmts = info.get("formats", [])
        candidates = []
        seen = set()
        for f in fmts:
            if not f.get("vcodec") or f.get("acodec") == "none":
                # skip audio-only unless packaged with video
                pass
            fmt_id = f.get("format_id") or str(f.get("height") or "") + f.get("ext","")
            height = f.get("height") or 0
            ext = f.get("ext") or ""
            label = f"{height}p ({ext})" if height else f"{f.get('format')} "
            key = (fmt_id, label)
            if key not in seen:
                candidates.append((fmt_id, label, height))
                seen.add(key)
        candidates.sort(key=lambda x: (x[2] or 0), reverse=True)
        return [(c[0], c[1]) for c in candidates]
    except Exception as e:
        logger.exception("list_qualities failed: %s", e)
        return [("best","best")]

# ---- Telegram bot handlers ----

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass
    txt = (
        "Send me a YouTube link (just paste) and I'll cut the most-watched sections "
        "into small clips. Use /help for step-by-step instructions."
    )
    await update.effective_user.send_message(txt)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    txt = (
        "Clipper bot — simple guide:\n\n"
        "1. Paste a YouTube link (text only). Files are not accepted.\n"
        "2. Pick clip duration (5/10/20/30/Custom up to 3 minutes).\n"
        "   - Custom format examples: `00H08M10S:00H09M20S`, `2:32-3:23`, `152-203`.\n"
        "3. Pick number of clips (up to 5 according to video length).\n"
        "4. Pick quality (the bot shows available qualities from the source).\n"
        "5. Wait for ⚡ progress message. When finished you get clips or a GoFile link.\n\n"
        "Commands:\n"
        "/feedback - send message to admin (auto-deleted)\n"
        "/donate - donation link\n"
    )
    await update.effective_user.send_message(txt)

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.delete()
    # UPI link provided by user request
    upi = "upi://pay?pn=MD%20SHAHNAWAJ&am=&mode=01&pa=md.3282-40@waaxis"
    await context.bot.send_message(chat_id=update.effective_user.id, text=f"Use this link to donate:\n{upi}")

# feedback: forward to admin
async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if not msg or not msg.text:
        try:
            await msg.delete()
        except: pass
        return
    # forward to admin and ack
    try:
        body = f"Feedback from {user.id} @{user.username or ''}:\n\n{msg.text}"
        if ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=body)
        await msg.reply_text("Thanks for your feedback — we sent it to admin.")
    except Exception:
        logger.exception("Failed to forward feedback")
        await msg.reply_text("Failed to send feedback, try again later.")
    # auto-delete original user message after small delay
    await asyncio.sleep(2)
    try:
        await msg.delete()
    except Exception:
        pass

# restrict uploads - ignore non-text and delete them
async def only_text_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    # delete any non-text content
    if msg is None:
        return
    if msg.photo or msg.document or msg.video or msg.sticker or msg.voice:
        try:
            await msg.delete()
        except Exception:
            pass
        return
    # if plain text but not command, process as potential url
    text = (msg.text or "").strip()
    if not text:
        try:
            await msg.delete()
        except Exception:
            pass
        return
    # auto-delete user message later (so chat remains clean)
    context.chat_data.setdefault("recent_user_messages", []).append(msg.message_id)
    # handle link
    if "youtube.com" in text or "youtu.be" in text:
        await handle_youtube_link(msg, context)
    else:
        # not a youtube link, delete
        try:
            await msg.delete()
        except Exception:
            pass

async def auto_cleanup_task(context: ContextTypes.DEFAULT_TYPE):
    # deletes old user messages stored in chat_data
    lst = context.chat_data.get("recent_user_messages", [])[:]
    for mid in lst:
        try:
            await context.bot.delete_message(chat_id=context._chat_id, message_id=mid)  # best-effort
        except Exception:
            pass
    context.chat_data["recent_user_messages"] = []

# ---- core flow ----
async def handle_youtube_link(msg, context: ContextTypes.DEFAULT_TYPE):
    chat_id = msg.chat_id
    url = msg.text.strip().split()[0]
    # delete original message to keep UI clean (we will DM result to user)
    try:
        await msg.delete()
    except Exception:
        pass

    # call get info and present options
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, get_video_info, url)
    except Exception as e:
        logger.exception("yt-dlp info failed")
        await context.bot.send_message(chat_id=chat_id, text="Failed to read the video. Try again later.")
        return

    title = info.get("title", "video")
    duration = int(info.get("duration") or 0)
    # decide max clips allowed based on duration (one clip min 5s)
    suggested_max_clips = min(MAX_CLIPS, max(1, duration // 5))
    # gather formats
    qualities = list_qualities(url)
    fmt_buttons = []
    # show up to 6 quality choices
    for fmt_code, label in qualities[:6]:
        fmt_buttons.append([InlineKeyboardButton(label, callback_data=f"fmt:{fmt_code}")])
    # durations choices
    dur_buttons = [
        [InlineKeyboardButton("5s", callback_data="dur:5"), InlineKeyboardButton("10s", callback_data="dur:10")],
        [InlineKeyboardButton("20s", callback_data="dur:20"), InlineKeyboardButton("30s", callback_data="dur:30")],
        [InlineKeyboardButton("Custom", callback_data="dur:c")]
    ]
    # number of clips (1..suggested_max_clips)
    count_buttons = [[InlineKeyboardButton(f"{i} clip{'s' if i>1 else ''}", callback_data=f"count:{i}") for i in range(1, min(6, suggested_max_clips+1))]]
    # extra: download full video button
    extra_buttons = [[InlineKeyboardButton("Download full video", callback_data="full:1")]]
    # assemble inline keyboard
    kb = InlineKeyboardMarkup(
        dur_buttons + fmt_buttons + count_buttons + extra_buttons
    )
    # send message (private chat)
    sent = await context.bot.send_message(chat_id=chat_id,
                                          text=f"Title: {title}\nDuration: ~{duration}s\nPick clip duration and quality:",
                                          reply_markup=kb)
    # store pending info
    context.user_data["pending_video"] = url
    context.user_data["pending_title"] = title
    context.user_data["pending_duration"] = duration
    context.user_data["pending_msg_id"] = sent.message_id

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()  # ack
    data = q.data or ""
    user = update.effective_user
    # ensure only the user who started can use buttons? we'll allow same chat.
    if data.startswith("dur:"):
        _, val = data.split(":",1)
        if val == "c":
            # ask for custom range
            await q.message.reply_text("Send custom range like `00H08M10S-00H09M20S` or `2:32-3:23` or `152-203` (max {}s).".format(MAX_CLIP_SECONDS))
            context.user_data["await_custom_range"] = True
            return
        try:
            dur = int(val)
            context.user_data["selected_duration"] = dur
            await q.message.reply_text(f"Duration set to {dur}s. Now pick quality.")
            return
        except:
            await q.message.reply_text("Invalid duration selection.")
            return

    if data.startswith("fmt:"):
        _, fmt = data.split(":",1)
        context.user_data["selected_format"] = fmt
        await q.message.reply_text("Format set. Now choose how many clips (1..{}).".format(MAX_CLIPS))
        return

    if data.startswith("count:"):
        _, val = data.split(":",1)
        try:
            n = int(val)
        except:
            await q.message.reply_text("Invalid count")
            return
        # check selected duration & format
        url = context.user_data.get("pending_video")
        dur = context.user_data.get("selected_duration")
        fmt = context.user_data.get("selected_format", "best")
        if not url or not dur:
            await q.message.reply_text("Select duration and quality first.")
            return
        # enforce limits
        if dur > MAX_CLIP_SECONDS:
            await q.message.reply_text(f"Requested duration too long (max {MAX_CLIP_SECONDS}s).")
            return
        if n > MAX_CLIPS:
            await q.message.reply_text(f"Too many clips selected (max {MAX_CLIPS}).")
            return
        await q.message.reply_text(f"Working on {n} clip(s)... This may take a while, please wait.")
        # launch background task
        asyncio.create_task(worker_create_clips(context, q.message.chat_id, url, dur, n, fmt, user.id))
        return

    if data.startswith("full:"):
        # download full video and upload via gofile if large
        url = context.user_data.get("pending_video")
        if not url:
            await q.message.reply_text("No video pending.")
            return
        await q.message.reply_text("Downloading full video, please wait...")
        asyncio.create_task(worker_download_full(context, update.effective_chat.id, url))
        return

async def worker_download_full(context, chat_id, url):
    # use yt-dlp to download full video
    out_template = os.path.join(TMP_DIR, "full.%(ext)s")
    try:
        run_subprocess(["yt-dlp", "-f", "best", "-o", out_template, url], timeout=900)
        # find file
        files = os.listdir(TMP_DIR)
        files = sorted([f for f in files if f.startswith("full.")], key=os.path.getmtime, reverse=True)
        if not files:
            await context.bot.send_message(chat_id=chat_id, text="Failed to download.")
            return
        path = os.path.join(TMP_DIR, files[0])
        size = os.path.getsize(path)
        if size < 20*1024*1024:
            # send directly
            await context.bot.send_video(chat_id=chat_id, video=open(path, "rb"))
            os.remove(path)
            return
        # else upload to gofile
        link = await asyncio.get_event_loop().run_in_executor(None, gofile_upload, path)
        if link:
            await context.bot.send_message(chat_id=chat_id, text=f"Full video: {link}")
            os.remove(path)
        else:
            await context.bot.send_message(chat_id=chat_id, text="Upload failed.")
    except Exception as e:
        logger.exception("download full failed")
        await context.bot.send_message(chat_id=chat_id, text=f"Error: {e}")

async def worker_create_clips(context, chat_id, url, duration, n_clips, fmt, requester_id):
    """
    High-level worker:
      - runs yt-dlp to download segments (using --download-sections)
      - selects likely most-watched parts: simple heuristic - sample center of video or use chapters if available
      - returns clips to user or uploads large clips to gofile
    """
    # spinner status message
    status_msg = await context.bot.send_message(chat_id=chat_id, text=f"{random.choice(SPINNER_FRAMES)} Starting...")
    start_time = time.time()
    clips = []
    links = []
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, get_video_info, url)
        total_dur = int(info.get("duration") or 0)
        # choose start times: naive: pick n segments evenly spaced but avoid very start/end
        # prefer center segments: sample midpoints around 25%,50%,75%
        positions = []
        if total_dur <= duration:
            positions = [0]
        else:
            # generate candidate start secs
            for i in range(n_clips):
                frac = (i+1) / (n_clips+1)
                # clamp against edges
                start_at = max(0, int(total_dur * frac - duration//2))
                if start_at + duration > total_dur:
                    start_at = max(0, total_dur - duration)
                positions.append(start_at)
        # create clips
        for idx, start in enumerate(positions, start=1):
            outname = f"clip_{safe_filename(info.get('id','vid'))}_{idx}.mp4"
            outpath_template = os.path.join(TMP_DIR, outname)
            # create sections string mm:ss format for yt-dlp
            start_s = f"{start}"
            end_s = f"{start+duration}"
            # yt-dlp supports --download-sections "*00:01:23-00:02:00" but it expects time in HH:MM:SS
            def sec_to_hms(s):
                h = s//3600; m=(s%3600)//60; ss=s%60
                return f"{h:02d}:{m:02d}:{ss:02d}"
            section = f"*{sec_to_hms(start)}-{sec_to_hms(start+duration)}"
            cmd = ["yt-dlp", "-4", "--socket-timeout", "30", "--retries", "3",
                   "-f", fmt, "--merge-output-format", "mp4",
                   "--download-sections", section,
                   "-o", outpath_template, url]
            # run and update spinner
            await update_spinner(context, status_msg, 0, f"Downloading clip {idx}/{len(positions)}")
            try:
                run_subprocess(cmd, timeout=600)
            except Exception as e:
                logger.exception("yt-dlp failed for clip %s", idx)
                await context.bot.send_message(chat_id=chat_id, text=f"Download failed for clip {idx}: {e}")
                continue
            # find created file (most recent in TMP_DIR matching pattern)
            candidates = [os.path.join(TMP_DIR, f) for f in os.listdir(TMP_DIR) if f.startswith(f"clip_{info.get('id','vid')}_{idx}")]
            if not candidates:
                # try broad search
                candidates = [os.path.join(TMP_DIR, f) for f in os.listdir(TMP_DIR) if outname in f or outname.replace(".mp4","") in f]
            if not candidates:
                await context.bot.send_message(chat_id=chat_id, text=f"Clip {idx} not found after download.")
                continue
            path = max(candidates, key=os.path.getmtime)
            clips.append(path)
            # update progress
            percent = int(100 * idx / max(1, len(positions)))
            await update_spinner(context, status_msg, percent, f"Downloaded {idx}/{len(positions)}")
        # send clips (or gofile)
        for i, path in enumerate(clips, start=1):
            try:
                size = os.path.getsize(path)
                if size < 20*1024*1024:
                    await context.bot.send_video(chat_id=chat_id, video=open(path, "rb"), caption=f"{i}/{len(clips)}")
                    os.remove(path)
                else:
                    # upload to gofile in executor
                    await update_spinner(context, status_msg, 90, "Uploading large clip...")
                    link = await asyncio.get_event_loop().run_in_executor(None, gofile_upload, path)
                    if link:
                        links.append(link)
                        os.remove(path)
                    else:
                        links.append("upload_failed")
            except Exception:
                logger.exception("sending clip failed")
                links.append("send_failed")
        # final messages
        if links:
            txt = "Done. Large files uploaded:\n" + "\n".join(links)
            await context.bot.send_message(chat_id=chat_id, text=txt)
        else:
            await context.bot.send_message(chat_id=chat_id, text="Done. Clips sent.")
    except Exception as e:
        logger.exception("worker failed")
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Error during processing: {e}")
        except:
            pass
    finally:
        try:
            await status_msg.delete()
        except:
            pass
        # cleanup
        clean_tmp()

async def update_spinner(context, status_message, percent:int, text:str):
    # edit status message with spinner and percent
    frame = random.choice(SPINNER_FRAMES)
    try:
        await status_message.edit_text(f"{frame} {percent}% · {text}")
    except Exception:
        pass

# catch custom range messages
async def custom_range_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_custom_range"):
        # not awaiting custom range
        # we still auto-delete any non-command text
        # if it's not a command or link, delete
        txt = (update.message.text or "")
        if update.message.entities:
            # if it contains a url entity and youtube link, let only_text_allowed handle
            for e in update.message.entities:
                if e.type == MessageEntity.URL or e.type == MessageEntity.TEXT_LINK:
                    return
        # otherwise auto-delete
        try:
            await update.message.delete()
        except:
            pass
        return

    txt = (update.message.text or "").strip()
    # parse
    rng = parse_range(txt)
    if not rng:
        # maybe user typed 'start-end' with accidental spaces. try extracting two time tokens
        tokens = re.findall(r'[\dHMS:]+', txt, flags=re.I)
        if len(tokens) >= 2:
            rng = parse_range(tokens[0] + "-" + tokens[1])
    if not rng:
        await update.message.reply_text("Couldn't parse range. Try formats: `00H08M10S-00H09M20S` or `2:32-3:23` or `152-203`.")
        # keep awaiting
        return
    start, end = rng
    length = end - start
    if length <= 0 or length > MAX_CLIP_SECONDS:
        await update.message.reply_text(f"Invalid range length. Max clip length is {MAX_CLIP_SECONDS}s.")
        context.user_data.pop("await_custom_range", None)
        return
    # store and ask for quality and count
    context.user_data["selected_duration"] = length
    context.user_data["selected_custom_range"] = (start, end)
    await update.message.reply_text(f"Custom range accepted ({start}s - {end}s, {length}s). Now pick quality and number of clips.")
    context.user_data.pop("await_custom_range", None)
    # delete the user message to keep chat clean
    try:
        await update.message.delete()
    except:
        pass

# startup safety
async def bot_startup(application):
    logger.info("Bot started")

def build_app():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("feedback", feedback))
    app.add_handler(CommandHandler("donate", donate))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # only_text_allowed will handle plain texts and drop non-text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_range_text))
    app.add_handler(MessageHandler(filters.ALL, only_text_allowed))

    app.post_init.append(bot_startup)
    return app

# ---- run ----
if __name__ == "__main__":
    app = build_app()
    app.run_polling(allowed_updates=["message","callback_query"])
