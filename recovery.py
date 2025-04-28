import json
import os
import time
import glob
import logging
from telethon import TelegramClient
from telethon.tl.types import PeerChat
from datetime import datetime
from telethon.tl.types import DocumentAttributeVideo
from urllib.parse import urlparse
from telethon import TelegramClient, events
from telethon.sessions import StringSession


API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
chat_id = int(-1002573548846)

# Setup logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

if SESSION_STRING:
    logger.info("Using provided session string to login.")
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
else:
    logger.error("SESSION_STRING is missing. Please set it in environment variables.")
    raise ValueError("SESSION_STRING environment variable not found!")

with open("dump.json","r") as file:
    content = json.load(file)
    # sort by date:
    content = sorted(content, key=lambda x: x["date"])
    
    client.start()

    group = client.get_entity(PeerChat(chat_id))

    for msg in content:

        message_id = msg["id"]
        message = msg["message"]
        has_media = msg['media'] != None
        has_message = message != ""
        date = datetime.fromisoformat(msg["date"]).strftime("%Y %b %d, %H:%M")

        # print message, date, and attachment info:
        print(f"{i} {message}, {date}, has_media: {has_media}")

        if (has_message):
            message = str(date) + "\n\n" + str(message)
        else:
            message = str(date)

        did_send_media_msg = False

        if has_media:
            file_names = glob.glob(f"{message_id}.*")
            for file_name in file_names:
                print(f"Sending Media: {file_name}")
                client.send_file(entity=group, file=file_name, caption=message, silent=True)
                did_send_media_msg = True

        elif has_message or (not did_send_media_msg):
            print(f"Sending Message: {message}")
            client.send_message(entity=group, message=message, silent=True)

        # sleep to avoid rate limiting, you may experiment with reducing this time:
        time.sleep(1)
      
