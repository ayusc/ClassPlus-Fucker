from pyrogram import Client, filters
import os
import requests
import subprocess
import re
from urllib.parse import urlparse

# Constants
BASE_DIR = "/sdcard/IITJAM"
MAX_LINKS = 5
MAX_PARTS = 10000
START_PART = 0
STOP_AFTER_MISSES = 3

os.makedirs(BASE_DIR, exist_ok=True)

# Helper: Extract base info from link
def extract_details(ts_url):
    parsed_url = urlparse(ts_url)
    path_parts = parsed_url.path.rsplit('/', 1)
    filename = path_parts[-1]

    match1 = re.match(r"(720p)_(\d{3})\.ts", filename)
    match2 = re.match(r"(data)(\d+)\.ts", filename)

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

# Helper: Download and merge video
async def download_and_merge(link, folder_index, video_index, event):
    prefix, base_path, parsed_url = extract_details(link)
    if prefix is None:
        await event.reply(f"Invalid URL format: {link}")
        return

    output_dir = os.path.join(BASE_DIR, str(folder_index))
    os.makedirs(output_dir, exist_ok=True)
    query = parsed_url.query

    downloaded_files = []
    progress_message = await event.reply(f"Lecture {video_index}\nDownloading parts...")

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
                misses = 0
            else:
                misses += 1
                if misses >= STOP_AFTER_MISSES:
                    break
        except:
            misses += 1
            if misses >= STOP_AFTER_MISSES:
                break

    if not downloaded_files:
        await progress_message.edit(f"Lecture {video_index}\nNo parts downloaded to merge.")
        return

    list_path = os.path.join(output_dir, "file_list.txt")
    with open(list_path, 'w') as f:
        for name in downloaded_files:
            f.write(f"file '{name}'\n")

    output_video = os.path.join(output_dir, f"Lecture{video_index}.mp4")

    try:
        await progress_message.edit(f"Lecture {video_index}\nMerging parts...")

        subprocess.run(
            [
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", "file_list.txt", "-c", "copy", f"Lecture{video_index}.mp4"
            ],
            cwd=output_dir,
            check=True
        )

        await progress_message.edit(f"Lecture {video_index}\nUploading... 0%")

        # Upload with progress
        file_size = os.path.getsize(output_video)
        last_reported = -5

        async def progress_callback(current, total):
            nonlocal last_reported
            percent = int((current / total) * 100)
            if percent >= last_reported + 5:
                last_reported = percent
                try:
                    await progress_message.edit(f"Lecture {video_index}\nUploading... {percent}%")
                except:
                    pass  # safe ignore

        await event.client.send_document(
            event.chat.id,
            output_video,
            caption=f"Lecture {video_index}",
            progress=progress_callback
        )

        await progress_message.edit(f"Lecture {video_index}\nCompleted ✅")

    except Exception as e:
        await progress_message.edit(f"Lecture {video_index}\nError: {e}")

    finally:
        # Cleanup
        try:
            os.remove(list_path)
            for file in downloaded_files:
                os.remove(os.path.join(output_dir, file))
            os.remove(output_video)
        except Exception as e:
            print(f"Cleanup error: {e}")

# Main command handler
@Client.on_message(filters.command("iit"))
async def iit_handler(client, message):
    args = message.text.split()
    
    if len(args) < 3:
        await message.reply_text("❌ Usage: `.iit <start_no> <link1> <link2> ...`\n(Up to 5 links allowed.)")
        return

    try:
        start_index = int(args[1])
    except ValueError:
        await message.reply_text("❌ Start number must be an integer.\nUsage: `.iit <start_no> <link1> <link2> ...`")
        return

    links = args[2:2 + MAX_LINKS]

    for idx, link in enumerate(links, start=0):
        await download_and_merge(link, folder_index=idx + 1, video_index=start_index + idx, event=message)

