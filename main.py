import os
import re
import subprocess
import requests
from urllib.parse import urlparse
from telegram import Update, Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Configuration
MAX_LINKS = 5
BASE_DIR = "IITJAM"
MAX_PARTS = 10000
START_PART = 0
STOP_AFTER_MISSES = 3 


try:
    my_var = os.environ['BOT_TOKEN'] 
    print(f'MY_VARIABLE is: {my_var}')
except KeyError:
    raise EnvironmentError('Required environment variable MY_VARIABLE is missing!')


# Ensure BASE_DIR exists
os.makedirs(BASE_DIR, exist_ok=True)

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

def download_and_merge(link, folder_index, video_index, update: Update, context: CallbackContext):
    prefix, base_path, parsed_url = extract_details(link)
    if prefix is None:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text=f"Invalid URL format: {link}")
        return

    output_dir = os.path.join(BASE_DIR, str(folder_index))
    os.makedirs(output_dir, exist_ok=True)
    query = parsed_url.query

    downloaded_files = []

    # Send initial message
    progress_message = context.bot.send_message(chat_id=update.effective_chat.id,
                                                text=f"Lecture {video_index}\nDownloading parts... [0%]")

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
                # Update progress
                progress = int((len(downloaded_files) / MAX_PARTS) * 100)
                context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                              message_id=progress_message.message_id,
                                              text=f"Lecture {video_index}\nDownloading parts... [{progress}%]")
            else:
                misses += 1
                if misses >= STOP_AFTER_MISSES:
                    break
        except Exception as e:
            misses += 1
            if misses >= STOP_AFTER_MISSES:
                break

    if downloaded_files:
        list_path = os.path.join(output_dir, "file_list.txt")
        with open(list_path, 'w') as f:
            for name in downloaded_files:
                f.write(f"file '{name}'\n")

        output_video = os.path.join(output_dir, f"Lecture{video_index}.mp4")
        try:
            # Update progress to merging
            context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                          message_id=progress_message.message_id,
                                          text=f"Lecture {video_index}\nMerging parts... [0%]")

            subprocess.run([
                "ffmpeg", "-f", "concat", "-safe", "0",
                "-i", "file_list.txt", "-c", "copy", f"Lecture{video_index}.mp4"
            ], cwd=output_dir, check=True)

            # Update progress to uploading
            context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                          message_id=progress_message.message_id,
                                          text=f"Lecture {video_index}\nUploading to Telegram... [0%]")

            # Send video with progress
            with open(output_video, 'rb') as video_file:
                context.bot.send_document(chat_id=update.effective_chat.id,
                                          document=video_file,
                                          filename=os.path.basename(output_video))

            # Cleanup
            os.remove(list_path)
            for file in downloaded_files:
                os.remove(os.path.join(output_dir, file))
            os.remove(output_video)

            # Final message
            context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                          message_id=progress_message.message_id,
                                          text=f"Lecture {video_index} processing completed successfully.")
        except subprocess.CalledProcessError:
            context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                          message_id=progress_message.message_id,
                                          text=f"Lecture {video_index} merging failed using ffmpeg.")
    else:
        context.bot.edit_message_text(chat_id=update.effective_chat.id,
                                      message_id=progress_message.message_id,
                                      text=f"Lecture {video_index} has no parts downloaded to merge.")

def start(update: Update, context: CallbackContext):
    update.message.reply_text("Bot is alive and ready to process your video links!")

def handle_links(update: Update, context: CallbackContext):
    user_input = update.message.text.strip()
    links = user_input.split()
    if len(links) > MAX_LINKS:
        update.message.reply_text(f"Please provide up to {MAX_LINKS} links.")
        return

    valid_links = []
    for link in links:
        prefix, base_path, parsed_url = extract_details(link)
        if prefix is None:
            update.message.reply_text(f"The specified URL is not in the correct format: {link}")
            return
        valid_links.append(link)

    update.message.reply_text("Processing your links. This may take a while...")

    for idx, link in enumerate(valid_links, 1):
        video_index = idx
        download_and_merge(link, idx, video_index, update, context)

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_links))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

