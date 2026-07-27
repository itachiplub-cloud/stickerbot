# -*- coding: utf-8 -*-
import os
import sys
import time
import re
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

# ==============================================
# LOAD ENVIRONMENT VARIABLES
# ==============================================
load_dotenv()  # Load .env file

# ==============================================
# BOT CONFIGURATION FROM .env
# (FIX: these were hardcoded before, .env was loaded but never used)
# ==============================================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "sticker_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "users")

# FIX: ADMIN_USER_ID must be int(s) for filters.user() to match by Telegram ID.
# Passing it as a string made filters.user() try to match it against a
# @username instead of the numeric id, so admin-only commands never fired.
# Supports one or more comma-separated IDs, e.g. ADMIN_USER_ID=id1,id2
_admin_raw = os.getenv("ADMIN_USER_ID", "").strip()
ADMIN_IDS = []
for _piece in _admin_raw.split(","):
    _piece = _piece.strip()
    if _piece.isdigit():
        ADMIN_IDS.append(int(_piece))

# Kept for anything that still wants a single "primary" admin (e.g. logging)
ADMIN_USER_ID = ADMIN_IDS[0] if ADMIN_IDS else None

DEFAULT_EMOJI = os.getenv("DEFAULT_EMOJI", "✨")
TEMP_DIR = os.getenv("TEMP_DIR", "temp_stickers")

# ==============================================
# STARTUP SANITY CHECKS
# (FIX: fail loudly instead of hanging/crashing silently)
# ==============================================
missing = [k for k, v in {
    "API_ID": API_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN,
    "MONGODB_URI": MONGODB_URI,
}.items() if not v]
if missing:
    print(f"❌ Missing required .env values: {', '.join(missing)}")
    print("   Fill these in your .env file before starting the bot.")
    sys.exit(1)

if not ADMIN_IDS:
    print("❌ ADMIN_USER_ID is missing/invalid in your .env file.")
    print("   Add a line like: ADMIN_USER_ID=805508459")
    print("   or for multiple admins: ADMIN_USER_ID=805508459,123456789")
    print("   (numeric Telegram user IDs only, comma-separated, no quotes, no @).")
    print("   This is required -- admin commands AND the force-subscribe")
    print("   gate both depend on knowing who the admin(s) are.")
    sys.exit(1)

API_ID = int(API_ID)

# ==============================================
# CREATE TEMP DIRECTORY
# ==============================================
os.makedirs(TEMP_DIR, exist_ok=True)

# ==============================================
# CONNECT TO MONGODB
# (FIX: surface connection errors immediately instead of hanging)
# ==============================================
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
    mongo_client.admin.command("ping")  # forces the connection to actually be tested now
except Exception as e:
    print(f"❌ Could not connect to MongoDB: {e}")
    sys.exit(1)

db = mongo_client[DB_NAME]
users_collection = db[COLLECTION_NAME]

