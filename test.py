from pyrogram import Client, filters
from dotenv import load_dotenv
import os

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# NOTE: this uses the SAME bot token as stick.py. Do not run this at the
# same time as stick.py — Telegram only allows one active getUpdates
# connection per bot token, so running both together makes one (or both)
# of them silently stop receiving messages.

app = Client(
    "test_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

@app.on_message(filters.command("start"))
async def start(_, message):
    print("START RECEIVED")
    await message.reply_text("Bot Working ✅")

print("Starting...")
app.run()
