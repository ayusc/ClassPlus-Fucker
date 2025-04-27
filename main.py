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

# Configuration
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BASE_DIR = "CLASSPLUS"
MAX_LINKS = 5
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

def get_video_metadata(video_path):
    probe = ffmpeg.probe(video_path)
    video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
    if video_stream is None:
        raise Exception('No video stream found')
    width = int(video_stream['width'])
    height = int(video_stream['height'])
    duration = float(video_stream['duration'])
    return width, height, duration

import os
import subprocess
import requests
import asyncio

async def download_and_merge(link, folder_index, video_index, event):
    logger.info(f"Processing video {video_index} for link: {link}")

    prefix, base_path, parsed_url = extract_details(link)
    if prefix is None:
        await event.reply(f"Invalid URL format: {link}")
        logger.error(f"Invalid URL format: {link}")
        return

    output_dir = os.path.join(BASE_DIR, str(folder_index))
    os.makedirs(output_dir, exist_ok=True)
    query = parsed_url.query

    downloaded_files = []
    progress_message = await event.reply(f"Lecture {video_index}\nDownloading...")

    misses = 0
    for i in range(START_PART, MAX_PARTS):
        part_name = f"{prefix}{i:03d}.ts"
        full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}/{part_name}"
        if query:
            full_url += f"?{query}"

        local_path = os.path.join(output_dir, part_name)

        if os.path.exists(local_path):
            logger.debug(f"Skipping existing part: {part_name}")
            continue

        part_failures = 0
        while part_failures < 3:  # Retry up to 3 times for the same part
            try:
                res = requests.get(full_url, stream=True, timeout=10)
                if res.status_code == 200:
                    with open(local_path, 'wb') as f:
                        for chunk in res.iter_content(chunk_size=1024):
                            f.write(chunk)
                    downloaded_files.append(part_name)
                    logger.info(f"Downloaded part: {part_name}")
                    break  # Exit the retry loop on successful download
                else:
                    logger.warning(f"Part not found: {part_name} (HTTP {res.status_code})")
            except Exception as e:
                logger.error(f"Error downloading {part_name}: {e}")

            part_failures += 1
            if part_failures < 3:
                #await asyncio.sleep(1)  # Wait before retrying

        if part_failures == 3:
            misses += 1
            logger.warning(f"Part {part_name} failed 3 times. Total misses: {misses}")

        if misses >= STOP_AFTER_MISSES:
            logger.warning(f"Stopping download after {STOP_AFTER_MISSES} consecutive misses.")
            break

    if not downloaded_files:
        await progress_message.edit(f"Lecture {video_index}\nNo parts downloaded!")
        logger.warning(f"No parts downloaded for Lecture {video_index}.")
        return

    list_path = os.path.join(output_dir, "file_list.txt")
    with open(list_path, 'w') as f:
        for name in downloaded_files:
            f.write(f"file '{name}'\n")

    output_video = os.path.join(output_dir, f"Lecture{video_index}.mp4")

    try:
        await progress_message.edit(f"Lecture {video_index}\nMerging video...")
        logger.info(f"Started merging parts into {output_video}...")

        subprocess.run([
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", "file_list.txt", "-c", "copy", f"Lecture{video_index}.mp4"
        ], cwd=output_dir, check=True)

        logger.info(f"Merging completed for {output_video}.")

        await progress_message.edit(f"Lecture {video_index}\nUploading... 0%")
        logger.info(f"Started uploading {output_video} to Telegram...")

        file_size = os.path.getsize(output_video)
        last_progress = -5

        async def progress_callback(current, total):
            nonlocal last_progress
            percent = int(current / total * 100)
            if percent >= last_progress + 5:
                last_progress = percent
                logger.info(f"Uploading {output_video}: {percent}% done.")
                try:
                    await progress_message.edit(f"Lecture {video_index}\nUploading... {percent}%")
                except:
                    pass  # Ignore FloodWait

        width, height, duration = get_video_metadata(output_video)

        await client.send_file(
            event.chat_id,
            output_video,
            caption=f"Lecture {video_index}",
            progress_callback=progress_callback,
            attributes=[
                DocumentAttributeVideo(
                    duration=int(duration),
                    w=width,
                    h=height,
                    supports_streaming=True  # Important for videos
                )
            ]
        )
        
        await progress_message.delete()
        logger.info(f"Lecture {video_index} uploaded successfully.")

        # Clean up
        os.remove(list_path)
        for file in downloaded_files:
            try:
                os.remove(os.path.join(output_dir, file))
                logger.debug(f"Deleted part: {file}")
            except Exception as e:
                logger.warning(f"Error deleting {file}: {e}")
        os.remove(output_video)
        logger.info(f"Cleaned up files for Lecture {video_index}.")

    except subprocess.CalledProcessError:
        await progress_message.edit(f"Lecture {video_index}\nMerging failed ❌")
        logger.error(f"FFmpeg merging failed for Lecture {video_index}")

@client.on(events.NewMessage(pattern=r'^\.iit\s+(.+)', outgoing=True))
async def handle_iit_command(event):
    await event.delete()
    logger.info("Received .iit command.")
    user_input = event.pattern_match.group(1)
    parts = user_input.split()

    if len(parts) < 2:
        await event.reply("❌ Usage: `.iit <start_no> <link1> <link2> ...`\n(up to 5 links allowed)")
        return

    try:
        start_index = int(parts[0])
    except ValueError:
        await event.reply("❌ Start number must be an integer.\nUsage: `.iit <start_no> <link1> <link2> ...`")
        return

    links = parts[1:]
    if len(links) > MAX_LINKS:
        await event.reply(f"❌ You can provide up to {MAX_LINKS} links only.")
        return

    valid_links = []
    for link in links:
        prefix, base_path, parsed_url = extract_details(link)
        if prefix is None:
            await event.reply(f"❌ Invalid URL format: {link}")
            return
        valid_links.append(link)

    clear_base_dir()
    #await event.reply(f"Processing {len(valid_links)} links...")

    for idx, link in enumerate(valid_links):
        video_index = start_index + idx
        await download_and_merge(link, idx + 1, video_index, event)

@client.on(events.NewMessage(pattern=r'^\.ping$', outgoing=True))
async def ping(event):
    logger.info("Received .ping command.")
    await event.reply("✅ Userbot is alive and running!")

def main():
    client.start()
    logger.info("Userbot started successfully.")
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
