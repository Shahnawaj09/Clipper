import os
import re
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from dotenv import load_dotenv

import aiohttp
import aiofiles
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
import yt_dlp
from moviepy.editor import VideoFileClip
from aiohttp import web  # add this line

# --- Health server for Render ---
async def health(request):
    return web.Response(text="ok")

async def start_health_server():
    port = int(os.environ.get("PORT", os.environ.get("HTTP_PORT", 8080)))
    app = web.Application()
    app.router.add_get("/healthz", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    # don't block: leave site running in background

# start server task so Render sees an open port
asyncio.create_task(start_health_server())

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip().lstrip('=')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
GOFILE_API_KEY = os.getenv('GOFILE_API_KEY', '').strip()
MAX_CLIP_SECONDS = int(os.getenv('MAX_CLIP_SECONDS', '180'))
MAX_CLIPS = int(os.getenv('MAX_CLIPS', '5'))
HTTP_PORT = int(os.getenv('HTTP_PORT', '8080'))

DOWNLOAD_DIR = Path('downloads')
CLIPS_DIR = Path('clips')
DOWNLOAD_DIR.mkdir(exist_ok=True)
CLIPS_DIR.mkdir(exist_ok=True)

user_states: Dict[int, Dict] = {}
processing_messages: Dict[int, int] = {}
bot_stats = {'clips_created': 0, 'videos_processed': 0, 'total_users': set()}


def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02d}H{minutes:02d}M{secs:02d}S"