# ==============================================
# CREATE THE BOT CLIENT (module-level, so handlers can bind to it)
# FIX: previously all handlers were declared with @app.on_message(...)
# (the CLASS, not an instance). Pyrogram only auto-registers those via the
# "Smart Plugins" system (Client(..., plugins=dict(root=...))), which this
# script never used. That meant NONE of the handlers were ever attached to
# the running bot -- it connected to Telegram fine and printed "ready",
# but had zero handlers, so nothing ever responded to anything.
# FIX: create `app` here and use @app.on_message / @app.on_callback_query
# everywhere below, which binds handlers directly to this instance.
# ==============================================
app = Client(
    "sticker_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ==============================================
# FORCE-SUBSCRIBE ("Must Join") SYSTEM
# See force_subscribe.py for the module itself.
# Add FORCE_SUB_CHANNELS=@channel1,@channel2 to .env to pre-seed channels,
# or leave empty and manage entirely with /addchannel, /removechannel, /channels.
# ==============================================
from force_subscribe import ForceSubscribe
fsub = ForceSubscribe(app, db, admin_ids=ADMIN_IDS)

# ==============================================
# SMALL CAPS CONVERTER
# ==============================================
def small_caps(text: str) -> str:
    mapping = {
        'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ꜰ','g':'ɢ','h':'ʜ','i':'ɪ','j':'ᴊ','k':'ᴋ','l':'ʟ','m':'ᴍ','n':'ɴ','o':'ᴏ','p':'ᴘ','q':'ǫ','r':'ʀ','s':'s','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x','y':'ʏ','z':'ᴢ',
        'A':'ᴀ','B':'ʙ','C':'ᴄ','D':'ᴅ','E':'ᴇ','F':'ꜰ','G':'ɢ','H':'ʜ','I':'ɪ','J':'ᴊ','K':'ᴋ','L':'ʟ','M':'ᴍ','N':'ɴ','O':'ᴏ','P':'ᴘ','Q':'ǫ','R':'ʀ','S':'s','T':'ᴛ','U':'ᴜ','V':'ᴠ','W':'ᴡ','X':'x','Y':'ʏ','Z':'ᴢ',
        '0':'𝟶','1':'𝟷','2':'𝟸','3':'𝟹','4':'𝟺','5':'𝟻','6':'𝟼','7':'𝟽','8':'𝟾','9':'𝟿'
    }
    return ''.join(mapping.get(ch, ch) for ch in text)

# ==============================================
# DEBUG LOGGER
# (FIX: was replying "✅ Debug Command Working" on every real command,
#  spamming users with an extra message. Now it only prints to console.
#  Set DEBUG_MODE = True below if you want the console logging back.)
# ==============================================
DEBUG_MODE = False

if DEBUG_MODE:
    @app.on_message(group=-100)
    async def debug_messages(client: Client, message: Message):
        print("\n" + "=" * 60)
        print("[DEBUG] MESSAGE RECEIVED")
        print(f"Chat ID     : {message.chat.id}")
        print(f"Chat Type   : {message.chat.type}")
        print(f"User ID     : {message.from_user.id if message.from_user else 'Unknown'}")
        print(f"Username    : @{message.from_user.username if message.from_user else 'Unknown'}")
        print(f"Text        : {message.text}")
        print(f"Caption     : {message.caption}")
        print("=" * 60)

    @app.on_callback_query(group=-100)
    async def debug_callbacks(client: Client, callback: CallbackQuery):
        print("\n" + "=" * 60)
        print("[DEBUG] CALLBACK RECEIVED")
        print(f"User ID     : {callback.from_user.id}")
        print(f"Data        : {callback.data}")
        print("=" * 60)

# ==============================================
# MONGODB STORAGE HELPERS
# ==============================================
def get_user_data(user_id):
    """Get user data from MongoDB"""
    user_data = users_collection.find_one({"user_id": str(user_id)})
    if not user_data:
        user_data = {
            "user_id": str(user_id),
            "pack_name": None,
            "emoji": DEFAULT_EMOJI,
            "total_stickers": 0,
            "created_at": datetime.now(),
            "last_active": datetime.now()
        }
        users_collection.insert_one(user_data)
    return user_data

def update_user_data(user_id, update_data):
    """Update user data in MongoDB"""
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$set": {**update_data, "last_active": datetime.now()}}
    )

def get_user_pack(user_id):
    """Get user's sticker pack info"""
    user_data = get_user_data(user_id)
    return {
        "name": user_data.get("pack_name"),
        "emoji": user_data.get("emoji", DEFAULT_EMOJI)
    }

def update_user_pack(user_id, pack_name, emoji=None):
    """Update user's sticker pack info"""
    update_data = {"pack_name": pack_name}
    if emoji:
        update_data["emoji"] = emoji
    update_user_data(user_id, update_data)

def increment_sticker_count(user_id):
    """Increment total stickers count for user"""
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$inc": {"total_stickers": 1}, "$set": {"last_active": datetime.now()}}
    )

def get_all_users():
    """Get all users from database"""
    return list(users_collection.find())

def get_stats():
    """Get bot statistics"""
    total_users = users_collection.count_documents({})
    total_stickers = users_collection.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$total_stickers"}}}
    ])
    total_stickers = list(total_stickers)
    return {
        "total_users": total_users,
        "total_stickers": total_stickers[0]["total"] if total_stickers else 0
    }

# ==============================================
# IMAGE PROCESSING WITH PILLOW
# ==============================================
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/Arial.ttf",
    os.getenv("CUSTOM_FONT_PATH", "")
]

def get_font(size):
    for path in FONT_PATHS:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

