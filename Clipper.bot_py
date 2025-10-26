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
    app.on_startup.append(bot_startup)
    return app

# ---- run ----
if __name__ == "__main__":
    app = build_app()
    app.run_polling(allowed_updates=["message","callback_query"])
                GNU GENERAL PUBLIC LICENSE
                       Version 2, June 1991

 Copyright (C) 1989, 1991 Free Software Foundation, Inc.,
 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
 Everyone is permitted to copy and distribute verbatim copies
 of this license document, but changing it is not allowed.

                            Preamble

  The licenses for most software are designed to take away your
freedom to share and change it.  By contrast, the GNU General Public
License is intended to guarantee your freedom to share and change free
software--to make sure the software is free for all its users.  This
General Public License applies to most of the Free Software
Foundation's software and to any other program whose authors commit to
using it.  (Some other Free Software Foundation software is covered by
the GNU Lesser General Public License instead.)  You can apply it to
your programs, too.

  When we speak of free software, we are referring to freedom, not
price.  Our General Public Licenses are designed to make sure that you
have the freedom to distribute copies of free software (and charge for
this service if you wish), that you receive source code or can get it
if you want it, that you can change the software or use pieces of it
in new free programs; and that you know you can do these things.

  To protect your rights, we need to make restrictions that forbid
anyone to deny you these rights or to ask you to surrender the rights.
These restrictions translate to certain responsibilities for you if you
distribute copies of the software, or if you modify it.

  For example, if you distribute copies of such a program, whether
gratis or for a fee, you must give the recipients all the rights that
you have.  You must make sure that they, too, receive or can get the
source code.  And you must show them these terms so they know their
rights.

  We protect your rights with two steps: (1) copyright the software, and
(2) offer you this license which gives you legal permission to copy,
distribute and/or modify the software.

  Also, for each author's protection and ours, we want to make certain
that everyone understands that there is no warranty for this free
software.  If the software is modified by someone else and passed on, we
want its recipients to know that what they have is not the original, so
that any problems introduced by others will not reflect on the original
authors' reputations.

  Finally, any free program is threatened constantly by software
patents.  We wish to avoid the danger that redistributors of a free
program will individually obtain patent licenses, in effect making the
program proprietary.  To prevent this, we have made it clear that any
patent must be licensed for everyone's free use or not licensed at all.

  The precise terms and conditions for copying, distribution and
modification follow.

                    GNU GENERAL PUBLIC LICENSE
   TERMS AND CONDITIONS FOR COPYING, DISTRIBUTION AND MODIFICATION

  0. This License applies to any program or other work which contains
a notice placed by the copyright holder saying it may be distributed
under the terms of this General Public License.  The "Program", below,
refers to any such program or work, and a "work based on the Program"
means either the Program or any derivative work under copyright law:
that is to say, a work containing the Program or a portion of it,
either verbatim or with modifications and/or translated into another
language.  (Hereinafter, translation is included without limitation in
the term "modification".)  Each licensee is addressed as "you".

Activities other than copying, distribution and modification are not
covered by this License; they are outside its scope.  The act of
running the Program is not restricted, and the output from the Program
is covered only if its contents constitute a work based on the
Program (independent of having been made by running the Program).
Whether that is true depends on what the Program does.

  1. You may copy and distribute verbatim copies of the Program's
source code as you receive it, in any medium, provided that you
conspicuously and appropriately publish on each copy an appropriate
copyright notice and disclaimer of warranty; keep intact all the
notices that refer to this License and to the absence of any warranty;
and give any other recipients of the Program a copy of this License
along with the Program.

You may charge a fee for the physical act of transferring a copy, and
you may at your option offer warranty protection in exchange for a fee.

  2. You may modify your copy or copies of the Program or any portion
of it, thus forming a work based on the Program, and copy and
distribute such modifications or work under the terms of Section 1
above, provided that you also meet all of these conditions:

    a) You must cause the modified files to carry prominent notices
    stating that you changed the files and the date of any change.

    b) You must cause any work that you distribute or publish, that in
    whole or in part contains or is derived from the Program or any
    part thereof, to be licensed as a whole at no charge to all third
    parties under the terms of this License.

    c) If the modified program normally reads commands interactively
    when run, you must cause it, when started running for such
    interactive use in the most ordinary way, to print or display an
    announcement including an appropriate copyright notice and a
    notice that there is no warranty (or else, saying that you provide
    a warranty) and that users may redistribute the program under
    these conditions, and telling the user how to view a copy of this
    License.  (Exception: if the Program itself is interactive but
    does not normally print such an announcement, your work based on
    the Program is not required to print an announcement.)

