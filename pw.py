import os
import re
import json
import shutil
import logging
import asyncio
from urllib.parse import urlparse
from telethon import TelegramClient, events, utils
from telethon.tl.types import DocumentAttributeVideo
from fastapi import FastAPI
import threading
import time
import requests
from FastTelethon import upload_file

# Configuration 
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PING_URL = os.getenv("PING_URL", "")
BASE_DIR = "PW_DOWNLOADS"
MAX_PAIRS = 5 # Telegram message limit length limit supports upto 5 links 

# Setup logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Telethon client (using a local sqlite session file)
if BOT_TOKEN:
    logger.info("Initializing bot with provided BOT_TOKEN.")
    client = TelegramClient("pw_bot_session", API_ID, API_HASH)
else:
    raise ValueError("BOT_TOKEN environment variable not found!")

os.makedirs(BASE_DIR, exist_ok=True)
is_processing = False

def set_processing_status(status: bool):
    global is_processing
    is_processing = status

def clear_base_dir():
    if os.path.exists(BASE_DIR):
        logger.info(f"Clearing contents of {BASE_DIR}...")
        shutil.rmtree(BASE_DIR)
    os.makedirs(BASE_DIR, exist_ok=True)

async def get_video_metadata(video_path):
    """Fully async metadata extraction using ffprobe to prevent bot freezing"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-of", "json", video_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    
    try:
        metadata = json.loads(stdout)
        stream = metadata['streams'][0]
        width = int(stream.get('width', 1280))
        height = int(stream.get('height', 720))
        duration = float(stream.get('duration', 0.0))
        return width, height, duration
    except Exception as e:
        logger.error(f"Metadata extraction failed: {e}")
        return 1280, 720, 0

async def create_thumbnail(video_path, thumb_path):
    """Fully async thumbnail generation at the 5-second mark"""
    cmd = [
        "ffmpeg", "-y", "-i", video_path, 
        "-ss", "00:00:05", 
        "-vframes", "1", 
        "-vf", "scale=320:-1", 
        thumb_path
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()

async def download_pw_video(link, key, video_index, event, topic_id):
    logger.info(f"Downloading Lecture {video_index}")
    progress_message = await client.send_message(event.chat_id, f"Lecture {video_index}\nDownloading & Decrypting...", reply_to=topic_id)

    save_name = f"Lecture_{video_index}"
    output_video = os.path.join(BASE_DIR, f"{save_name}.mp4")

    cmd = [
        "N_m3u8DL-RE",
        link,
        "--key", key,
        "--thread-count", "8",        # Optimizes CPU usage for free tier
        "--append-url-params",
        "--auto-select",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "-H", "Referer: https://www.pw.live/",
        "--save-dir", BASE_DIR,
        "--tmp-dir", BASE_DIR,       
        "--save-name", save_name,
        "--del-after-done",           # Instantly cleans up fragments to save disk space
        "-M", "format=mp4"
    ]

    def run_downloader():
        import subprocess
        try:
            subprocess.run(cmd, check=True, stdout=None, stderr=None)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Downloader failed with exit code: {e.returncode}")
            return False

    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(None, run_downloader)

    if success and os.path.exists(output_video):
        await progress_message.edit(f"Lecture {video_index}\nDownloaded successfully! Preparing upload...")
        await asyncio.sleep(1)
        await progress_message.delete()
        return output_video
    else:
        await progress_message.edit(f"Lecture {video_index}\nDownload Failed! Check terminal logs.")
        for f in os.listdir(BASE_DIR):
            if f.startswith(save_name) and not f.endswith('.mp4'):
                os.remove(os.path.join(BASE_DIR, f))
        return None

async def upload_video(output_video, video_index, event, topic_id):
    logger.info(f"Uploading video {video_index}: {output_video}")
    progress_message = await client.send_message(event.chat_id, f"Lecture {video_index}\nUploading... 0%", reply_to=topic_id)

    last_progress = -5
    async def progress_callback(current, total):
        nonlocal last_progress
        percent = int(current / total * 100)
        if percent >= last_progress + 5:
            last_progress = percent
            try:
                await progress_message.edit(f"Lecture {video_index}\nUploading... {percent}%")
            except:
                pass

    thumbnail_path = os.path.join(BASE_DIR, f"thumb_{video_index}.jpg")
    width, height, duration = await get_video_metadata(output_video)
    await create_thumbnail(output_video, thumbnail_path)

    with open(output_video, "rb") as out:
        res = await upload_file(client, out, progress_callback=progress_callback)
        res.name = f'Lecture_{video_index}.mp4'
        
        await client.send_file(
            event.chat_id,
            res,
            reply_to=topic_id,
            attributes=[DocumentAttributeVideo(duration=int(duration), w=width, h=height, supports_streaming=True)],
            caption=f"Lecture {video_index}",
            thumb=thumbnail_path if os.path.exists(thumbnail_path) else None,
            supports_streaming=True
        )

    if os.path.exists(thumbnail_path):
        os.remove(thumbnail_path)
    os.remove(output_video)

    await progress_message.delete()
    logger.info(f"Lecture {video_index} uploaded successfully")

@client.on(events.ChatAction)
async def delete_service_messages(event):
    try:
        # ChatAction covers pins, user joins, name changes, etc.
        await event.delete()
        logger.info("Deleted a system service message.")
    except Exception:
        # Fails silently if the bot lacks admin rights
        pass

# Main command handler
@client.on(events.NewMessage(pattern=r'(?i)^/pw(?:@[a-zA-Z0-9_]+)?\s+(.+)', incoming=True))
async def handle_pw_command(event):
    topic_id = event.reply_to_msg_id if event.is_reply else None

    global is_processing
    if is_processing:
        await client.send_message(event.chat_id, "Another task is already running. Please wait.", reply_to=topic_id)
        return

    set_processing_status(True)
    
    try:
        await event.delete()
    except Exception:
        pass

    user_input = event.pattern_match.group(1).strip()
    parts = user_input.split()

    if len(parts) < 3 or (len(parts) - 1) % 2 != 0:
        await client.send_message(event.chat_id, "Usage: `/pw <start_no> <link1> <key1> <link2> <key2> ...`", reply_to=topic_id)
        set_processing_status(False)
        return

    try:
        start_index = int(parts[0])
    except ValueError:
        await client.send_message(event.chat_id, "Start number must be an integer.", reply_to=topic_id)
        set_processing_status(False)
        return

    items = parts[1:]
    link_key_pairs = [(items[i], items[i+1]) for i in range(0, len(items), 2)]

    if len(link_key_pairs) > MAX_PAIRS:
        await client.send_message(event.chat_id, f"You can only provide up to {MAX_PAIRS} videos at once due to Telegram message limits.", reply_to=topic_id)
        set_processing_status(False)
        return

    clear_base_dir()

    for idx, (link, key) in enumerate(link_key_pairs):
        video_index = start_index + idx
        output_video = await download_pw_video(link, key, video_index, event, topic_id)
        
        if output_video:
            await upload_video(output_video, video_index, event, topic_id)
        else:
            logger.warning(f"Skipping upload for Lecture {video_index} due to download failure.")
            
        # Aggressive cleanup after EVERY lecture to prevent Koyeb disk throttling
        clear_base_dir()

    set_processing_status(False)

@client.on(events.NewMessage(pattern=r'(?i)^/ping(?:@[a-zA-Z0-9_]+)?$', incoming=True))
async def ping(event):
    await event.reply("PW Downloader Bot is alive and ready!")

# --- FastAPI & Background Tasks ---
app = FastAPI()

@app.get("/")
async def root(): return {"status": "Running"}

@app.get("/health")
async def health(): return {"status": "Healthy"}

def ping_self():
    if not PING_URL: return
    while True:
        try:
            requests.get(PING_URL)
        except Exception as e:
            pass
        time.sleep(60)
        
def start_telethon():
    async def runner():
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Telethon Bot client started successfully!")
        await client.run_until_disconnected()
    asyncio.run(runner())

threading.Thread(target=start_telethon, daemon=True).start()
threading.Thread(target=ping_self, daemon=True).start()