def draw_text_with_outline(draw, xy, text, font):
    """Draw white text with thick black outline (8 directions)"""
    x, y = xy
    offsets = [(-2,-2),(-2,0),(-2,2),(0,-2),(0,2),(2,-2),(2,0),(2,2)]
    for dx, dy in offsets:
        draw.text((x+dx, y+dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill="white")

def wrap_text(text, max_width, font, draw):
    words = text.split()
    lines = []
    cur = []
    for w in words:
        test = ' '.join(cur + [w])
        bbox = draw.textbbox((0,0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(' '.join(cur))
            cur = [w]
    if cur:
        lines.append(' '.join(cur))
    return '\n'.join(lines)

def create_sticker(image_bytes, text, position, size_multiplier=1.0):
    """
    position: "top-left", "top-mid", "top-right", "mid-left", "center",
              "mid-right", "bot-left", "bot-mid", "bot-right"
    size_multiplier: 0.7 (small), 1.0 (medium), 1.4 (large), 1.8 (xlarge)
    """
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(img)

    base_font_size = max(20, img.width // 12)
    font_size = int(base_font_size * size_multiplier)
    font = get_font(font_size)

    max_w = int(img.width * 0.8)
    wrapped = wrap_text(text, max_w, font, draw)

    bbox = draw.multiline_textbbox((0,0), wrapped, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad = 20
    if "left" in position:
        x = pad
    elif "right" in position:
        x = img.width - tw - pad
    else:
        x = (img.width - tw) // 2

    if "top" in position:
        y = pad
    elif "bot" in position:
        y = img.height - th - pad
    else:
        y = (img.height - th) // 2

    lines = wrapped.split('\n')
    line_height = font_size + 5
    cy = y
    for line in lines:
        draw_text_with_outline(draw, (x, cy), line, font)
        cy += line_height

    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

# ==============================================
# PYROGRAM STICKER SET HELPERS
# ==============================================
async def create_sticker_pack(client, user_id, pack_name, title, sticker_bytes, emoji):
    """Create a new sticker set on Telegram"""
    sticker_file = BytesIO(sticker_bytes)
    sticker_file.name = "sticker.png"
    try:
        await client.create_new_sticker_set(
            user_id=user_id,
            name=pack_name,
            title=title,
            png_sticker=sticker_file,
            emojis=emoji
        )
        return True, f"https://t.me/addstickers/{pack_name}"
    except Exception as e:
        return False, str(e)

async def add_sticker_to_pack(client, user_id, pack_name, sticker_bytes, emoji):
    """Add a sticker to an existing pack"""
    sticker_file = BytesIO(sticker_bytes)
    sticker_file.name = "sticker.png"
    try:
        await client.add_sticker_to_set(
            user_id=user_id,
            name=pack_name,
            png_sticker=sticker_file,
            emojis=emoji
        )
        return True, None
    except Exception as e:
        return False, str(e)

# ==============================================
# SESSION STORAGE
# ==============================================
user_sessions = {}  # {user_id: {"step": str, "image_bytes": bytes, "text": str, "size": float, "position": str}}

# ==============================================
# /start – FIX: there was no real start handler before, only the debug echo
# ==============================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    await message.reply_text(
        small_caps(
            "🌟 **Sticker Maker Bot** 🌟\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Welcome! I can turn your images into high-quality Telegram stickers.\n\n"
            "🖼 **/sticker**\n"
            "Create a sticker from any image.\n\n"
            "📦 **/mypacks**\n"
            "Manage and view your sticker packs.\n\n"
            "😊 **/setemoji <emoji>**\n"
            "Choose the default emoji for new stickers.\n\n"
            "🚀 Just send me an image to begin!"
        )
    )

# ==============================================
# COMMAND: /sticker
# ==============================================
@app.on_message(filters.command("sticker") & filters.private)
async def sticker_start(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "waiting_image"}
    await message.reply_text(small_caps("📸 **Step 1/4:** Send me an image (photo or document)."))

# ==============================================
# HANDLE IMAGE
# ==============================================
@app.on_message((filters.photo | filters.document) & filters.private, group=5)
async def handle_image(client: Client, message: Message):
    if not message.from_user:
        return  # defensive: shouldn't happen in private chats, but just in case
    uid = message.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_image":
        return

    file_id = None
    if message.photo:
        file_id = message.photo.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.reply_text(small_caps("❌ Please send a valid image file."))
        return

    proc = await message.reply_text(small_caps("⚙️ Downloading image..."))
    try:
        file = await client.download_media(file_id, in_memory=True)
        # FIX: getbuffer() returns a memoryview tied to the BytesIO object;
        # getvalue() returns an independent bytes copy that's safe to keep
        # in user_sessions long after `file` goes out of scope.
        img_bytes = file.getvalue()
        user_sessions[uid]["image_bytes"] = img_bytes
        user_sessions[uid]["step"] = "waiting_text"
        await proc.edit_text(small_caps("✏️ **Step 2/4:** Send me the text you want on the sticker."))
    except Exception as e:
        await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))

# ==============================================
# HANDLE TEXT
# ==============================================
@app.on_message(
    filters.text & filters.private &
    ~filters.command(["sticker", "setemoji", "mypacks", "stats", "users", "reset", "start"]),
    group=6
)
async def handle_text(client: Client, message: Message):
    if not message.from_user:
        return  # defensive: e.g. a channel post or anonymous-admin message slipping through
    uid = message.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_text":
        return

    text = message.text.strip()
    if not text:
        await message.reply_text(small_caps("❌ Please send some text."))
        return

    user_sessions[uid]["text"] = text
    user_sessions[uid]["step"] = "waiting_size"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔹 Sᴍᴀʟʟ", callback_data="size_small"),
         InlineKeyboardButton("🔸 Mᴇᴅɪᴜᴍ", callback_data="size_medium")],
        [InlineKeyboardButton("🔹 Lᴀʀɢᴇ", callback_data="size_large"),
         InlineKeyboardButton("🔸 Exᴛʀᴀ Lᴀʀɢᴇ", callback_data="size_xlarge")]
    ])
    await message.reply_text(small_caps("🔧 **Step 3/4:** Choose text size:"), reply_markup=kb)

