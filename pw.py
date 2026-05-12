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
from datetime import datetime, timedelta, timezone

# Configuration 
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PING_URL = os.getenv("PING_URL", "")
BASE_DIR = "PW_DOWNLOADS"
MAX_PAIRS = 5 # Telegram message limit length limit supports upto 5 links 
OWNER_ID = int(os.getenv("OWNER_ID"))
KOYEB_API_TOKEN = os.getenv("KOYEB_API_TOKEN").strip()
KOYEB_SERVICE_ID = os.getenv("KOYEB_SERVICE_ID").strip()
SHOULD_EARNXP = os.getenv("SHOULD_EARNXP", "true").strip().lower() == "true"

# Setup logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Telethon client
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
        "--thread-count", "8",        
        "--append-url-params",
        "--auto-select",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "-H", "Referer: https://www.pw.live/",
        "--save-dir", BASE_DIR,
        "--tmp-dir", BASE_DIR,       
        "--save-name", save_name,
        "--del-after-done",           
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
        await event.delete()
        logger.info("Deleted a system service message.")
    except Exception:
        pass

# --- Download Command Handler ---
@client.on(events.NewMessage(pattern=r'(?i)^/pw(?:@[a-zA-Z0-9_]+)?\s+(.+)', incoming=True))
async def handle_pw_command(event):
        
    if event.sender_id != OWNER_ID: 
        return
    
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
            
        clear_base_dir()

    set_processing_status(False)

