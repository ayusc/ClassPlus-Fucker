import os
import re
import shutil
import logging
import subprocess
import requests
import asyncio
import ffmpeg
from telethon.tl.types import DocumentAttributeVideo

from urllib.parse import urlparse
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from fastapi import FastAPI
import uvicorn
import threading
import time
import aiohttp
from aiohttp import ClientSession
#from FastTelethonhelper import fast_upload
from FastTelethon import upload_file
from telethon import events, utils
from telethon.tl import types

# Configuration
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BASE_DIR = "CLASSPLUS"
MAX_LINKS = 4
MAX_PARTS = 10000
START_PART = 0
STOP_AFTER_MISSES = 3

# Setup logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize Telethon client
if SESSION_STRING:
    logger.info("Using provided session string to login.")
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    logger.error("SESSION_STRING is missing. Please set it in environment variables.")
    raise ValueError("SESSION_STRING environment variable not found!")

# Ensure BASE_DIR exists
os.makedirs(BASE_DIR, exist_ok=True)

# To track if a task is in progress
is_processing = False

def set_processing_status(status: bool):
    global is_processing
    is_processing = status

def clear_base_dir():
    if os.path.exists(BASE_DIR):
        logger.info(f"Clearing contents of {BASE_DIR}...")
        shutil.rmtree(BASE_DIR)
    os.makedirs(BASE_DIR, exist_ok=True)
    logger.info(f"Recreated {BASE_DIR} directory after clearing.")

def extract_details(ts_url):
    parsed_url = urlparse(ts_url)
    path_parts = parsed_url.path.rsplit('/', 1)
    filename = path_parts[-1]

    match1 = re.match(r"(720p)_\d+\.ts", filename)
    match2 = re.match(r"(data)\d+\.ts", filename)

    if match1:
        prefix = match1.group(1) + "_"
        base_path = path_parts[0]
        return prefix, base_path, parsed_url
    elif match2:
        prefix = match2.group(1)
        base_path = path_parts[0]
        return prefix, base_path, parsed_url
    else:
        return None, None, None

def get_video_metadata(video_path, thumb_path=None):
    probe = ffmpeg.probe(video_path)
    video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
    if video_stream is None:
        raise Exception('No video stream found')

    width = int(video_stream['width'])
    height = int(video_stream['height'])
    duration = float(video_stream['duration'])

    if thumb_path:
        create_thumbnail(video_path, thumb_path)

    return width, height, duration

def create_thumbnail(video_path, thumb_path):
    try:
        (
            ffmpeg
            .input(video_path, ss=1)
            .filter('scale', 320, -1)
            .output(thumb_path, vframes=1)
            .overwrite_output()
            .run(quiet=True)
        )
    except Exception as e:
        logger.error(f"Failed to create thumbnail: {e}")

async def fetch_part(session, url, local_path, part_name, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as res:
                if res.status == 200:
                    with open(local_path, 'wb') as f:
                        async for chunk in res.content.iter_chunked(1024):
                            f.write(chunk)
                    logger.info(f"✅ Downloaded part: {part_name}")
                    return True
                else:
                    logger.warning(f"Part not found: {part_name} (HTTP {res.status})")
        except Exception as e:
            logger.error(f"Error downloading {part_name}, attempt {attempt+1}: {e}")
        await asyncio.sleep(1)
    return False

async def download_video(link, folder_index, video_index, event, topic_id):
    logger.info(f"Downloading video {video_index} from link: {link}")
    prefix, base_path, parsed_url = extract_details(link)
    if prefix is None:
        await client.send_message(event.chat_id, f"Invalid URL format: {link}", reply_to=topic_id)
        logger.error(f"Invalid URL format: {link}")
        return None

    output_dir = os.path.join(BASE_DIR, str(folder_index))
    os.makedirs(output_dir, exist_ok=True)
    query = parsed_url.query

    downloaded_files = []
    progress_message = await client.send_message(event.chat_id, f"Lecture {video_index}\nDownloading...", reply_to=topic_id)

    misses = 0
    part_index = START_PART

    async with aiohttp.ClientSession() as session:
        while part_index < MAX_PARTS and misses < STOP_AFTER_MISSES:
            batch_tasks = []
            batch_parts = []

            for i in range(part_index, part_index + 10):
                part_name = f"{prefix}{i:03d}.ts"
                full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}/{part_name}"
                if query:
                    full_url += f"?{query}"
                local_path = os.path.join(output_dir, part_name)

                if os.path.exists(local_path):
                    continue

                task = fetch_part(session, full_url, local_path, part_name)
                batch_tasks.append(task)
                batch_parts.append(part_name)

            if not batch_tasks:
                break  # nothing to do

            results = await asyncio.gather(*batch_tasks)
            for success, part_name in zip(results, batch_parts):
                if success:
                    downloaded_files.append(part_name)
                    misses = 0  # reset on success
                else:
                    misses += 1
                    if misses >= STOP_AFTER_MISSES:
                        break

            part_index += 10

    if not downloaded_files:
        await progress_message.edit(f"Lecture {video_index}\nNo parts downloaded ❌")
        logger.warning(f"No parts downloaded for Lecture {video_index}")
        return None

    await progress_message.edit(f"Lecture {video_index}\nDownloaded successfully ✅")
    await asyncio.sleep(1)
    await progress_message.delete()

    return output_dir