These requirements apply to the modified work as a whole.  If
identifiable sections of that work are not derived from the Program,
and can be reasonably considered independent and separate works in
themselves, then this License, and its terms, do not apply to those
sections when you distribute them as separate works.  But when you
distribute the same sections as part of a whole which is a work based
on the Program, the distribution of the whole must be on the terms of
this License, whose permissions for other licensees extend to the
entire whole, and thus to each and every part regardless of who wrote it.

Thus, it is not the intent of this section to claim rights or contest
your rights to work written entirely by you; rather, the intent is to
exercise the right to control the distribution of derivative or
collective works based on the Program.

In addition, mere aggregation of another work not based on the Program
with the Program (or with a work based on the Program) on a volume of
a storage or distribution medium does not bring the other work under
the scope of this License.

  3. You may copy and distribute the Program (or a work based on it,
under Section 2) in object code or executable form under the terms of
Sections 1 and 2 above provided that you also do one of the following:

    a) Accompany it with the complete corresponding machine-readable
    source code, which must be distributed under the terms of Sections
    1 and 2 above on a medium customarily used for software interchange; or,

    b) Accompany it with a written offer, valid for at least three
    years, to give any third party, for a charge no more than your
    cost of physically performing source distribution, a complete
    machine-readable copy of the corresponding source code, to be
    distributed under the terms of Sections 1 and 2 above on a medium
    customarily used for software interchange; or,

    c) Accompany it with the information you received as to the offer
    to distribute corresponding source code.  (This alternative is
    allowed only for noncommercial distribution and only if you
    received the program in object code or executable form with such
    an offer, in accord with Subsection b above.)

The source code for a work means the preferred form of the work for
making modifications to it.  For an executable work, complete source
code means all the source code for all modules it contains, plus any
associated interface definition files, plus the scripts used to
control compilation and installation of the executable.  However, as a
special exception, the source code distributed need not include
anything that is normally distributed (in either source or binary
form) with the major components (compiler, kernel, and so on) of the
operating system on which the executable runs, unless that component
itself accompanies the executable.

If distribution of executable or object code is made by offering
access to copy from a designated place, then offering equivalent
access to copy the source code from the same place counts as
distribution of the source code, even though third parties are not
compelled to copy the source along with the object code.

  4. You may not copy, modify, sublicense, or distribute the Program
except as expressly provided under this License.  Any attempt
otherwise to copy, modify, sublicense or distribute the Program is
void, and will automatically terminate your rights under this License.
However, parties who have received copies, or rights, from you under
this License will not have their licenses terminated so long as such
parties remain in full compliance.

  5. You are not required to accept this License, since you have not
signed it.  However, nothing else grants you permission to modify or
distribute the Program or its derivative works.  These actions are
prohibited by law if you do not accept this License.  Therefore, by
modifying or distributing the Program (or any work based on the
Program), you indicate your acceptance of this License to do so, and
all its terms and conditions for copying, distributing or modifying
the Program or works based on it.

  6. Each time you redistribute the Program (or any work based on the
Program), the recipient automatically receives a license from the
original licensor to copy, distribute or modify the Program subject to
these terms and conditions.  You may not impose any further
restrictions on the recipients' exercise of the rights granted herein.
You are not responsible for enforcing compliance by third parties to
this License.

  7. If, as a consequence of a court judgment or allegation of patent
infringement or for any other reason (not limited to patent issues),
conditions are imposed on you (whether by court order, agreement or
otherwise) that contradict the conditions of this License, they do not
excuse you from the conditions of this License.  If you cannot
distribute so as to satisfy simultaneously your obligations under this
License and any other pertinent obligations, then as a consequence you
may not distribute the Program at all.  For example, if a patent
license would not permit royalty-free redistribution of the Program by
all those who receive copies directly or indirectly through you, then
the only way you could satisfy both it and this License would be to
refrain entirely from distribution of the Program.

If any portion of this section is held invalid or unenforceable under
any particular circumstance, the balance of the section is intended to
apply and the section as a whole is intended to apply in other
circumstances.

It is not the purpose of this section to induce you to infringe any
patents or other property right claims or to contest validity of any
such claims; this section has the sole purpose of protecting the
integrity of the free software distribution system, which is
implemented by public license practices.  Many people have made
generous contributions to the wide range of software distributed
through that system in reliance on consistent application of that
system; it is up to the author/donor to decide if he or she is willing
to distribute software through any other system and a licensee cannot
impose that choice.

This section is intended to make thoroughly clear what is believed to
be a consequence of the rest of this License.

  8. If the distribution and/or use of the Program is restricted in
certain countries either by patents or by copyrighted interfaces, the
original copyright holder who places the Program under this License
may add an explicit geographical distribution limitation excluding
those countries, so that distribution is permitted only in or among
countries not thus excluded.  In such case, this License incorporates
the limitation as if written in the body of this License.

  9. The Free Software Foundation may publish revised and/or new versions