# --- XP Command Handler ---
@client.on(events.NewMessage(pattern=r'(?i)^/earnxp(?:@[a-zA-Z0-9_]+)?(?:\s+(.+))?$', incoming=True))
async def handle_earnxp_command(event):
        
    if event.sender_id != OWNER_ID: 
        return
    
    topic_id = event.reply_to_msg_id if event.is_reply else None
    value_str = event.pattern_match.group(1)
    
    if not value_str:
        await client.send_message(event.chat_id, "Error: Please provide an XP value. Usage: `/earnxp <value>`", reply_to=topic_id)
        return

    try:
        required_xp = int(value_str.strip())
    except ValueError:
        await client.send_message(event.chat_id, "Error: XP value must be a positive integer.", reply_to=topic_id)
        return

    if required_xp <= 0:
        await client.send_message(event.chat_id, "Error: XP value must be greater than 0.", reply_to=topic_id)
        return

    if required_xp % 100 != 0:
        await client.send_message(event.chat_id, "Error: XP value must be a multiple of 100.", reply_to=topic_id)
        return

    global is_processing
    if is_processing:
        await client.send_message(event.chat_id, "Another task is already running. Please wait.", reply_to=topic_id)
        return

    set_processing_status(True)
    progress_message = await client.send_message(event.chat_id, f"Starting script to earn {required_xp} XP...", reply_to=topic_id)

    try:
        # STDOUT merges stderr so we capture everything live
        process = await asyncio.create_subprocess_exec(
            "python3", "-u", "pwxphack.py", str(required_xp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        
        output_lines = []
        
        while True:
            line = await process.stdout.readline()
            if not line:
                break
        
            decoded_line = line.decode().strip()
        
            print(f"{decoded_line}", flush=True)
        
            output_lines.append(decoded_line)
            
        await process.wait()
            
        await progress_message.edit(f"**XP Command Finished**\n{required_xp}XP is credited to your account ✅")
        
    except Exception as e:
        logger.error(f"Failed to execute pwxphack.py: {e}")
        await progress_message.edit(f"An error occurred while running the script: {str(e)}")
    finally:
        set_processing_status(False)


# We will need to update BEARER_TOKEN once the old one expires so we implement a command handler for ease
@client.on(events.NewMessage(pattern=r'(?i)^/bearer(?:@[a-zA-Z0-9_]+)?(?:\s+(.+))?$', incoming=True))
async def handle_bearer_update(event):
    
    if event.sender_id != OWNER_ID: 
        return
    
    topic_id = event.reply_to_msg_id if event.is_reply else None
    new_token = event.pattern_match.group(1)

    if not new_token or not new_token.strip():
        await client.send_message(
            event.chat_id,
            "Error: Please provide a bearer token.\nUsage: `/bearer <token>`",
            reply_to=topic_id
        )
        return
    
    new_token = new_token.strip()

    if not KOYEB_API_TOKEN or not KOYEB_SERVICE_ID:
        await client.send_message(
            event.chat_id,
            "**Configuration Error:** `KOYEB_API_TOKEN` and `KOYEB_SERVICE_ID` environment variables are not set.",
            reply_to=topic_id
        )
        return

    progress_message = await client.send_message(
        event.chat_id,
        "Please wait...",
        reply_to=topic_id
    )

    loop = asyncio.get_running_loop()

    def blocking_task():
        headers = {
            "Authorization": f"Bearer {KOYEB_API_TOKEN}",
            "Content-Type": "application/json"
        }

        url = f"https://app.koyeb.com/v1/services/{KOYEB_SERVICE_ID}"

        # Fetch service
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            return False, f"Failed to fetch service: {resp.text}"

        data = resp.json()
        definition = data.get("service", {}).get("definition")

        if not definition:
            return False, "Invalid service definition received from Koyeb."

        env_vars = definition.get("env", [])

        token_updated = False
        for env in env_vars:
            if env.get("key") == "BEARER_TOKEN":
                env["value"] = new_token
                token_updated = True
                break

        if not token_updated:
            env_vars.append({"key": "BEARER_TOKEN", "value": new_token})

        definition["env"] = env_vars  
        patch_payload = {
            "definition": definition,
            "skip_build": True
        }

        patch_resp = requests.patch(url, headers=headers, json=patch_payload)

        if patch_resp.status_code == 200:
            return True, "**Successfully updated `BEARER_TOKEN`.**\nA new deployment without build has been triggered on Koyeb."
        else:
            return False, f"**Failed to update service:**\n`{patch_resp.text}`"

    success, result_msg = await loop.run_in_executor(None, blocking_task)
    await progress_message.edit(result_msg)

@client.on(events.NewMessage(pattern=r'(?i)^/ping(?:@[a-zA-Z0-9_]+)?$', incoming=True))
async def ping(event):
    await event.reply("PW Downloader is alive and ready!")

# --- Scheduled Background Task ---
IST = timezone(timedelta(hours=5, minutes=30))

async def daily_xp_scheduler():
    """Runs pwxphack.py automatically at 12:01 AM IST"""
    while True:
        now = datetime.now(IST)
        target = now.replace(hour=0, minute=1, second=0, microsecond=0)

        if now >= target:
            target += timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        logger.info(f"Scheduler: Sleeping for {sleep_seconds} seconds until next 12:01 AM IST.")

        await asyncio.sleep(sleep_seconds)

        logger.info("Scheduler: Running daily automated XP hack...")

        try:
            # send start message
            msg = await client.send_message(
                7803597625,
                "Auto XP scheduler started.\nCrediting 2000 XP..."
            )

            process = await asyncio.create_subprocess_exec(
                "python3", "-u", "pwxphack.py", "2000",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            logs = []

            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                decoded_line = line.decode().rstrip()
                print(decoded_line, flush=True)
                logs.append(decoded_line)

            await process.wait()

            # edit message instead of sending new one
            await msg.edit(
                "Auto XP completed successfully.\n2000 XP credited."
            )

            logger.info("Scheduler: Daily XP hack completed successfully.")

        except Exception as e:
            logger.error(f"Scheduler: Failed to run automated script: {e}")

            try:
                await msg.edit(f"Auto XP scheduler failed:\n{e}")
            except:
                await client.send_message(
                    7803597625,
                    f"Auto XP scheduler failed:\n{e}"
                )
            
# --- FastAPI & Runners ---
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
        
        # Start the background task 
        if SHOULD_EARNXP:
            asyncio.create_task(daily_xp_scheduler())
            logger.info("Daily XP scheduler started.")
        else:
            logger.info("Daily XP scheduler is disabled.")
        
        await client.run_until_disconnected()
    asyncio.run(runner())

threading.Thread(target=start_telethon, daemon=True).start()
threading.Thread(target=ping_self, daemon=True).start()