def parse_timestamp(timestamp: str) -> Optional[int]:
    patterns = [
        r'(\d+)[hH](\d+)[mM](\d+)[sS]',
        r'(\d+):(\d+):(\d+)',
        r'(\d+)[mM](\d+)[sS]',
        r'(\d+):(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, timestamp)
        if match:
            groups = match.groups()
            if len(groups) == 3:
                return int(groups[0]) * 3600 + int(groups[1]) * 60 + int(groups[2])
            elif len(groups) == 2:
                return int(groups[0]) * 60 + int(groups[1])
    
    if timestamp.isdigit():
        return int(timestamp)
    
    return None


def parse_custom_range(text: str) -> Optional[tuple]:
    auto_corrected = text.replace(' ', '').replace('h', 'H').replace('m', 'M').replace('s', 'S')
    
    parts = auto_corrected.split(':')
    if len(parts) != 2:
        parts = re.split(r'[-,]', auto_corrected)
    
    if len(parts) == 2:
        start = parse_timestamp(parts[0])
        end = parse_timestamp(parts[1])
        if start is not None and end is not None and start < end:
            return (start, end)
    
    return None


async def upload_to_gofile(file_path: Path) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            server_url = 'https://store1.gofile.io/uploadFile'
            
            async with aiofiles.open(file_path, 'rb') as f:
                file_data = await f.read()
            
            data = aiohttp.FormData()
            data.add_field('file', file_data, filename=file_path.name)
            if GOFILE_API_KEY:
                data.add_field('token', GOFILE_API_KEY)
            
            async with session.post(server_url, data=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if result.get('status') == 'ok':
                        return result['data']['downloadPage']
                        
        logger.error(f"GoFile upload failed for {file_path.name}")
        return None
    except Exception as e:
        logger.error(f"GoFile upload error: {e}")
        return None


async def download_video(url: str, user_id: int) -> Optional[tuple]:
    try:
        output_path = DOWNLOAD_DIR / f"{user_id}_{datetime.now().timestamp()}"
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': str(output_path) + '.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            filename = ydl.prepare_filename(info)
            duration = info.get('duration', 0) if isinstance(info, dict) else 0
            title = info.get('title', 'video') if isinstance(info, dict) else 'video'
            
            return (Path(filename), duration, title)
            
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


async def create_clip(video_path: Path, start: int, end: int, output_path: Path) -> bool:
    try:
        clip = VideoFileClip(str(video_path))
        subclip = clip.subclip(start, end)
        subclip.write_videofile(
            str(output_path),
            codec='libx264',
            audio_codec='aac',
            temp_audiofile=str(output_path.parent / 'temp_audio.m4a'),
            remove_temp=True,
            logger=None
        )
        clip.close()
        subclip.close()
        return True
    except Exception as e:
        logger.error(f"Clip creation error: {e}")
        return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    user = update.effective_user
    bot_stats['total_users'].add(user.id)
    
    keyboard = [
        [InlineKeyboardButton("📖 How to Use", callback_data="help")],
        [InlineKeyboardButton("💬 Send Feedback", callback_data="feedback")],
        [InlineKeyboardButton("☕ Donate", callback_data="donate")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        f"👋 <b>Welcome {user.first_name}!</b>\n\n"
        f"🎬 <b>Clipper Bot</b> - Your Video Clipping Assistant\n\n"
        f"✨ I can trim videos from YouTube, Instagram, Twitter, and more!\n\n"
        f"💡 Just send me a video link and I'll help you create perfect clips.\n\n"
        f"Use the buttons below to get started:"
    )
    
    msg = await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    await asyncio.sleep(2)
    await update.message.delete()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    help_text = (
        "📖 <b>How to Use Clipper Bot</b>\n\n"
        "1️⃣ Send me a video link (YouTube, Instagram, Twitter, etc.)\n"
        "2️⃣ Choose your clip length (5s, 10s, 20s, 30s, or Custom)\n"
        "3️⃣ Select how many clips you want (1-5)\n"
        "4️⃣ Wait for processing ⚡\n"
        "5️⃣ Get your download links!\n\n"
        "<b>Custom Format:</b>\n"
        "• <code>00H08M10S:00H09M20S</code> (8m10s to 9m20s)\n"
        "• <code>1:30-2:45</code> (1m30s to 2m45s)\n"
        "• <code>90-150</code> (90s to 150s)\n\n"
        "⚠️ <b>Note:</b> Text links or commands only!"
    )
    
    msg = await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)
    await asyncio.sleep(2)
    await update.message.delete()


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    user_states[user_id] = {'state': 'awaiting_feedback'}
    
    msg = await update.message.reply_text(
        "💬 <b>Send Your Feedback</b>\n\n"
        "Please type your message below:",
        parse_mode=ParseMode.HTML
    )
    
    await asyncio.sleep(2)
    await update.message.delete()


async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    upi_link = "upi://pay?pn=MD%20SHAHNAWAJ&am=&mode=01&pa=md.3282-40@waaxis"
    
    keyboard = [[InlineKeyboardButton("☕ Donate via UPI", url=upi_link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    msg = await update.message.reply_text(
        "☕ <b>Support Clipper Bot</b>\n\n"
        "Your donations help keep this bot running!\n"
        "Thank you for your support! 🙏",
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    await asyncio.sleep(2)
    await update.message.delete()


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    if update.effective_user.id != ADMIN_ID:
        return
    
    stats_text = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: {len(bot_stats['total_users'])}\n"
        f"🎬 Videos Processed: {bot_stats['videos_processed']}\n"
        f"✂️ Clips Created: {bot_stats['clips_created']}\n"
    )
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_id in user_states and user_states[user_id].get('state') == 'awaiting_feedback':
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💬 <b>Feedback from {update.effective_user.first_name}</b>\n\n{text}",
            parse_mode=ParseMode.HTML
        )
        await update.message.reply_text("✅ Thanks for your feedback!")
        del user_states[user_id]
        await asyncio.sleep(3)
        await update.message.delete()
        return
    
    if user_id in user_states and user_states[user_id].get('state') == 'awaiting_custom':
        custom_range = parse_custom_range(text)
        if custom_range:
            start, end = custom_range
            user_states[user_id]['clip_duration'] = end - start
            user_states[user_id]['custom_range'] = (start, end)
            user_states[user_id]['state'] = 'choose_clips'
            
            max_possible = min(MAX_CLIPS, 5)
            keyboard = [[InlineKeyboardButton(f"{i} Clip{'s' if i > 1 else ''}", callback_data=f"clips_{i}")] for i in range(1, max_possible + 1)]
            
            await update.message.reply_text(
                f"✅ Custom range set: {format_timestamp(start)} - {format_timestamp(end)}\n\n"
                f"How many clips do you want?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            await update.message.delete()
        else:
            await update.message.reply_text(
                "❌ Invalid format! Try:\n"
                "• <code>00H08M10S:00H09M20S</code>\n"
                "• <code>1:30-2:45</code>\n"
                "• <code>90-150</code>",
                parse_mode=ParseMode.HTML
            )
            await asyncio.sleep(5)
            await update.message.delete()
        return
    
    url_pattern = r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&/=]*)'
    urls = re.findall(url_pattern, text)
    
    if urls:
        user_states[user_id] = {
            'url': urls[0],
            'state': 'choose_duration'
        }
        
        keyboard = [
            [InlineKeyboardButton("5s", callback_data="dur_5"), InlineKeyboardButton("10s", callback_data="dur_10")],
            [InlineKeyboardButton("20s", callback_data="dur_20"), InlineKeyboardButton("30s", callback_data="dur_30")],
            [InlineKeyboardButton("✏️ Custom", callback_data="dur_custom")]
        ]
        
        msg = await update.message.reply_text(
            "🎬 <b>Video link received!</b>\n\n"
            "⏱️ Choose your clip length:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        
        await asyncio.sleep(2)
        await update.message.delete()
    else:
        msg = await update.message.reply_text(
            "⚠️ <b>Text links or commands only.</b>\n\n"
            "Please send a valid video URL or use /help for instructions.",
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(5)
        try:
            await update.message.delete()
            await msg.delete()
        except:
            pass


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user or not query.data:
        return
    
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "help":
        help_text = (
            "📖 <b>How to Use Clipper Bot</b>\n\n"
            "1️⃣ Send me a video link\n"
            "2️⃣ Choose clip length\n"
            "3️⃣ Select number of clips\n"
            "4️⃣ Get your downloads!\n\n"
            "<b>Custom Format:</b>\n"
            "• <code>00H08M10S:00H09M20S</code>\n"
            "• <code>1:30-2:45</code>\n"
            "• <code>90-150</code>"
        )
        await query.edit_message_text(help_text, parse_mode=ParseMode.HTML)
        
    elif data == "feedback":
        user_states[user_id] = {'state': 'awaiting_feedback'}
        await query.edit_message_text(
            "💬 <b>Send Your Feedback</b>\n\n"
            "Please type your message:",
            parse_mode=ParseMode.HTML
        )
        
    elif data == "donate":
        upi_link = "upi://pay?pn=MD%20SHAHNAWAJ&am=&mode=01&pa=md.3282-40@waaxis"
        keyboard = [[InlineKeyboardButton("☕ Donate via UPI", url=upi_link)]]
        await query.edit_message_text(
            "☕ <b>Support Clipper Bot</b>\n\n"
            "Your donations help keep this bot running!\n"
            "Thank you! 🙏",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
        
    elif data.startswith("dur_"):
        if user_id not in user_states:
            await query.edit_message_text("❌ Session expired. Please send the video link again.")
            return
        
        duration_map = {"dur_5": 5, "dur_10": 10, "dur_20": 20, "dur_30": 30}
        
        if data == "dur_custom":
            user_states[user_id]['state'] = 'awaiting_custom'
            await query.edit_message_text(
                "✏️ <b>Custom Range</b>\n\n"
                "Enter in format:\n"
                "• <code>00H08M10S:00H09M20S</code>\n"
                "• <code>1:30-2:45</code>\n"
                "• <code>90-150</code>",
                parse_mode=ParseMode.HTML
            )
        else:
            duration = duration_map.get(data, 10)
            user_states[user_id]['clip_duration'] = duration
            user_states[user_id]['state'] = 'choose_clips'
            
            max_possible = min(MAX_CLIPS, 5)
            keyboard = [[InlineKeyboardButton(f"{i} Clip{'s' if i > 1 else ''}", callback_data=f"clips_{i}")] for i in range(1, max_possible + 1)]
            
            await query.edit_message_text(
                f"⏱️ Clip length: <b>{duration}s</b>\n\n"
                f"How many clips do you want?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
            
    elif data.startswith("clips_"):
        if user_id not in user_states:
            await query.edit_message_text("❌ Session expired. Please send the video link again.")
            return
        
        num_clips = int(data.split("_")[1])
        user_states[user_id]['num_clips'] = num_clips
        
        await query.edit_message_text(
            f"💥⚡💥 <b>Processing your request...</b>\n\n"
            f"Please wait while I work on your video!",
            parse_mode=ParseMode.HTML
        )
        if query.message:
            processing_messages[user_id] = query.message.message_id
        
        await process_video(query, context, user_id)


async def process_video(query, context, user_id: int):
    try:
        state = user_states.get(user_id)
        if not state:
            return
        
        url = state['url']
        clip_duration = state['clip_duration']
        num_clips = state['num_clips']
        custom_range = state.get('custom_range')
        
        total_duration = clip_duration * num_clips if not custom_range else clip_duration
        
        if total_duration > MAX_CLIP_SECONDS:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text=f"⚙️ <b>This may take a while...</b>\n\n"
                     f"Processing {num_clips} clip{'s' if num_clips > 1 else ''} ({total_duration}s total)\n"
                     f"Please be patient! 💥⚡💥",
                parse_mode=ParseMode.HTML
            )
        
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=processing_messages[user_id],
            text=f"📥 <b>Downloading video...</b> 💥⚡💥",
            parse_mode=ParseMode.HTML
        )
        
        download_result = await download_video(url, user_id)
        if not download_result:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text="❌ <b>Download failed!</b>\n\nPlease check your link and try again.",
                parse_mode=ParseMode.HTML
            )
            return
        
        video_path, video_duration, video_title = download_result
        bot_stats['videos_processed'] += 1
        
        if custom_range:
            start, end = custom_range
            if end > video_duration:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=processing_messages[user_id],
                    text=f"❌ <b>Invalid range!</b>\n\n"
                         f"Video duration is only {format_timestamp(video_duration)}.\n"
                         f"Your requested end time ({format_timestamp(end)}) exceeds the video length.",
                    parse_mode=ParseMode.HTML
                )
                video_path.unlink(missing_ok=True)
                return
        elif clip_duration > video_duration:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text=f"❌ <b>Clip too long!</b>\n\n"
                     f"Video duration is only {format_timestamp(video_duration)}.\n"
                     f"Your requested clip length ({clip_duration}s) is longer than the video.",
                parse_mode=ParseMode.HTML
            )
            video_path.unlink(missing_ok=True)
            return
        
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=processing_messages[user_id],
            text=f"✂️ <b>Creating clips...</b> 💥⚡💥\n\n"
                 f"Video: {video_title[:40]}...",
            parse_mode=ParseMode.HTML
        )
        
        clips_created = []
        
        if custom_range:
            start, end = custom_range
            start = max(0, min(start, video_duration - 1))
            end = max(start + 1, min(end, video_duration))
            output_file = CLIPS_DIR / f"{user_id}_clip_1.mp4"
            success = await create_clip(video_path, start, end, output_file)
            if success:
                clips_created.append(output_file)
                bot_stats['clips_created'] += 1
        else:
            interval = max(1, int((video_duration - clip_duration) / max(num_clips - 1, 1)))
            
            for i in range(num_clips):
                start = max(0, min(i * interval, video_duration - clip_duration))
                end = min(start + clip_duration, video_duration)
                
                output_file = CLIPS_DIR / f"{user_id}_clip_{i+1}.mp4"
                success = await create_clip(video_path, start, end, output_file)
                if success:
                    clips_created.append(output_file)
                    bot_stats['clips_created'] += 1
        
        if not clips_created:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text="❌ <b>Clip creation failed!</b>\n\nPlease try again later.",
                parse_mode=ParseMode.HTML
            )
            video_path.unlink(missing_ok=True)
            return
        
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=processing_messages[user_id],
            text=f"☁️ <b>Uploading to GoFile...</b> 💥⚡💥",
            parse_mode=ParseMode.HTML
        )
        
        upload_links = []
        for clip_file in clips_created:
            link = await upload_to_gofile(clip_file)
            if link:
                upload_links.append(link)
            clip_file.unlink(missing_ok=True)
        
        video_path.unlink(missing_ok=True)
        
        if upload_links:
            result_text = f"✅ <b>All Done!</b>\n\n"
            result_text += f"🎬 Created {len(upload_links)} clip{'s' if len(upload_links) > 1 else ''}:\n\n"
            
            for idx, link in enumerate(upload_links, 1):
                result_text += f"{idx}. <a href='{link}'>Download Clip {idx}</a>\n"
            
            keyboard = [[InlineKeyboardButton("🔄 Create Another", callback_data="help")]]
            
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text=result_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        else:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages[user_id],
                text="❌ <b>Upload failed!</b>\n\nClips were created but upload failed. Please try again.",
                parse_mode=ParseMode.HTML
            )
        
        if user_id in user_states:
            del user_states[user_id]
        if user_id in processing_messages:
            del processing_messages[user_id]
            
    except Exception as e:
        logger.error(f"Processing error for user {user_id}: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=processing_messages.get(user_id),
                text=f"❌ <b>An error occurred!</b>\n\n{str(e)[:100]}",
                parse_mode=ParseMode.HTML
            )
        except:
            pass


def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        return
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("donate", donate_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🚀 Clipper Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