# ==============================================
# SIZE CALLBACK
# FIX: was `@app.on_callback_query()` with no filter, which matches every
# callback query. Since it was registered before position_callback in the
# same default group, it silently swallowed "pos_*" callbacks too, so
# position_callback could never run. Scoping the filter to "^size_" fixes it.
# ==============================================
@app.on_callback_query(filters.regex(r"^size_"))
async def size_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_size":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    size_map = {
        "size_small": 0.7,
        "size_medium": 1.0,
        "size_large": 1.4,
        "size_xlarge": 1.8
    }
    multiplier = size_map.get(callback.data, 1.0)
    user_sessions[uid]["size"] = multiplier
    user_sessions[uid]["step"] = "waiting_position"

    positions = [
        ("Tᴏᴘ Lᴇꜰᴛ", "top-left"), ("Tᴏᴘ Mɪᴅ", "top-mid"), ("Tᴏᴘ Rɪɢʜᴛ", "top-right"),
        ("Mɪᴅ Lᴇꜰᴛ", "mid-left"), ("Cᴇɴᴛᴇʀ", "center"), ("Mɪᴅ Rɪɢʜᴛ", "mid-right"),
        ("Bᴏᴛ Lᴇꜰᴛ", "bot-left"), ("Bᴏᴛ Mɪᴅ", "bot-mid"), ("Bᴏᴛ Rɪɢʜᴛ", "bot-right")
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[:3]],
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[3:6]],
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[6:]]
    ])
    await callback.message.edit_text(small_caps("📍 **Step 4/4:** Choose text position:"), reply_markup=kb)
    await callback.answer()

# ==============================================
# POSITION CALLBACK + FINALIZE
# FIX: scoped filter to "^pos_" so this handler actually gets reached now.
# ==============================================
@app.on_callback_query(filters.regex(r"^pos_"))
async def position_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_position":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    position = callback.data.replace("pos_", "")
    user_sessions[uid]["position"] = position
    user_sessions[uid]["step"] = "finalizing"

    await callback.answer(small_caps("⚙️ Processing sticker..."))

    img_bytes = user_sessions[uid]["image_bytes"]
    text = user_sessions[uid]["text"]
    size_mult = user_sessions[uid]["size"]

    try:
        sticker_bytes = create_sticker(img_bytes, text, position, size_mult)
        user_data = get_user_pack(uid)
        pack_name = user_data["name"]
        emoji = user_data["emoji"]

        if pack_name:
            success, err = await add_sticker_to_pack(client, uid, pack_name, sticker_bytes, emoji)
            if success:
                link = f"https://t.me/addstickers/{pack_name}"
                increment_sticker_count(uid)
                await callback.message.reply_document(BytesIO(sticker_bytes), file_name="sticker.png",
                    caption=small_caps(f"✅ Sticker added to your pack!\n🔗 {link}"))
            else:
                await callback.message.reply_text(small_caps(f"❌ Failed to add sticker: {err}"))
        else:
            try:
                user = await client.get_users(uid)
                base = user.username or f"user_{uid}"
                base = re.sub(r'[^a-zA-Z0-9_]', '_', base)[:30]
                pack_name = f"{base}_stickers"
                title = f"{base}'s Stickers"
            except Exception:
                pack_name = f"pack_{uid}_{int(time.time())}"
                title = "My Sticker Pack"

            success, result = await create_sticker_pack(client, uid, pack_name, title, sticker_bytes, emoji)
            if success:
                update_user_pack(uid, pack_name, emoji)
                increment_sticker_count(uid)
                await callback.message.reply_document(BytesIO(sticker_bytes), file_name="sticker.png",
                    caption=small_caps(f"✅ Pack created!\n🔗 {result}"))
            else:
                await callback.message.reply_text(small_caps(f"❌ Failed to create pack: {result}"))
    except Exception as e:
        await callback.message.reply_text(small_caps(f"❌ Error: {str(e)}"))
    finally:
        user_sessions.pop(uid, None)
        try:
            await callback.message.delete_reply_markup()
        except Exception:
            pass