async def merge_video(output_dir, video_index, event, topic_id):
    logger.info(f"Merging video {video_index} in folder {output_dir}")

    list_path = os.path.join(output_dir, "file_list.txt")
    downloaded_files = sorted([f for f in os.listdir(output_dir) if f.endswith(".ts")])

    with open(list_path, 'w') as f:
        for name in downloaded_files:
            f.write(f"file '{name}'\n")

    output_video = os.path.join(output_dir, f"Lecture{video_index}.mp4")

    try:
        subprocess.run([
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", "file_list.txt", "-c", "copy", f"Lecture{video_index}.mp4"
        ], cwd=output_dir, check=True)

        logger.info(f"Merging completed: {output_video}")
        return output_video

    except subprocess.CalledProcessError:
        await client.send_message(event.chat_id, f"Lecture {video_index}\nMerging failed ❌", reply_to=topic_id)
        logger.error(f"Merging failed for Lecture {video_index}")
        return None

async def upload_video(output_video, video_index, event, topic_id):
    logger.info(f"Uploading video {video_index}: {output_video}")

    progress_message = await client.send_message(event.chat_id, f"Lecture {video_index}\nUploading... 0%", reply_to=topic_id)

    file_size = os.path.getsize(output_video)
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

    thumbnail_path = os.path.join(os.path.dirname(output_video), f"thumb_{video_index}.jpg")
    width, height, duration = get_video_metadata(output_video, thumb_path=thumbnail_path)

    with open(output_video, "rb") as out:
         res = await upload_file(client, out, progress_callback=progress_callback)
         thumbnail = await client.upload_file(thumbnail_path)
         mime_type = utils.get_attributes(output_video)
         media = types.InputMediaUploadedDocument(
                file=res,
                mime_type=mime_type,
                attributes=[DocumentAttributeVideo(duration=int(duration), w=width, h=height, supports_streaming=True)],              
                force_file=False,
                thumb=thumbnail)
        
         #media = (await client._file_to_media(res, thumb=thumbnail_path, supports_streaming=True))[1]
        
         print(media)
        
         await client.send_file(
         event.chat_id,
         media,
         reply_to=topic_id,
         caption=f"Lecture {video_index}")

    if os.path.exists(thumbnail_path):
        os.remove(thumbnail_path)

    await progress_message.delete()

    logger.info(f"Lecture {video_index} uploaded successfully ✅")

@client.on(events.NewMessage(pattern=r'^\.iit\s+(.+)', outgoing=True))
async def handle_iit_command(event):
    topic_id = None
    if getattr(event.reply_to, 'forum_topic', None):
        topic_id = top if (top := event.reply_to.reply_to_top_id) else event.reply_to_msg_id

    global is_processing
    if is_processing:
        await client.send_message(event.chat_id, "❌ Another task is already running. Please wait.", reply_to=topic_id)
        return

    set_processing_status(True)
    await event.delete()

    logger.info("Received .iit command.")

    user_input = event.pattern_match.group(1)
    parts = user_input.split()

    if len(parts) < 2:
        await client.send_message(event.chat_id, "❌ Usage: `.iit <start_no> <link1> <link2> ...`", reply_to=topic_id)
        set_processing_status(False)
        return

    try:
        start_index = int(parts[0])
    except ValueError:
        await client.send_message(event.chat_id, "❌ Start number must be an integer.", reply_to=topic_id)
        set_processing_status(False)
        return

    links = parts[1:]
    if len(links) > MAX_LINKS:
        await client.send_message(event.chat_id, f"❌ You can provide up to {MAX_LINKS} links only.", reply_to=topic_id)
        set_processing_status(False)
        return

    clear_base_dir()

    downloads_info = []

    for idx, link in enumerate(links):
        video_index = start_index + idx
        output_dir = await download_video(link, idx + 1, video_index, event, topic_id)
        if output_dir:
            downloads_info.append((output_dir, video_index))
        else:
            logger.warning(f"Skipping merge and upload for Lecture {video_index} due to download failure.")

    merged_videos = []
    for output_dir, video_index in downloads_info:
        output_video = await merge_video(output_dir, video_index, event, topic_id)
        if output_video:
            merged_videos.append((output_video, video_index))
        else:
            logger.warning(f"Skipping upload for Lecture {video_index} due to merge failure.")

    for output_video, video_index in merged_videos:
        await upload_video(output_video, video_index, event, topic_id)

    set_processing_status(False)
    logger.info("All tasks completed ✅")

@client.on(events.NewMessage(pattern=r'^\.ping$', outgoing=True))
async def ping(event):
    logger.info("Received .ping command.")
    await event.reply("✅ Userbot is alive and running!")

# ---- ADDED THIS SECTION ----

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Running ✅"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

# 🔁 Function to periodically ping the app's health endpoint
def ping_self():
    while True:
        try:
            res = requests.get("https://amazing-margit-ayuschatterjee-94e3bcaf.koyeb.app/health")
            if res.status_code == 200:
                print("✅ Self-ping succeeded")
            else:
                print(f"⚠️ Self-ping failed with status code {res.status_code}")
        except Exception as e:
            print(f"❌ Error in self-ping: {e}")
        time.sleep(60)  # Sleep for 5 minutes (300 seconds)

# 🚀 Start Telethon in a background thread
def start_telethon():
    async def main():
        logger.info("🚀 Starting Telethon client")
        await client.start()
        logger.info("✅ Telethon client started")
        await client.run_until_disconnected()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())

# ✅ Start both background threads after handlers are defined
threading.Thread(target=start_telethon, daemon=True).start()
threading.Thread(target=ping_self, daemon=True).start()  
