import os
import re
import shutil
import logging
import subprocess
import requests
from urllib.parse import urlparse
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Configuration
API_ID = int(os.getenv("API_ID"))         # your Telegram API ID
API_HASH = os.getenv("API_HASH")           # your Telegram API Hash
SESSION_STRING = os.getenv("SESSION_STRING")  # Session string stored in env
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

# Initialize Telethon client using StringSession
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

    match1 = re.match(r"(720p)_(\d{3})\.ts", filename)
    match2 = re.match(r"(data)(\d+)\.ts", filename)

    if match1:
        prefix = match1.group(1)
        base_path = path_parts[0]
        return prefix, base_path, parsed_url
    elif match2:
        prefix = match2.group(1)
        base_path = path_parts[0]
        return prefix, base_path, parsed_url
    else:
        return None, None, None

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

    progress_message = await event.reply(f"Lecture {video_index}\nDownloading parts...")
    logger.info(f"Started downloading parts for video {video_index}...")

    misses = 0
    for i in range(START_PART, MAX_PARTS):
        part_name = f"{prefix}{i:03d}.ts"
        full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}/{part_name}"
        if query:
            full_url += f"?{query}"
        local_path = os.path.join(output_dir, part_name)

        try:
            res = requests.get(full_url, stream=True, timeout=10)
            if res.status_code == 200:
                with open(local_path, 'wb') as f:
                    for chunk in res.iter_content(chunk_size=1024):
                        f.write(chunk)
                downloaded_files.append(part_name)
                logger.info(f"Downloaded part: {part_name}")
                misses = 0
            else:
                misses += 1
                logger.warning(f"Part {part_name} not found (miss #{misses})")
                if misses >= STOP_AFTER_MISSES:
                    logger.warning(f"Stopping download after {STOP_AFTER_MISSES} consecutive misses.")
                    break
        except Exception as e:
            misses += 1
            logger.error(f"Error downloading {part_name}: {e}")
            if misses >= STOP_AFTER_MISSES:
                logger.error(f"Stopping download after {STOP_AFTER_MISSES} consecutive errors.")
                break

    if downloaded_files:
        list_path = os.path.join(output_dir, "file_list.txt")
        with open(list_path, 'w') as f:
            for name in downloaded_files:
                f.write(f"file '{name}'\n")

        output_video = os.path.join(output_dir, f"Lecture{video_index}.mp4")
        try:
            await progress_message.edit(f"Lecture {video_index}\nMerging parts...")
            logger.info(f"Merging parts for video {video_index}...")

            subprocess.run([
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", "file_list.txt", "-c", "copy", f"Lecture{video_index}.mp4"
            ], cwd=output_dir, check=True)

            await progress_message.edit(f"Lecture {video_index}\nUploading to Telegram...")
            logger.info(f"Uploading merged video {output_video}...")

            await client.send_file(event.chat_id, output_video, caption=f"Lecture {video_index}")

            # Cleanup
            os.remove(list_path)
            for file in downloaded_files:
                os.remove(os.path.join(output_dir, file))
            os.remove(output_video)

            await progress_message.edit(f"Lecture {video_index} processing completed successfully.")
            logger.info(f"Lecture {video_index} processing completed successfully.")

        except subprocess.CalledProcessError as e:
            await progress_message.edit(f"Lecture {video_index} merging failed using ffmpeg.")
            logger.error(f"FFmpeg merge failed for Lecture {video_index}: {e}")
    else:
        await progress_message.edit(f"Lecture {video_index} has no parts downloaded to merge.")
        logger.warning(f"No parts downloaded for Lecture {video_index}.")

@client.on(events.NewMessage(pattern=r'^\.iit\s+(.+)', outgoing=True))
async def handle_links(event):
    logger.info(f"Received .iit command from user.")
    user_input = event.pattern_match.group(1)
    links = user_input.split()
    if len(links) > MAX_LINKS:
        await event.reply(f"Please provide up to {MAX_LINKS} links only.")
        logger.warning(f"More than {MAX_LINKS} links received.")
        return

    valid_links = []
    for link in links:
        prefix, base_path, parsed_url = extract_details(link)
        if prefix is None:
            await event.reply(f"The specified URL is not in the correct format: {link}")
            logger.error(f"Invalid link format: {link}")
            return
        valid_links.append(link)

    clear_base_dir()

    await event.reply("Processing your links now...")
    logger.info(f"Processing {len(valid_links)} links.")

    for idx, link in enumerate(valid_links, 1):
        video_index = idx
        await download_and_merge(link, idx, video_index, event)

@client.on(events.NewMessage(pattern=r'^\.ping$', outgoing=True))
async def ping(event):
    logger.info(f"Received .ping command.")
    await event.reply("✅ Userbot is alive and running!")

def main():
    client.start()
    logger.info("Userbot started successfully.")
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