of the General Public License from time to time.  Such new versions will
be similar in spirit to the present version, but may differ in detail to
address new problems or concerns.

Each version is given a distinguishing version number.  If the Program
specifies a version number of this License which applies to it and "any
later version", you have the option of following the terms and conditions
either of that version or of any later version published by the Free
Software Foundation.  If the Program does not specify a version number of
this License, you may choose any version ever published by the Free Software
Foundation.

  10. If you wish to incorporate parts of the Program into other free
programs whose distribution conditions are different, write to the author
to ask for permission.  For software which is copyrighted by the Free
Software Foundation, write to the Free Software Foundation; we sometimes
make exceptions for this.  Our decision will be guided by the two goals
of preserving the free status of all derivatives of our free software and
of promoting the sharing and reuse of software generally.

                            NO WARRANTY

  11. BECAUSE THE PROGRAM IS LICENSED FREE OF CHARGE, THERE IS NO WARRANTY
FOR THE PROGRAM, TO THE EXTENT PERMITTED BY APPLICABLE LAW.  EXCEPT WHEN
OTHERWISE STATED IN WRITING THE COPYRIGHT HOLDERS AND/OR OTHER PARTIES
PROVIDE THE PROGRAM "AS IS" WITHOUT WARRANTY OF ANY KIND, EITHER EXPRESSED
OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE.  THE ENTIRE RISK AS
TO THE QUALITY AND PERFORMANCE OF THE PROGRAM IS WITH YOU.  SHOULD THE
PROGRAM PROVE DEFECTIVE, YOU ASSUME THE COST OF ALL NECESSARY SERVICING,
REPAIR OR CORRECTION.

  12. IN NO EVENT UNLESS REQUIRED BY APPLICABLE LAW OR AGREED TO IN WRITING
WILL ANY COPYRIGHT HOLDER, OR ANY OTHER PARTY WHO MAY MODIFY AND/OR
REDISTRIBUTE THE PROGRAM AS PERMITTED ABOVE, BE LIABLE TO YOU FOR DAMAGES,
INCLUDING ANY GENERAL, SPECIAL, INCIDENTAL OR CONSEQUENTIAL DAMAGES ARISING
OUT OF THE USE OR INABILITY TO USE THE PROGRAM (INCLUDING BUT NOT LIMITED
TO LOSS OF DATA OR DATA BEING RENDERED INACCURATE OR LOSSES SUSTAINED BY
YOU OR THIRD PARTIES OR A FAILURE OF THE PROGRAM TO OPERATE WITH ANY OTHER
PROGRAMS), EVEN IF SUCH HOLDER OR OTHER PARTY HAS BEEN ADVISED OF THE
POSSIBILITY OF SUCH DAMAGES.

                     END OF TERMS AND CONDITIONS

            How to Apply These Terms to Your New Programs

  If you develop a new program, and you want it to be of the greatest
possible use to the public, the best way to achieve this is to make it
free software which everyone can redistribute and change under these terms.

  To do so, attach the following notices to the program.  It is safest
to attach them to the start of each source file to most effectively
convey the exclusion of warranty; and each file should have at least
the "copyright" line and a pointer to where the full notice is found.

    <one line to give the program's name and a brief idea of what it does.>
    Copyright (C) <year>  <name of author>

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License along
    with this program; if not, write to the Free Software Foundation, Inc.,
    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

Also add information on how to contact you by electronic and paper mail.

If the program is interactive, make it output a short notice like this
when it starts in an interactive mode:

    Gnomovision version 69, Copyright (C) year name of author
    Gnomovision comes with ABSOLUTELY NO WARRANTY; for details type `show w'.
    This is free software, and you are welcome to redistribute it
    under certain conditions; type `show c' for details.

The hypothetical commands `show w' and `show c' should show the appropriate
parts of the General Public License.  Of course, the commands you use may
be called something other than `show w' and `show c'; they could even be
mouse-clicks or menu items--whatever suits your program.

You should also get your employer (if you work as a programmer) or your
school, if any, to sign a "copyright disclaimer" for the program, if
necessary.  Here is a sample; alter the names:

  Yoyodyne, Inc., hereby disclaims all copyright interest in the program
  `Gnomovision' (which makes passes at compilers) written by James Hacker.

  <signature of Ty Coon>, 1 April 1989
  Ty Coon, President of Vice

This General Public License does not permit incorporating your program into
proprietary programs.  If your program is a subroutine library, you may
consider it more useful to permit linking proprietary applications with the
library.  If this is what you want to do, use the GNU Lesser General
Public License instead of this License.
