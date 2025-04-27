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

# Locking the processing to prevent multiple simultaneous .iit commands
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

    # Create a thumbnail if path is provided
    if thumb_path:
        create_thumbnail(video_path, thumb_path)

    return width, height, duration

def create_thumbnail(video_path, thumb_path):
    try:
        (
            ffmpeg
            .input(video_path, ss=1)  # Capture frame at 1 second
            .filter('scale', 320, -1)  # Resize width to 320px, maintain aspect ratio
            .output(thumb_path, vframes=1)
            .overwrite_output()
            .run(quiet=True)
        )
    except Exception as e:
        logger.error(f"Failed to create thumbnail: {e}")


import aiohttp

# New helper for downloading parts asynchronously
async def download_part(session, url, save_path, semaphore):
    async with semaphore:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        while True:
                            chunk = await resp.content.read(1024)
                            if not chunk:
                                break
                            f.write(chunk)
                    return True
                else:
                    logger.warning(f"Part not found at {url} (HTTP {resp.status})")
                    return False
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
            return False

# Modify your download_and_merge
async def download_and_merge(link, folder_index, video_index, event):
    logger.info(f"Processing video {video_index} for link: {link}")
    topic_id = None 

    if getattr(event.reply_to, 'forum_topic', None):
        topic_id = top if (top := event.reply_to.reply_to_top_id) else event.reply_to_msg_id
    else:
        topic_id = event.reply_to_msg_id

    prefix, base_path, parsed_url = extract_details(link)
    if prefix is None:
        await client.send_message(event.chat_id, f"Invalid URL format: {link}", reply_to=topic_id)
        logger.error(f"Invalid URL format: {link}")
        return

    output_dir = os.path.join(BASE_DIR, str(folder_index))
    os.makedirs(output_dir, exist_ok=True)
    query = parsed_url.query

    progress_message = await client.send_message(event.chat_id, f"Lecture {video_index}\nDownloading...", reply_to=topic_id)

    downloaded_files = []
    misses = 0
    download_tasks = []
    max_parallel_downloads = 10  # limit to 10 concurrent downloads per video
    semaphore = asyncio.Semaphore(max_parallel_downloads)

    async with aiohttp.ClientSession() as session:
        for i in range(START_PART, MAX_PARTS):
            part_name = f"{prefix}{i:03d}.ts"
            full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{base_path}/{part_name}"
            if query:
                full_url += f"?{query}"

            local_path = os.path.join(output_dir, part_name)

            if os.path.exists(local_path):
                logger.debug(f"Skipping existing part: {part_name}")
                downloaded_files.append(part_name)
                continue

            # create a download task for each part
            task = asyncio.create_task(download_part(session, full_url, local_path, semaphore))
            download_tasks.append((task, part_name))

        # Run all downloads
        results = await asyncio.gather(*(task for task, _ in download_tasks))

        # Check which parts downloaded successfully
        for (success, (task, part_name)) in zip(results, download_tasks):
            if success:
                downloaded_files.append(part_name)
            else:
                misses += 1
                if misses >= STOP_AFTER_MISSES:
                    logger.warning(f"Stopping download after {STOP_AFTER_MISSES} consecutive misses.")
                    break

    if not downloaded_files:
        await progress_message.edit(f"Lecture {video_index}\nNo parts downloaded!")
        logger.warning(f"No parts downloaded for Lecture {video_index}.")
        return

    # Now merging and uploading same as your code...

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
                    pass

        thumbnail_path = os.path.join(output_dir, f"thumb_{video_index}.jpg")
        width, height, duration = get_video_metadata(output_video, thumb_path=thumbnail_path)

        await client.send_file(
            event.chat_id,
            output_video,
            reply_to=topic_id,
            caption=f"Lecture {video_index}",
            thumb=thumbnail_path,
            progress_callback=progress_callback,
            attributes=[
                DocumentAttributeVideo(
                    duration=int(duration),
                    w=width,
                    h=height,
                    supports_streaming=True
                )
            ]
        )
        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)

        await progress_message.delete()

        logger.info(f"Lecture {video_index} uploaded successfully.")

        # Clean up
        os.remove(list_path)
        for file in downloaded_files:
            try:
                os.remove(os.path.join(output_dir, file))
            except Exception as e:
                logger.warning(f"Error deleting {file}: {e}")
        os.remove(output_video)
        logger.info(f"Cleaned up files for Lecture {video_index}.")

    except subprocess.CalledProcessError:
        await progress_message.edit(f"Lecture {video_index}\nMerging failed ❌")
        logger.error(f"FFmpeg merging failed for Lecture {video_index}")


@client.on(events.NewMessage(pattern=r'^\.iit\s+(.+)', outgoing=True))
async def handle_iit_command(event):
    topic_id = None 
    if getattr(event.reply_to, 'forum_topic', None):
        topic_id = event.reply_to.reply_to_top_id
    else:
        topic_id = event.reply_to_msg_id

    global is_processing
    if is_processing:
        await client.send_message(event.chat_id, "❌ Another task is already in progress. Please wait for it to finish before running again.", reply_to=topic_id)
        return

    set_processing_status(True)
    await event.delete()
    logger.info("Received .iit command.")

    user_input = event.pattern_match.group(1)
    parts = user_input.split()

    if len(parts) < 2:
        await client.send_message(event.chat_id, "❌ Usage: `.iit <start_no> <link1> <link2> ...`\n(up to 5 links allowed)", reply_to=topic_id)
        set_processing_status(False)
        return

    try:
        start_index = int(parts[0])
    except ValueError:
        await client.send_message(event.chat_id, "❌ Start number must be an integer.\nUsage: `.iit <start_no> <link1> <link2> ...`", reply_to=topic_id)
        set_processing_status(False)
        return

    links = parts[1:]
    if len(links) > MAX_LINKS:
        await client.send_message(event.chat_id, f"❌ You can provide up to {MAX_LINKS} links only.", reply_to=topic_id)
        set_processing_status(False)
        return

    valid_links = []
    for link in links:
        prefix, base_path, parsed_url = extract_details(link)
        if prefix is None:
            await client.send_message(event.chat_id, f"❌ Invalid URL format: {link}", reply_to=topic_id)
            set_processing_status(False)
            return
        valid_links.append(link)

    clear_base_dir()

    # Process all links concurrently
    tasks = []
    for idx, link in enumerate(valid_links):
        video_index = start_index + idx
        tasks.append(asyncio.create_task(
            download_and_merge(link, idx + 1, video_index, event)
        ))

    await asyncio.gather(*tasks)

    set_processing_status(False)

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