# ==============================================
# /setemoji – set default emoji for future stickers
# ==============================================
@app.on_message(filters.command("setemoji") & filters.private)
async def set_emoji_cmd(client: Client, message: Message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(small_caps("❌ Usage: /setemoji 😀"))
        return
    emoji = args[1].strip()
    if not emoji:
        await message.reply_text(small_caps("❌ Please send a valid emoji."))
        return

    get_user_data(uid)
    update_user_data(uid, {"emoji": emoji})
    await message.reply_text(small_caps(f"✅ Default emoji set to {emoji}"))

# ==============================================
# /mypacks – show user's sticker pack link
# ==============================================
@app.on_message(filters.command("mypacks") & filters.private)
async def mypacks_cmd(client: Client, message: Message):
    uid = message.from_user.id
    user_data = get_user_pack(uid)
    pack_name = user_data["name"]

    if not pack_name:
        await message.reply_text(small_caps("You have no sticker pack yet. Create one with /sticker."))
        return

    link = f"https://t.me/addstickers/{pack_name}"
    doc = users_collection.find_one({"user_id": str(uid)})
    total_stickers = doc.get("total_stickers", 0) if doc else 0

    await message.reply_text(
        small_caps(f"📦 **Your Pack:** {link}\n"
                   f"📊 **Total Stickers:** {total_stickers}\n"
                   f"😊 **Default Emoji:** {user_data['emoji']}")
    )

# ==============================================
# /stats – show bot statistics (admin only)
# FIX: filters.user() now gets real int ID(s), so this actually matches
# the admin(s) -- accepts a list, so multiple admins all work.
# ==============================================
@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def stats_cmd(client: Client, message: Message):
    stats = get_stats()
    await message.reply_text(
        small_caps(f"📊 **Bot Statistics:**\n\n"
                   f"👥 **Total Users:** {stats['total_users']}\n"
                   f"🎨 **Total Stickers:** {stats['total_stickers']}")
    )

# ==============================================
# /users – list all users (admin only)
# ==============================================
@app.on_message(filters.command("users") & filters.user(ADMIN_IDS))
async def users_cmd(client: Client, message: Message):
    users = get_all_users()
    if not users:
        await message.reply_text(small_caps("No users found."))
        return

    user_list = []
    for user in users:
        user_list.append(f"👤 User ID: {user['user_id']}\n"
                        f"📦 Pack: {user.get('pack_name', 'None')}\n"
                        f"🎨 Stickers: {user.get('total_stickers', 0)}\n")

    user_text = small_caps("📋 **User List:**\n\n") + "\n".join(user_list[:20])
    if len(user_list) > 20:
        user_text += small_caps("\n\n... and more users.")

    await message.reply_text(user_text)

# ==============================================
# /reset – reset user's sticker pack (dangerous)
# FIX: was two separate handlers in the same group where the base "/reset"
# filter always matched first, so "/reset confirm" could never reach the
# actual delete logic. Merged into one handler that checks the args itself.
# ==============================================
@app.on_message(filters.command("reset") & filters.private)
async def reset_cmd(client: Client, message: Message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    confirmed = len(args) > 1 and args[1].strip().lower() == "confirm"

    if not confirmed:
        await message.reply_text(
            small_caps("⚠️ **WARNING:** This will delete your sticker pack data from the database.\n"
                       "Your Telegram sticker pack will still exist but won't be linked to the bot.\n\n"
                       "To confirm, send: `/reset confirm`")
        )
        return

    users_collection.delete_one({"user_id": str(uid)})
    await message.reply_text(small_caps("✅ Your data has been reset. You can start fresh with /sticker."))

# ==============================================
# INITIALIZE AND RUN BOT
# ==============================================
def main():
    print(small_caps("🚀 Starting Sticker Bot..."))

    with app:
        me = app.get_me()

        print(small_caps(f"📊 Bot Username: @{me.username}"))
        print(small_caps(f"🤖 Bot Name: {me.first_name}"))
        print(small_caps(f"📁 Database: {DB_NAME}"))
        print(small_caps(f"👤 Admin ID(s): {', '.join(str(a) for a in ADMIN_IDS)}"))
        print(small_caps("✅ Bot is ready!"))

        users_collection.create_index("user_id", unique=True)
        users_collection.create_index("last_active")

        idle()

if __name__ == "__main__":
    main()
