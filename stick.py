# -*- coding: utf-8 -*-
import os
import sys
import time
import re
import json
import uuid
import asyncio
import aiohttp
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, idle, StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
try:
    from pyrogram.enums import ParseMode
    HTML_PARSE_MODE = ParseMode.HTML
except ImportError:
    HTML_PARSE_MODE = "html"
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

# ==============================================
# LOAD ENVIRONMENT VARIABLES
# ==============================================
load_dotenv()

# ==============================================
# BOT CONFIGURATION FROM .env
# ==============================================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "sticker_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "users")

_admin_raw = os.getenv("ADMIN_USER_ID", "").strip()
ADMIN_IDS = []
for _piece in _admin_raw.split(","):
    _piece = _piece.strip()
    if _piece.isdigit():
        ADMIN_IDS.append(int(_piece))

ADMIN_USER_ID = ADMIN_IDS[0] if ADMIN_IDS else None

DEFAULT_EMOJI = os.getenv("DEFAULT_EMOJI", "✨")
TEMP_DIR = os.getenv("TEMP_DIR", "temp_stickers")

# ==============================================
# STARTUP SANITY CHECKS
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
    sys.exit(1)

API_ID = int(API_ID)

# ==============================================
# CREATE TEMP DIRECTORY
# ==============================================
os.makedirs(TEMP_DIR, exist_ok=True)

# ==============================================
# CONNECT TO MONGODB
# ==============================================
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
    mongo_client.admin.command("ping")
except Exception as e:
    print(f"❌ Could not connect to MongoDB: {e}")
    sys.exit(1)

db = mongo_client[DB_NAME]
users_collection = db[COLLECTION_NAME]

# ==============================================
# NEW: MULTI-PACK COLLECTION
# ==============================================
PACKS_COLLECTION_NAME = os.getenv("PACKS_COLLECTION_NAME", "sticker_packs")
packs_collection = db[PACKS_COLLECTION_NAME]

# ==============================================
# CREATE THE BOT CLIENT
# ==============================================
app = Client(
    "sticker_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Populated in main() from app.get_me(); used to build Telegram sticker set short names.
BOT_USERNAME = None

# ==============================================
# FORCE-SUBSCRIBE ("Must Join") SYSTEM
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
# NEW: GROUP FIRST-TIME START HELPER
# ==============================================
def has_started_bot(user_id):
    """Whether the user has ever pressed /start in a private chat with the bot."""
    user_data = get_user_data(user_id)
    return bool(user_data.get("started_bot", False))

# ==============================================
# NEW: MULTI-PACK STORAGE HELPERS
# ==============================================
def get_user_packs(user_id):
    """Get all sticker packs owned by a user, sorted by permanent pack_index"""
    return list(packs_collection.find({"user_id": str(user_id)}).sort("pack_index", 1))

def get_next_pack_index(user_id):
    """Next available pack index for a user. Indexes are never reused/renumbered."""
    last = packs_collection.find_one(
        {"user_id": str(user_id)},
        sort=[("pack_index", -1)]
    )
    return (last["pack_index"] + 1) if last else 1

def get_pack_by_index(user_id, pack_index):
    """Locate a pack ONLY by (user_id, pack_index) — never by name."""
    return packs_collection.find_one({"user_id": str(user_id), "pack_index": pack_index})

def create_pack_record(user_id, pack_index, display_title, telegram_pack_name, emoji):
    """Insert a new pack record. (user_id, pack_index) is unique and never overwritten."""
    packs_collection.insert_one({
        "user_id": str(user_id),
        "pack_index": pack_index,
        "display_title": display_title,
        "telegram_pack_name": telegram_pack_name,
        "emoji": emoji,
        "created_at": datetime.now(),
        "total_stickers": 1
    })

def increment_pack_sticker_count(user_id, pack_index):
    """Increment total stickers count for one specific pack"""
    packs_collection.update_one(
        {"user_id": str(user_id), "pack_index": pack_index},
        {"$inc": {"total_stickers": 1}}
    )

def generate_telegram_pack_name(display_title, bot_username):
    """Turn a user-facing display name into a valid Telegram sticker set short name."""
    base = display_title.strip().lower()
    base = re.sub(r'\s+', '_', base)
    base = re.sub(r'[^a-z0-9_]', '', base)
    base = base.strip('_')
    if not base:
        base = "pack"
    if base[0].isdigit():
        base = f"p_{base}"
    suffix = f"_by_{bot_username}"
    max_base_len = 64 - len(suffix)
    if max_base_len < 1:
        max_base_len = 20
    base = base[:max_base_len]
    return f"{base}{suffix}"

def delete_pack_record(user_id, pack_index):
    """Delete ONLY the database record for a pack. Indexes are never reused/renumbered."""
    packs_collection.delete_one({"user_id": str(user_id), "pack_index": pack_index})

def update_pack_display_title(user_id, pack_index, new_title):
    """Update only the display_title of a specific pack record."""
    packs_collection.update_one(
        {"user_id": str(user_id), "pack_index": pack_index},
        {"$set": {"display_title": new_title}}
    )

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

# ------------------------------------------------
# CHANGE #4: SUPPORT STATIC TELEGRAM STICKER INPUT
# Converts a downloaded static sticker (.webp) into PNG bytes, preserving
# transparency, so it can flow through the exact same create_sticker() /
# pack-creation pipeline as a normal uploaded photo.
# ------------------------------------------------
def convert_webp_to_png(image_bytes):
    """Convert webp sticker bytes into PNG bytes (RGBA, transparency preserved)."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

# ==============================================
# PYROGRAM STICKER SET HELPERS
# ==============================================
TELEGRAM_BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ------------------------------------------------
# FIX #1: STICKER_PNG_DIMENSIONS
# Telegram rejects PNGs whose dimensions don't fit its sticker rules.
# Every sticker byte string — whether freshly rendered or downloaded
# from another pack during /clonepack — is normalized here before upload.
# ------------------------------------------------
def normalize_sticker_png(image_bytes):
    """Regenerate any image as a Telegram-safe 512x512 transparent RGBA PNG."""
    img = Image.open(BytesIO(image_bytes))
    original_format = img.format
    original_mode = img.mode
    original_size = img.size
    print(f"[STICKER NORMALIZE] Original Format: {original_format}")
    print(f"[STICKER NORMALIZE] Original Mode: {original_mode}")
    print(f"[STICKER NORMALIZE] Original Size: {original_size}")

    img = img.convert("RGBA")

    w, h = img.size
    if w >= h:
        new_w = 512
        new_h = max(1, round(h * (512 / w)))
    else:
        new_h = 512
        new_w = max(1, round(w * (512 / h)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    offset_x = (512 - new_w) // 2
    offset_y = (512 - new_h) // 2
    canvas.paste(resized, (offset_x, offset_y), resized)

    print(f"[STICKER NORMALIZE] Final Mode: {canvas.mode}")
    print(f"[STICKER NORMALIZE] Final Size: {canvas.size}")

    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    print("[STICKER NORMALIZE] Upload started")
    return out.getvalue()

async def _bot_api_upload(method, form_fields, file_field_name, file_bytes, file_name, content_type="image/png"):
    """Send a multipart/form-data request to the official Telegram Bot API."""
    url = f"{TELEGRAM_BOT_API_BASE}/{method}"
    form = aiohttp.FormData()
    for key, value in form_fields.items():
        form.add_field(key, str(value))
    form.add_field(file_field_name, file_bytes, filename=file_name, content_type=content_type)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form) as resp:
            result = await resp.json()
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return result.get("result")

async def create_sticker_pack(client, user_id, pack_name, title, sticker_bytes, emoji):
    """Create a new sticker set on Telegram"""
    try:
        normalized_bytes = normalize_sticker_png(sticker_bytes)

        # CHANGE #1: visible Telegram title gets " • @<bot_username>" appended.
        # The pack short name (pack_name / URL slug) is untouched.
        username_suffix = f" • @{BOT_USERNAME}"
        max_title_len = 64 - len(username_suffix)
        if max_title_len < 1:
            max_title_len = 30
        visible_title = f"{title[:max_title_len]}{username_suffix}"

        stickers_payload = json.dumps([{
            "sticker": "attach://sticker_file",
            "format": "static",
            "emoji_list": [emoji]
        }])
        await _bot_api_upload(
            "createNewStickerSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "title": visible_title,
                "stickers": stickers_payload
            },
            "sticker_file",
            normalized_bytes,
            "sticker.png"
        )
        return True, f"https://t.me/addstickers/{pack_name}"
    except Exception as e:
        return False, str(e)

async def add_sticker_to_pack(client, user_id, pack_name, sticker_bytes, emoji):
    """Add a sticker to an existing pack"""
    try:
        normalized_bytes = normalize_sticker_png(sticker_bytes)
        sticker_payload = json.dumps({
            "sticker": "attach://sticker_file",
            "format": "static",
            "emoji_list": [emoji]
        })
        await _bot_api_upload(
            "addStickerToSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "sticker": sticker_payload
            },
            "sticker_file",
            normalized_bytes,
            "sticker.png"
        )
        return True, None
    except Exception as e:
        return False, str(e)

async def create_video_sticker_pack(client, user_id, pack_name, title, sticker_bytes, emoji):
    """Create a new video sticker set on Telegram"""
    try:
        username_suffix = f" • @{BOT_USERNAME}"
        max_title_len = 64 - len(username_suffix)
        if max_title_len < 1:
            max_title_len = 30
        visible_title = f"{title[:max_title_len]}{username_suffix}"

        stickers_payload = json.dumps([{
            "sticker": "attach://sticker_file",
            "format": "video",
            "emoji_list": [emoji]
        }])
        await _bot_api_upload(
            "createNewStickerSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "title": visible_title,
                "stickers": stickers_payload
            },
            "sticker_file",
            sticker_bytes,
            "sticker.webm",
            content_type="video/webm"
        )
        return True, f"https://t.me/addstickers/{pack_name}"
    except Exception as e:
        return False, str(e)

async def add_video_sticker_to_pack(client, user_id, pack_name, sticker_bytes, emoji):
    """Add a video sticker to an existing pack"""
    try:
        sticker_payload = json.dumps({
            "sticker": "attach://sticker_file",
            "format": "video",
            "emoji_list": [emoji]
        })
        await _bot_api_upload(
            "addStickerToSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "sticker": sticker_payload
            },
            "sticker_file",
            sticker_bytes,
            "sticker.webm",
            content_type="video/webm"
        )
        return True, None
    except Exception as e:
        return False, str(e)

async def create_unique_video_sticker_pack(client, user_id, display_title, sticker_bytes, emoji):
    """Create a video sticker pack, auto-retrying with a numeric suffix if the
    generated Telegram short name is already occupied globally."""
    base_name = generate_telegram_pack_name(display_title, BOT_USERNAME)
    candidate = base_name
    attempt = 0
    last_error = None
    while attempt <= MAX_PACK_NAME_RETRIES:
        success, result = await create_video_sticker_pack(client, user_id, candidate, display_title, sticker_bytes, emoji)
        if success:
            return True, result, candidate
        last_error = result
        if _pack_name_taken(result):
            attempt += 1
            candidate = _suffixed_pack_name(base_name, attempt, BOT_USERNAME)
            continue
        return False, result, candidate
    return False, last_error, candidate

# ------------------------------------------------
# FIX #2: UNIQUE TELEGRAM PACK NAME
# Wraps create_sticker_pack() with automatic retry + numeric suffix
# whenever the generated short name is already taken globally.
# ------------------------------------------------
MAX_PACK_NAME_RETRIES = 25

def _pack_name_taken(error_text):
    if not error_text:
        return False
    low = error_text.lower()
    return "occupied" in low or ("already" in low and "name" in low)

def _suffixed_pack_name(base_name, n, bot_username):
    suffix = f"_by_{bot_username}"
    core = base_name[:-len(suffix)] if base_name.endswith(suffix) else base_name
    tail = f"_{n}{suffix}"
    max_core_len = 64 - len(tail)
    if max_core_len < 1:
        max_core_len = 10
    core = core[:max_core_len]
    return f"{core}{tail}"

async def create_unique_sticker_pack(client, user_id, display_title, sticker_bytes, emoji):
    """Create a sticker pack, auto-retrying with a numeric suffix if the
    generated Telegram short name is already occupied globally.
    Returns (success, result_or_error, telegram_pack_name_used)."""
    base_name = generate_telegram_pack_name(display_title, BOT_USERNAME)
    candidate = base_name
    attempt = 0
    last_error = None
    while attempt <= MAX_PACK_NAME_RETRIES:
        success, result = await create_sticker_pack(client, user_id, candidate, display_title, sticker_bytes, emoji)
        if success:
            return True, result, candidate
        last_error = result
        if _pack_name_taken(result):
            attempt += 1
            candidate = _suffixed_pack_name(base_name, attempt, BOT_USERNAME)
            continue
        return False, result, candidate
    return False, last_error, candidate

async def set_sticker_set_title(pack_name, title):
    """Rename a sticker set's TITLE via the official Bot API (setStickerSetTitle).
    Note: Telegram's Bot API has no method to change a set's short name/slug —
    only its display title. If this fails or is unsupported, callers fall back
    to updating just the stored display_title in MongoDB."""
    url = f"{TELEGRAM_BOT_API_BASE}/setStickerSetTitle"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data={"name": pack_name, "title": title}) as resp:
            result = await resp.json()
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return True

# ==============================================
# NEW: /clonepack HELPERS
# ==============================================
async def get_sticker_set(short_name):
    """Fetch a sticker set via the official Bot API's getStickerSet method."""
    url = f"{TELEGRAM_BOT_API_BASE}/getStickerSet"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"name": short_name}) as resp:
            result = await resp.json()
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return result.get("result")

def extract_pack_short_name(raw):
    """Extract a Telegram sticker set short name from a link or bare name."""
    raw = raw.strip()
    m = re.match(r'^(?:https?://)?t\.me/addstickers/([A-Za-z0-9_]+)/?$', raw, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.match(r'^[A-Za-z0-9_]+$', raw):
        return raw
    return None

# ==============================================
# NEW: /plain & /vid VIDEO STICKER HELPERS
# ==============================================
async def convert_mp4_to_sticker_webm(input_path, output_path):
    """Convert MP4 to Telegram-compatible WEBM video sticker format.
    Scales to 512x512, trims to 3 seconds, VP9 codec, no audio.
    Returns (success, error_message_or_None)."""
    vf = "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black"
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(MAX_PREVIEW_SECONDS),
        "-vf", vf,
        "-c:v", "libvpx",
        "-b:v", "200k",
        "-maxrate", "250k",
        "-bufsize", "500k",
        "-an",
        "-deadline", "good",
        "-cpu-used", "4",
        "-auto-alt-ref", "0",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            err = stderr.decode(errors="ignore")[-500:] if stderr else "Unknown FFmpeg error"
            return False, err
        return True, None
    except FileNotFoundError:
        return False, "FFmpeg is not installed on the server."
    except Exception as e:
        return False, str(e)

async def normalize_video_sticker(input_path, output_path):
    """Normalize any video to Telegram sticker WEBM format (512x512, VP9, max 256KB, max 3s).
    Returns (success, error_message_or_None)."""
    vf = "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black"
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(MAX_PREVIEW_SECONDS),
        "-vf", vf,
        "-c:v", "libvpx",
        "-b:v", "200k",
        "-maxrate", "250k",
        "-bufsize", "500k",
        "-an",
        "-deadline", "good",
        "-cpu-used", "4",
        "-auto-alt-ref", "0",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            err = stderr.decode(errors="ignore")[-500:] if stderr else "Unknown FFmpeg error"
            return False, err
        file_size = os.path.getsize(output_path)
        if file_size > 256 * 1024:
            cmd_retry = [
                "ffmpeg", "-y",
                "-i", input_path,
                "-t", str(MAX_PREVIEW_SECONDS),
                "-vf", vf,
                "-c:v", "libvpx",
                "-b:v", "120k",
                "-maxrate", "150k",
                "-bufsize", "300k",
                "-an",
                "-deadline", "good",
                "-cpu-used", "4",
                "-auto-alt-ref", "0",
                output_path
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd_retry,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc2.communicate()
            if proc2.returncode != 0 or not os.path.exists(output_path):
                return False, "Failed to compress video to under 256KB."
        return True, None
    except FileNotFoundError:
        return False, "FFmpeg is not installed on the server."
    except Exception as e:
        return False, str(e)

def prepare_plain_image_sticker(image_bytes):
    """Prepare an image as a sticker without text overlay.
    Returns normalized 512x512 PNG bytes."""
    return normalize_sticker_png(image_bytes)

# ==============================================
# NEW: /vid PREVIEW VIDEO HELPERS
# Official Catbox upload API + FFmpeg processing for pack preview videos.
# No preview video is ever kept permanently on the VPS — every temp file
# created below is removed (see cleanup_temp_files) once it's either
# uploaded to Catbox or the flow is cancelled/fails.
# ==============================================
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
MAX_PREVIEW_SECONDS = 3.0
DURATION_TOLERANCE = 0.05  # small leeway for container/encoder rounding

def cleanup_temp_files(*paths):
    """Best-effort removal of one or more temp files. Never raises."""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

async def probe_video_duration(path):
    """Return a video's duration in seconds via ffprobe, or None if the
    file can't be probed (invalid/unsupported format)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode(errors="ignore"))
        return float(data.get("format", {}).get("duration"))
    except Exception:
        return None

def _escape_ffmpeg_text(text):
    """Escape characters that are special to FFmpeg's drawtext filter."""
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("%", "\\%")
    )

async def generate_vid_preview(input_path, output_path, text=None):
    """Resize the source video to fit a 512x512 canvas (aspect ratio
    preserved, padded), optionally overlay text, trim to a max of 3
    seconds, and export a Telegram-friendly optimized MP4.
    Returns (success, error_message_or_None)."""
    vf_parts = [
        "scale=512:512:force_original_aspect_ratio=decrease",
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black"
    ]
    if text:
        safe_text = _escape_ffmpeg_text(text)
        vf_parts.append(
            f"drawtext=text='{safe_text}':fontcolor=white:fontsize=48:"
            f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-th-30"
        )
    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(MAX_PREVIEW_SECONDS),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        output_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            err = stderr.decode(errors="ignore")[-500:] if stderr else "Unknown FFmpeg error"
            return False, err
        return True, None
    except FileNotFoundError:
        return False, "FFmpeg is not installed on the server."
    except Exception as e:
        return False, str(e)

async def upload_to_catbox(file_path):
    """Upload a file to Catbox via the official upload API and return the
    resulting public URL. Raises on failure."""
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("fileToUpload", file_bytes, filename=os.path.basename(file_path), content_type="video/mp4")

    async with aiohttp.ClientSession() as session:
        async with session.post(CATBOX_UPLOAD_URL, data=form) as resp:
            result_text = (await resp.text()).strip()

    if not result_text.startswith("http"):
        raise Exception(result_text or "Unknown Catbox error")
    return result_text

# ==============================================
# SESSION STORAGE
# ==============================================
user_sessions = {}  # {user_id: {"step": str, "image_bytes": bytes, "text": str, "size": float, "position": str, "sticker_bytes": bytes}}

# ==============================================
# NEW: /create WIZARD SESSION STORAGE
# Kept completely separate from user_sessions so it can never collide
# with an in-progress /sticker flow for the same admin.
# ==============================================
create_sessions = {}  # {user_id: {"step": str, "started_at": float, "chat_id": str, "username": str, "title": str, "request": bool, "button_text": str}}
generated_commands = {}  # {message_id: command_string} — used by the "📋 Copy Command" button fallback

# ==============================================
# NEW: /vid PREVIEW-VIDEO WIZARD SESSION STORAGE
# Kept completely separate from user_sessions and create_sessions so it
# can never collide with an in-progress /sticker flow or /create wizard
# for the same user.
# ==============================================
vid_sessions = {}  # {user_id: {"step": str, "pack_index": int, "video_path": str, "preview_path": str, "text": str|None, "started_at": float}}

# ==============================================
# NEW: /plain STICKER WIZARD SESSION STORAGE
# Kept completely separate from user_sessions, create_sessions, and
# vid_sessions so it can never collide with any other flow.
# ==============================================
plain_sessions = {}  # {user_id: {"step": str, "pack_index": int|None, "display_title": str|None, "media_bytes": bytes|None, "media_type": str, "media_path": str|None, "emoji": str, "sticker_bytes": bytes|None, "started_at": float}}

async def send_vid_message(client, chat_id, is_group, from_user, text, reply_markup=None):
    """Send a /vid-flow message from a callback context (where there is no
    incoming user Message to reply to directly)."""
    if is_group and from_user:
        mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
        text = f"{mention}\n\n{text}"
    return await client.send_message(chat_id, text, reply_markup=reply_markup)

# ==============================================
# NEW: COMMAND REGISTRY FOR /help AND /adminhelp
# Every public command registers itself here (right next to its own
# handler) instead of being hardcoded into the help text. /help and
# /adminhelp simply render whatever is currently in these structures, so
# any future command that calls register_user_command(...) or
# register_admin_command(...) automatically shows up — nothing else
# needs to be touched.
# ==============================================
from collections import OrderedDict

CATEGORY_STICKER = "📦 Sticker Commands"
CATEGORY_GENERAL = "ℹ️ General Commands"

USER_COMMAND_CATEGORIES = OrderedDict()  # {category_label: [(command, description), ...]}
ADMIN_COMMANDS = []  # [(command, description), ...]

def register_user_command(command, description, category=CATEGORY_GENERAL):
    """Add a public/user command to the /help registry. Call this once,
    at module load time, right after the handler it documents."""
    USER_COMMAND_CATEGORIES.setdefault(category, []).append((command, description))

def register_admin_command(command, description):
    """Add an admin-only command to the /adminhelp registry. Call this
    once, at module load time, right after the handler it documents."""
    ADMIN_COMMANDS.append((command, description))

# ==============================================
# HELPER: Reply to user in DM or Group
# ==============================================
async def reply_or_dm(client, message, text, reply_markup=None, parse_mode=None):
    """Reply to user in DM if private, or mention them in group"""
    if message.chat.type == "private":
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        mention = f"[{message.from_user.first_name}](tg://user?id={message.from_user.id})"
        return await message.reply_text(f"{mention}\n\n{text}", reply_markup=reply_markup, parse_mode=parse_mode)

# ==============================================
# NEW: GROUP FIRST-TIME START GATE
# Runs before every command handler (early group=-1). Blocks command
# execution in groups until the user has pressed /start in DM once.
# ==============================================
@app.on_message(filters.group & filters.text & filters.regex(r'^/\w'), group=-1)
async def group_start_gate(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if has_started_bot(uid):
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start in DM", url=f"https://t.me/{BOT_USERNAME}?start=start")]
    ])
    await message.reply_text(
        small_caps("👋 You need to start me in private first.\nPress the button below."),
        reply_markup=kb
    )
    raise StopPropagation

# ==============================================
# /start
# ==============================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    get_user_data(uid)
    update_user_data(uid, {"started_bot": True})
    await message.reply_text(
        small_caps(
            "🌟 **Sticker Maker Bot** 🌟\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Welcome! I can turn your images into high-quality Telegram stickers.\n\n"

            "🖼 **/sticker**\n"
            "Create a sticker from any image.\n\n"

            "📦 **/mypacks**\n"
            "Manage and view your sticker packs.\n\n"

            "📋 **/help**\n"
            "View all available commands.\n\n"

            "😊 **/setemoji <emoji>**\n"
            "Choose the default emoji for new stickers.\n\n"

            "👥 **/users**\n"
            "View total bot users. *(Admin Only)*\n\n"

            "🚀 Just send me an image to begin!"
    )
)
register_user_command("start", "Start the bot and see the welcome message.", category=CATEGORY_GENERAL)

# ==============================================
# COMMAND: /sticker - Works in DM and Groups
# ==============================================
@app.on_message(filters.command("sticker") & (filters.private | filters.group))
async def sticker_start(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "waiting_image"}
    await reply_or_dm(client, message, small_caps("📸 **Step 1/4:** Send me an image (photo or document)."))
register_user_command("sticker", "Create a sticker from any image.", category=CATEGORY_STICKER)

# ==============================================
# NEW COMMAND: /plain - Works in DM and Groups
# Creates stickers WITHOUT text overlay. Supports:
# Photo, PNG, WEBP Sticker, Static Sticker, MP4 Video, WEBM Video Sticker.
# ==============================================
@app.on_message(filters.command("plain") & (filters.private | filters.group))
async def plain_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    plain_sessions[uid] = {"step": "plain_waiting_destination", "started_at": time.time()}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 New Pack", callback_data="plaindest_new")],
        [InlineKeyboardButton("📦 Existing Pack", callback_data="plaindest_existing")],
        [InlineKeyboardButton("❌ Cancel", callback_data="plaindest_cancel")]
    ])
    await reply_or_dm(client, message, small_caps("📦 Where do you want to save this sticker?"), reply_markup=kb)
register_user_command("plain", "Create a sticker without text overlay.", category=CATEGORY_STICKER)

# ==============================================
# NEW COMMAND: /clonepack - Works in DM and Groups
# ==============================================
@app.on_message(filters.command("clonepack") & (filters.private | filters.group))
async def clonepack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "clonepack_waiting_link"}
    await reply_or_dm(
        client, message,
        small_caps("📦 Send the Telegram Sticker Pack link you want to clone.\n\nExample:\nhttps://t.me/addstickers/NarutoPack")
    )
register_user_command("clonepack", "Clone an existing Telegram sticker pack.", category=CATEGORY_STICKER)

# ==============================================
# NEW COMMAND: /renamepack - Works in DM and Groups
# ==============================================
@app.on_message(filters.command("renamepack") & (filters.private | filters.group))
async def renamepack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "renamepack_waiting_index"}
    await reply_or_dm(client, message, small_caps("📦 Send the Pack Index.\n\nExample:\n1"))
register_user_command("renamepack", "Rename one of your sticker packs.", category=CATEGORY_STICKER)

# ==============================================
# NEW COMMAND: /delpack - Works in DM and Groups
# ==============================================
@app.on_message(filters.command("delpack") & (filters.private | filters.group))
async def delpack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "delpack_waiting_index"}
    await reply_or_dm(client, message, small_caps("📦 Send the Pack Index you want to delete.\n\nExample:\n2"))
register_user_command("delpack", "Delete one of your sticker packs from the database.", category=CATEGORY_STICKER)

# ==============================================
# HANDLE IMAGE - Works in DM and Groups
# ==============================================
@app.on_message((filters.photo | filters.document | filters.sticker) & (filters.private | filters.group), group=5)
async def handle_image(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_image":
        return

    is_sticker_input = False
    file_id = None
    if message.sticker:
        if message.sticker.is_animated or message.sticker.is_video:
            await reply_or_dm(client, message, small_caps("❌ Animated and Video stickers are currently not supported."))
            return
        file_id = message.sticker.file_id
        is_sticker_input = True
    elif message.photo:
        file_id = message.photo.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid image file."))
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading image..."))
    try:
        file = await client.download_media(file_id, in_memory=True)
        img_bytes = file.getvalue()
        if is_sticker_input:
            img_bytes = convert_webp_to_png(img_bytes)
        user_sessions[uid]["image_bytes"] = img_bytes
        user_sessions[uid]["step"] = "waiting_text"
        await proc.edit_text(small_caps("✏️ **Step 2/4:** Send me the text you want on the sticker."))
    except Exception as e:
        await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))

# ==============================================
# HANDLE PLAIN MEDIA - Works in DM and Groups
# Handles photo, PNG, WEBP sticker, static sticker, MP4 video, WEBM
# video sticker for the /plain flow.
# ==============================================
@app.on_message((filters.photo | filters.document | filters.sticker | filters.video) & (filters.private | filters.group), group=10)
async def handle_plain_media(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_media":
        return

    session = plain_sessions[uid]
    pack_index = session.get("pack_index")
    if pack_index is None:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /plain."))
        plain_sessions.pop(uid, None)
        return

    pack = get_pack_by_index(uid, pack_index)
    existing_pack_type = pack.get("pack_type", "static") if pack else "static"

    is_video_input = False
    is_sticker_input = False
    file_id = None
    media_path = None

    if message.sticker:
        if message.sticker.is_animated:
            await reply_or_dm(client, message, small_caps("❌ Animated stickers are not supported."))
            return
        if message.sticker.is_video:
            is_video_input = True
            file_id = message.sticker.file_id
        else:
            is_sticker_input = True
            file_id = message.sticker.file_id
    elif message.video:
        is_video_input = True
        file_id = message.video.file_id
    elif message.photo:
        file_id = message.photo.file_id
    elif message.document and message.document.mime_type:
        mime = message.document.mime_type.lower()
        if mime.startswith("video/"):
            is_video_input = True
            file_id = message.document.file_id
        elif mime.startswith("image/"):
            file_id = message.document.file_id
        else:
            await reply_or_dm(client, message, small_caps("❌ Unsupported file type. Send a photo, PNG, WEBP sticker, static sticker, MP4 video, or WEBM video sticker."))
            return
    else:
        await reply_or_dm(client, message, small_caps("❌ Unsupported media. Send a photo, PNG, WEBP sticker, static sticker, MP4 video, or WEBM video sticker."))
        return

    if existing_pack_type == "static" and is_video_input:
        await reply_or_dm(client, message, small_caps("❌ This is a static sticker pack. Video stickers cannot be added. Use /plain with an image instead."))
        return
    if existing_pack_type == "video" and not is_video_input:
        await reply_or_dm(client, message, small_caps("❌ This is a video sticker pack. Static stickers cannot be added. Use /plain with a video instead."))
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading and processing media..."))

    try:
        if is_video_input:
            media_path = os.path.join(TEMP_DIR, f"plain_vid_{uid}_{uuid.uuid4().hex}.webm")
            temp_input = os.path.join(TEMP_DIR, f"plain_vid_in_{uid}_{uuid.uuid4().hex}.mp4")
            await client.download_media(message, file_name=temp_input)

            file_name_lower = (getattr(message.video or message.document, "file_name", "") or "").lower()
            mime_lower = (getattr(message.video or message.document, "mime_type", "") or "").lower()

            if file_name_lower.endswith(".webm") or "webm" in mime_lower:
                if message.sticker and message.sticker.is_video:
                    duration = await probe_video_duration(temp_input)
                    if duration and duration <= MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
                        sticker_size = os.path.getsize(temp_input)
                        if sticker_size <= 256 * 1024:
                            os.rename(temp_input, media_path)
                            session["media_path"] = media_path
                            session["media_type"] = "video"
                            session["step"] = "plain_waiting_emoji"
                            cleanup_temp_files()
                            await proc.edit_text(
                                small_caps("😀 Send an emoji for this sticker."),
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("😀 Default", callback_data="plainemoji_default"),
                                     InlineKeyboardButton("✏️ Send Emoji", callback_data="plainemoji_send")]
                                ])
                            )
                            return

                ok, err = await normalize_video_sticker(temp_input, media_path)
                cleanup_temp_files(temp_input)
                if not ok:
                    await proc.edit_text(small_caps(f"❌ Failed to process video: {err}"))
                    return
            else:
                duration = await probe_video_duration(temp_input)
                if duration is None:
                    cleanup_temp_files(temp_input)
                    await proc.edit_text(small_caps("❌ Invalid or unsupported video format."))
                    return
                if duration > MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
                    cleanup_temp_files(temp_input)
                    await proc.edit_text(small_caps(f"❌ Video is too long ({duration:.1f}s). Maximum allowed is 3 seconds."))
                    return

                ok, err = await normalize_video_sticker(temp_input, media_path)
                cleanup_temp_files(temp_input)
                if not ok:
                    await proc.edit_text(small_caps(f"❌ Failed to convert video: {err}"))
                    return

            session["media_path"] = media_path
            session["media_type"] = "video"
        else:
            img_file = await client.download_media(file_id, in_memory=True)
            img_bytes = img_file.getvalue()
            if is_sticker_input:
                img_bytes = convert_webp_to_png(img_bytes)
            sticker_bytes = prepare_plain_image_sticker(img_bytes)
            session["media_bytes"] = sticker_bytes
            session["media_type"] = "static"

        session["step"] = "plain_waiting_emoji"
        await proc.edit_text(
            small_caps("😀 Send an emoji for this sticker."),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("😀 Default", callback_data="plainemoji_default"),
                 InlineKeyboardButton("✏️ Send Emoji", callback_data="plainemoji_send")]
            ])
        )
    except Exception as e:
        cleanup_temp_files(
            plain_sessions.get(uid, {}).get("media_path"),
        )
        await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))
        plain_sessions.pop(uid, None)
# (called from handle_text below, not decorated themselves)
# ==============================================
async def handle_new_pack_name(client: Client, message: Message, uid):
    display_title = message.text.strip()
    if not display_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
        return

    sticker_bytes = user_sessions.get(uid, {}).get("sticker_bytes")
    if not sticker_bytes:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /sticker."))
        user_sessions.pop(uid, None)
        return

    emoji = get_user_pack(uid)["emoji"]
    pack_index = get_next_pack_index(uid)

    proc = await reply_or_dm(client, message, small_caps("⚙️ Creating your new pack..."))

    success, result, telegram_pack_name = await create_unique_sticker_pack(client, uid, display_title, sticker_bytes, emoji)
    if success:
        create_pack_record(uid, pack_index, display_title, telegram_pack_name, emoji)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🌟 Open Sticker Pack",
                    url=f"https://t.me/addstickers/{telegram_pack_name}"
                )
            ]
        ])
        await proc.edit_text(
            small_caps(f"✅ Pack Created Successfully!\n📦 Pack Index: {pack_index}\n📝 Name: {display_title}"),
            reply_markup=kb
        )
    else:
        await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))

    user_sessions.pop(uid, None)

async def handle_pack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    sticker_bytes = user_sessions.get(uid, {}).get("sticker_bytes")
    if not sticker_bytes:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /sticker."))
        user_sessions.pop(uid, None)
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Adding sticker to pack..."))

    success, err = await add_sticker_to_pack(client, uid, pack["telegram_pack_name"], sticker_bytes, pack["emoji"])
    if success:
        increment_pack_sticker_count(uid, pack_index)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🌟 Open Sticker Pack",
                    url=f"https://t.me/addstickers/{pack['telegram_pack_name']}"
                )
            ]
        ])
        await proc.edit_text(
            small_caps(f"✅ Sticker Added Successfully!\n📦 Pack Index: {pack_index}\n📝 Pack Name: {pack['display_title']}"),
            reply_markup=kb
        )
    else:
        await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))

    user_sessions.pop(uid, None)

# ==============================================
# NEW: /clonepack STEP HANDLERS
# (called from handle_text below, not decorated themselves)
# ==============================================
async def handle_clonepack_link(client: Client, message: Message, uid):
    raw = message.text.strip()
    short_name = extract_pack_short_name(raw)
    if not short_name:
        await reply_or_dm(client, message, small_caps("❌ Invalid sticker pack link."))
        return

    try:
        sticker_set = await get_sticker_set(short_name)
    except Exception:
        await reply_or_dm(client, message, small_caps("❌ Unable to access this sticker pack."))
        return

    if not sticker_set.get("stickers"):
        await reply_or_dm(client, message, small_caps("❌ Unable to access this sticker pack."))
        return

    user_sessions[uid]["source_pack"] = sticker_set
    user_sessions[uid]["step"] = "clonepack_waiting_name"
    await reply_or_dm(
        client, message,
        small_caps("✏️ Enter a name for your cloned sticker pack.\n\nExample:\nNaruto Collection")
    )

async def handle_clonepack_name(client: Client, message: Message, uid):
    display_title = message.text.strip()
    if not display_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
        return

    source_pack = user_sessions.get(uid, {}).get("source_pack")
    if not source_pack:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /clonepack."))
        user_sessions.pop(uid, None)
        return

    stickers = source_pack.get("stickers", [])
    default_emoji = get_user_pack(uid)["emoji"]
    pack_index = get_next_pack_index(uid)
    total = len(stickers)

    progress = await reply_or_dm(
        client, message,
        small_caps(f"⚙️ Cloning Sticker Pack...\n\n0 / {total} copied...")
    )

    copied = 0
    skipped = 0
    failed = 0

    # Find the first static sticker to seed the new pack (createNewStickerSet needs one).
    first_sticker_bytes = None
    first_index = None
    for i, st in enumerate(stickers):
        if st.get("is_animated") or st.get("is_video"):
            continue
        try:
            downloaded = await client.download_media(st["file_id"], in_memory=True)
            first_sticker_bytes = downloaded.getvalue()
            first_index = i
            break
        except Exception:
            continue

    if first_sticker_bytes is None:
        await progress.edit_text(small_caps("❌ Unable to access this sticker pack."))
        user_sessions.pop(uid, None)
        return

    first_emoji = stickers[first_index].get("emoji") or default_emoji
    success, result, telegram_pack_name = await create_unique_sticker_pack(client, uid, display_title, first_sticker_bytes, first_emoji)
    if not success:
        await progress.edit_text(small_caps(f"❌ Unable to create the cloned sticker pack: {result}"))
        user_sessions.pop(uid, None)
        return

    create_pack_record(uid, pack_index, display_title, telegram_pack_name, first_emoji)
    copied += 1

    for i, st in enumerate(stickers):
        if i == first_index:
            continue

        if st.get("is_animated") or st.get("is_video"):
            skipped += 1
        else:
            try:
                downloaded = await client.download_media(st["file_id"], in_memory=True)
                sticker_bytes = downloaded.getvalue()
            except Exception:
                sticker_bytes = None

            if sticker_bytes is None:
                failed += 1
            else:
                sticker_emoji = st.get("emoji") or default_emoji
                add_success, _add_err = await add_sticker_to_pack(client, uid, telegram_pack_name, sticker_bytes, sticker_emoji)
                if add_success:
                    increment_pack_sticker_count(uid, pack_index)
                    copied += 1
                else:
                    failed += 1

        done = copied + skipped + failed
        if done % 5 == 0 or done == total:
            try:
                await progress.edit_text(small_caps(f"⚙️ Cloning Sticker Pack...\n\n{done} / {total} copied..."))
            except Exception:
                pass

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🌟 Open Sticker Pack",
                url=f"https://t.me/addstickers/{telegram_pack_name}"
            )
        ]
    ])
    await progress.edit_text(
        small_caps(
            f"✅ Sticker Pack Cloned Successfully!\n"
            f"📦 Pack Index: {pack_index}\n"
            f"📝 Name: {display_title}\n"
            f"📊 Stickers Copied: {copied}\n\n"
            f"Copied: {copied}\n"
            f"Skipped: {skipped}\n"
            f"Failed: {failed}"
        ),
        reply_markup=kb
    )

    user_sessions.pop(uid, None)

# ==============================================
# NEW: /renamepack STEP HANDLERS
# (called from handle_text below, not decorated themselves)
# ==============================================
async def handle_renamepack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    user_sessions[uid]["rename_pack_index"] = pack_index
    user_sessions[uid]["step"] = "renamepack_waiting_name"
    await reply_or_dm(client, message, small_caps("✏️ Enter the new display name.\n\nExample:\nAnime Collection"))

async def handle_renamepack_name(client: Client, message: Message, uid):
    new_title = message.text.strip()
    if not new_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid name."))
        return

    pack_index = user_sessions.get(uid, {}).get("rename_pack_index")
    pack = get_pack_by_index(uid, pack_index) if pack_index is not None else None
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /renamepack."))
        user_sessions.pop(uid, None)
        return

    # Telegram's Bot API has no method to change a sticker set's short name/slug —
    # only its title (via setStickerSetTitle). We try that first; if it's rejected
    # or unsupported, we fall back to updating only the stored display name.
    try:
        await set_sticker_set_title(pack["telegram_pack_name"], new_title)
        update_pack_display_title(uid, pack_index, new_title)
        await reply_or_dm(
            client, message,
            small_caps(f"✅ Pack renamed!\n📦 Pack Index: {pack_index}\n📝 New Name: {new_title}")
        )
    except Exception:
        update_pack_display_title(uid, pack_index, new_title)
        await reply_or_dm(
            client, message,
            small_caps(
                f"✅ Display name updated!\n📦 Pack Index: {pack_index}\n📝 New Name: {new_title}\n\n"
                f"ℹ️ Telegram does not allow renaming a sticker pack's link, so only the name shown in /mypacks was updated."
            )
        )

    user_sessions.pop(uid, None)

# ==============================================
# NEW: /delpack STEP HANDLER
# (called from handle_text below, not decorated itself)
# ==============================================
async def handle_delpack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    user_sessions[uid]["delete_pack_index"] = pack_index
    user_sessions[uid]["step"] = "delpack_confirming"

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Delete", callback_data="delpack_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="delpack_cancel")
        ]
    ])
    await reply_or_dm(
        client, message,
        small_caps(f"⚠️ Delete Pack Index {pack_index} ({pack['display_title']}) from the database?"),
        reply_markup=kb
    )

# ==============================================
# HANDLE TEXT - Works in DM and Groups
# Routes by session step: waiting_text (original flow), the two
# multi-pack steps (waiting_new_pack_name / waiting_pack_index), the two
# /clonepack steps, and the new /renamepack / /delpack steps.
# ==============================================
@app.on_message(
    filters.text & (filters.private | filters.group) &
    ~filters.command(["sticker", "setemoji", "mypacks", "stats", "users", "reset", "start", "addchannel", "removechannel", "channels", "clonepack", "renamepack", "delpack", "help", "adminhelp", "create", "cancel", "vid", "plain"]),
    group=6
)
async def handle_text(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id

    # Plain session text routing
    if uid in plain_sessions:
        step = plain_sessions[uid].get("step")
        if step == "plain_waiting_pack_name":
            await handle_plain_new_pack_name(client, message, uid)
            return
        if step in ("plain_waiting_emoji", "plain_waiting_emoji_text"):
            await handle_plain_emoji_input(client, message, uid)
            return

    if uid not in user_sessions:
        return

    step = user_sessions[uid].get("step")

    if step == "waiting_text":
        text = message.text.strip()
        if not text:
            await reply_or_dm(client, message, small_caps("❌ Please send some text."))
            return

        user_sessions[uid]["text"] = text
        user_sessions[uid]["step"] = "waiting_size"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔹 Sᴍᴀʟʟ", callback_data="size_small"),
             InlineKeyboardButton("🔸 Mᴇᴅɪᴜᴍ", callback_data="size_medium")],
            [InlineKeyboardButton("🔹 Lᴀʀɢᴇ", callback_data="size_large"),
             InlineKeyboardButton("🔸 Exᴛʀᴀ Lᴀʀɢᴇ", callback_data="size_xlarge")]
        ])
        await reply_or_dm(client, message, small_caps("🔧 **Step 3/4:** Choose text size:"), reply_markup=kb)
        return

    if step == "waiting_new_pack_name":
        await handle_new_pack_name(client, message, uid)
        return

    if step == "waiting_pack_index":
        await handle_pack_index(client, message, uid)
        return

    if step == "clonepack_waiting_link":
        await handle_clonepack_link(client, message, uid)
        return

    if step == "clonepack_waiting_name":
        await handle_clonepack_name(client, message, uid)
        return

    if step == "renamepack_waiting_index":
        await handle_renamepack_index(client, message, uid)
        return

    if step == "renamepack_waiting_name":
        await handle_renamepack_name(client, message, uid)
        return

    if step == "delpack_waiting_index":
        await handle_delpack_index(client, message, uid)
        return

# ==============================================
# NEW: STEP HANDLERS FOR /plain FLOW
# (called from handle_text above, not decorated themselves)
# ==============================================
async def handle_plain_new_pack_name(client: Client, message: Message, uid):
    display_title = message.text.strip()
    if not display_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
        return

    session = plain_sessions.get(uid)
    if not session:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /plain."))
        plain_sessions.pop(uid, None)
        return

    pack_index = get_next_pack_index(uid)
    session["display_title"] = display_title
    session["pack_index"] = pack_index
    session["step"] = "plain_waiting_media"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
    ])
    await reply_or_dm(
        client, message,
        small_caps(
            "📸 Send me the media now.\n\n"
            "Supported:\n"
            "• Photo\n• PNG\n• WEBP Sticker\n• Static Sticker\n"
            "• MP4 Video\n• WEBM Video Sticker"
        ),
        reply_markup=kb
    )

async def handle_plain_emoji_input(client: Client, message: Message, uid):
    emoji = message.text.strip()
    if not emoji:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid emoji."))
        return

    session = plain_sessions.get(uid)
    if not session:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /plain."))
        plain_sessions.pop(uid, None)
        return

    session["emoji"] = emoji
    session["step"] = "plain_preview"
    await send_plain_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")

# ==============================================
# NEW: HELPER TO SEND /plain PREVIEW
# ==============================================
async def send_plain_preview(client, chat_id, from_user, uid, is_group=False):
    """Generate and send the plain sticker preview with Add/Replace/Cancel buttons."""
    session = plain_sessions.get(uid)
    if not session:
        return

    media_type = session.get("media_type")
    emoji = session.get("emoji", "😀")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Add To Pack", callback_data="plainpreview_save"),
         InlineKeyboardButton("🔄 Replace Media", callback_data="plainpreview_replace")],
        [InlineKeyboardButton("❌ Cancel", callback_data="plainpreview_cancel")]
    ])

    if is_group and from_user:
        mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
        caption = f"{mention}\n\n{small_caps(f'🎨 Preview ({emoji})')}"
    else:
        caption = small_caps(f"🎨 Preview ({emoji})")

    if media_type == "static":
        sticker_bytes = session.get("sticker_bytes")
        if not sticker_bytes:
            return
        photo_file = BytesIO(sticker_bytes)
        photo_file.name = "preview.png"
        await client.send_photo(chat_id=chat_id, photo=photo_file, caption=caption, reply_markup=kb)
    else:
        media_path = session.get("media_path")
        if not media_path or not os.path.exists(media_path):
            return
        await client.send_video(chat_id=chat_id, video=media_path, caption=caption, reply_markup=kb)

# ==============================================
# SIZE CALLBACK
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
# NEW: HELPER TO SEND THE "EDIT AGAIN / SKIP" PREVIEW STEP
# Sends the current rendered sticker as a photo along with the two
# inline buttons. Kept modular so position_callback and (in future) any
# other re-render path can reuse it without duplicating logic.
# ==============================================
async def send_edit_again_preview(client: Client, chat_id: int, from_user, sticker_bytes, is_group: bool):
    caption = small_caps(
        "📝 Text added successfully.\n"
        "If you want to add more text (for example, text at the top and bottom), press Edit Again.\n"
        "Otherwise press Skip to continue."
    )
    if is_group:
        mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
        caption = f"{mention}\n\n{caption}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Edit Again", callback_data="editagain_edit"),
         InlineKeyboardButton("✅ Skip", callback_data="editagain_skip")]
    ])

    photo_file = BytesIO(sticker_bytes)
    photo_file.name = "preview.png"
    await client.send_photo(
        chat_id=chat_id,
        photo=photo_file,
        caption=caption,
        reply_markup=kb
    )

# ==============================================
# POSITION CALLBACK -> RENDER STICKER -> NEW: ASK "EDIT AGAIN / SKIP"
# (previously this jumped straight to the destination-choice step;
# now it renders a preview and offers to add another text layer first)
# ==============================================
@app.on_callback_query(filters.regex(r"^pos_"))
async def position_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_position":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    position = callback.data.replace("pos_", "")
    user_sessions[uid]["position"] = position

    await callback.answer(small_caps("⚙️ Rendering sticker..."))

    img_bytes = user_sessions[uid]["image_bytes"]
    text = user_sessions[uid]["text"]
    size_mult = user_sessions[uid]["size"]

    try:
        sticker_bytes = create_sticker(img_bytes, text, position, size_mult)
        user_sessions[uid]["sticker_bytes"] = sticker_bytes
        user_sessions[uid]["step"] = "preview_edit_again"

        try:
            await callback.message.delete()
        except Exception:
            pass

        await send_edit_again_preview(
            client,
            callback.message.chat.id,
            callback.from_user,
            sticker_bytes,
            callback.message.chat.type != "private"
        )
    except Exception as e:
        await callback.message.reply_text(small_caps(f"❌ Error: {str(e)}"))
        user_sessions.pop(uid, None)

# ==============================================
# NEW: "EDIT AGAIN / SKIP" CALLBACK
# Edit Again -> loops back to the text step, using the CURRENT rendered
# preview as the new base image so every previous text layer is preserved.
# Skip -> continues to the destination-choice step exactly as before.
# ==============================================
@app.on_callback_query(filters.regex(r"^editagain_"))
async def edit_again_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "preview_edit_again":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    choice = callback.data.replace("editagain_", "")
    await callback.answer()

    if choice == "edit":
        # The next render should draw on top of the current preview, not the
        # original image, so previously added text layers are kept.
        user_sessions[uid]["image_bytes"] = user_sessions[uid]["sticker_bytes"]
        user_sessions[uid]["step"] = "waiting_text"
        await callback.message.reply_text(
            small_caps("✏️ Send me the text you want to add on the sticker.")
        )
    else:
        user_sessions[uid]["step"] = "choose_destination"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Create New Pack", callback_data="dest_new")],
            [InlineKeyboardButton("📂 Existing Pack", callback_data="dest_existing")]
        ])
        await callback.message.reply_text(
            small_caps("📦 Where do you want to save this sticker?"),
            reply_markup=kb
        )

# ==============================================
# NEW: DESTINATION CALLBACK (new pack vs existing pack)
# ==============================================
@app.on_callback_query(filters.regex(r"^dest_"))
async def destination_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "choose_destination":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    choice = callback.data.replace("dest_", "")
    await callback.answer()

    if choice == "new":
        user_sessions[uid]["step"] = "waiting_new_pack_name"
        await callback.message.edit_text(
            small_caps("✏️ Enter the display name for your new sticker pack.\n\nExample:\nAnime Pack")
        )
    else:
        user_sessions[uid]["step"] = "waiting_pack_index"
        await callback.message.edit_text(
            small_caps("📂 Send the Pack Index where you want to save this sticker.\n\nExample:\n1")
        )

# ==============================================
# NEW: /delpack CONFIRMATION CALLBACK
# ==============================================
@app.on_callback_query(filters.regex(r"^delpack_"))
async def delpack_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "delpack_confirming":
        await callback.answer(small_caps("Session expired. Start over with /delpack."), show_alert=True)
        return

    choice = callback.data.replace("delpack_", "")
    pack_index = user_sessions[uid].get("delete_pack_index")
    await callback.answer()

    if choice == "cancel":
        await callback.message.edit_text(small_caps("❌ Cancelled."))
        user_sessions.pop(uid, None)
        return

    delete_pack_record(uid, pack_index)
    await callback.message.edit_text(
        small_caps(
            f"✅ Pack Index {pack_index} deleted from the database.\n\n"
            f"ℹ️ Telegram's Bot API does not support deleting sticker sets, so the pack itself still exists on Telegram — it's just no longer tracked here, and its index will never be reused."
        )
    )
    user_sessions.pop(uid, None)

# ==============================================
# /setemoji – Works in DM and Groups
# ==============================================
@app.on_message(filters.command("setemoji") & (filters.private | filters.group))
async def set_emoji_cmd(client: Client, message: Message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await reply_or_dm(client, message, small_caps("❌ Usage: /setemoji 😀"))
        return
    emoji = args[1].strip()
    if not emoji:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid emoji."))
        return

    get_user_data(uid)
    update_user_data(uid, {"emoji": emoji})
    await reply_or_dm(client, message, small_caps(f"✅ Default emoji set to {emoji}"))
register_user_command("setemoji", "Choose the default emoji for new stickers.", category=CATEGORY_STICKER)

# ==============================================
# /mypacks – Works in DM and Groups
# Updated to list every pack the user owns, indexed permanently.
# ==============================================
@app.on_message(filters.command("mypacks") & (filters.private | filters.group))
async def mypacks_cmd(client: Client, message: Message):
    uid = message.from_user.id
    packs = get_user_packs(uid)

    if not packs:
        await reply_or_dm(client, message, small_caps("You have no sticker packs yet. Create one with /sticker."))
        return

    blocks = [small_caps("📦 Your Sticker Packs")]
    for pack in packs:
        telegram_name = pack.get("telegram_pack_name")
        if telegram_name:
            link_line = f"🔗 https://t.me/addstickers/{telegram_name}\n"
        else:
            link_line = small_caps("🔗 No Telegram Pack Yet") + "\n"
        preview_line = ""
        if pack.get("preview_video_url"):
            preview_line = small_caps("🎬 Preview Available") + "\n"
        else:
            preview_line = small_caps("❌ No Preview") + "\n"
        blocks.append(
            small_caps(f"[{pack['pack_index']}] {pack['display_title']}") + "\n"
            + link_line
            + small_caps(f"🎨 Stickers: {pack.get('total_stickers', 0)}") + "\n"
            + preview_line
        )

    await reply_or_dm(client, message, "\n\n".join(blocks))
register_user_command("mypacks", "View and manage your sticker packs.", category=CATEGORY_STICKER)

# ==============================================
# /stats – show bot statistics (admin only)
# ==============================================
@app.on_message(filters.command("stats") & filters.user(ADMIN_IDS))
async def stats_cmd(client: Client, message: Message):
    stats = get_stats()

    total_packs = packs_collection.count_documents({})

    top_user = users_collection.find_one(
        sort=[("total_stickers", -1)]
    )

    if top_user:
        try:
            tg = await client.get_users(int(top_user["user_id"]))
            top_name = tg.first_name
            top_username = (
                f"@{tg.username}" if tg.username else "No Username"
            )
        except:
            top_name = "Unknown"
            top_username = "Unknown"

        top_stickers = top_user.get("total_stickers", 0)
    else:
        top_name = "-"
        top_username = "-"
        top_stickers = 0

    text = (
        "📊 **BOT STATISTICS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "👥 **Users**\n"
        f"├ Total Users : **{stats['total_users']}**\n"
        f"└ Active Database : **{stats['total_users']}**\n\n"

        "🎨 **Sticker Data**\n"
        f"├ Total Stickers : **{stats['total_stickers']}**\n"
        f"└ Total Packs : **{total_packs}**\n\n"

        "🏆 **Top Sticker Creator**\n"
        f"├ Name : **{top_name}**\n"
        f"├ Username : {top_username}\n"
        f"└ Stickers : **{top_stickers}**\n\n"

        "🤖 **Bot Status**\n"
        "└ 🟢 Online"
    )

    await message.reply_text(
        text,
        disable_web_page_preview=True
    )
register_admin_command("stats", "View bot statistics.")

# ==============================================
# /users – list all users (admin only)
# ==============================================
@app.on_message(filters.command("users") & filters.user(ADMIN_IDS))
async def users_cmd(client: Client, message: Message):
    users = sorted(
        get_all_users(),
        key=lambda x: x.get("total_stickers", 0),
        reverse=True
    )

    if not users:
        await message.reply_text("❌ No users found.")
        return

    text = (
        "🏆 **TOP 10 STICKER MAKERS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 **Total Users:** {len(users)}\n\n"
    )

    medals = ["🥇", "🥈", "🥉"]

    for i, user in enumerate(users[:10], start=1):
        uid = int(user["user_id"])

        try:
            tg = await client.get_users(uid)
            name = tg.first_name or "Unknown"
            username = f"@{tg.username}" if tg.username else "No Username"
        except:
            name = "Unknown User"
            username = "Unavailable"

        stickers = user.get("total_stickers", 0)
        packs = packs_collection.count_documents(
            {"user_id": str(uid)}
        )

        rank = medals[i-1] if i <= 3 else f"#{i}"

        text += (
            f"{rank} **{name}**\n"
            f"├ 👤 {username}\n"
            f"├ 🆔 `{uid}`\n"
            f"├ 📦 Packs : {packs}\n"
            f"└ 🎨 Stickers : **{stickers}**\n\n"
        )

    await message.reply_text(
        text,
        disable_web_page_preview=True
    )
register_admin_command("users", "View top sticker makers and all bot users.")

# ==============================================
# /reset – reset user's sticker pack (dangerous)
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
register_user_command("reset", "Reset your sticker pack data.", category=CATEGORY_STICKER)

# ==============================================
# /addchannel – Add a channel to force-subscribe list (admin only)
# ==============================================
@app.on_message(filters.command("addchannel") & filters.user(ADMIN_IDS))
async def addchannel_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            small_caps("❌ **Usage:** /addchannel @channelusername\n\n"
                       "Example: /addchannel @MyChannel")
        )
        return
    
    channel_input = args[1].strip()
    
    if channel_input.startswith("@"):
        channel_input = channel_input[1:]
    
    try:
        chat = await client.get_chat(f"@{channel_input}")
        if chat.type not in ["channel", "supergroup"]:
            await message.reply_text(small_caps("❌ This is not a valid channel."))
            return
        
        try:
            member = await client.get_chat_member(chat.id, "me")
            if member.status not in ["administrator", "creator"]:
                await message.reply_text(
                    small_caps("❌ I am not an admin in this channel.\n"
                               "Please add me as an admin and try again.")
                )
                return
        except Exception:
            await message.reply_text(
                small_caps("❌ I cannot access this channel.\n"
                           "Make sure I am an admin in the channel.")
            )
            return
        
        success = await fsub.add_channel(chat.id)
        if success:
            await message.reply_text(
                small_caps(f"✅ **Channel added successfully!**\n\n"
                           f"📢 Channel: @{channel_input}\n"
                           f"🆔 Chat ID: `{chat.id}`\n\n"
                           f"Users must now join this channel to use the bot.")
            )
        else:
            await message.reply_text(
                small_caps(f"❌ Channel @{channel_input} is already in the force-subscribe list.")
            )
            
    except Exception as e:
        await message.reply_text(
            small_caps(f"❌ **Error:** Could not find or access channel @{channel_input}.\n\n"
                       f"Make sure:\n"
                       f"• The channel exists\n"
                       f"• I am an admin in the channel\n"
                       f"• The channel username is correct\n\n"
                       f"Error: {str(e)}")
        )
register_admin_command("addchannel", "Add a channel to the force-subscribe list.")

# ==============================================
# /removechannel – Remove a channel from force-subscribe list (admin only)
# ==============================================
@app.on_message(filters.command("removechannel") & filters.user(ADMIN_IDS))
async def removechannel_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            small_caps("❌ **Usage:** /removechannel @channelusername\n\n"
                       "Example: /removechannel @MyChannel\n\n"
                       "To see all channels, use /channels")
        )
        return
    
    channel_input = args[1].strip()
    
    if channel_input.startswith("@"):
        channel_input = channel_input[1:]
    
    try:
        chat = await client.get_chat(f"@{channel_input}")
        success = await fsub.remove_channel(chat.id)
        
        if success:
            await message.reply_text(
                small_caps(f"✅ **Channel removed successfully!**\n\n"
                           f"📢 Channel: @{channel_input}\n"
                           f"🆔 Chat ID: `{chat.id}`\n\n"
                           f"Users are no longer required to join this channel.")
            )
        else:
            await message.reply_text(
                small_caps(f"❌ Channel @{channel_input} is not in the force-subscribe list.\n\n"
                           f"Use /channels to see the current list.")
            )
            
    except Exception as e:
        await message.reply_text(
            small_caps(f"❌ **Error:** Could not find channel @{channel_input}.\n\n"
                       f"Error: {str(e)}")
        )
register_admin_command("removechannel", "Remove a channel from the force-subscribe list.")

# ==============================================
# /channels – List all force-subscribe channels (admin only)
# ==============================================
@app.on_message(filters.command("channels") & filters.user(ADMIN_IDS))
async def channels_cmd(client: Client, message: Message):
    channels = await fsub.get_channels()
    
    if not channels:
        await message.reply_text(
            small_caps("📢 **Force-Subscribe Channels**\n\n"
                       "No channels are currently set.\n\n"
                       "Use /addchannel @channelusername to add one.")
        )
        return
    
    channel_list = []
    for channel_id in channels:
        try:
            chat = await client.get_chat(int(channel_id))
            username = f"@{chat.username}" if chat.username else "No username"
            title = chat.title or "Unknown"
            channel_list.append(f"• **{title}**\n  📌 {username}\n  🆔 `{channel_id}`")
        except Exception:
            channel_list.append(f"• **Unknown Channel**\n  🆔 `{channel_id}`")
    
    channels_text = "\n\n".join(channel_list)
    
    await message.reply_text(
        small_caps(f"📢 **Force-Subscribe Channels**\n"
                   f"━━━━━━━━━━━━━━━━━━\n\n"
                   f"{channels_text}\n\n"
                   f"**Total Channels:** {len(channels)}\n\n"
                   f"Use /addchannel to add a channel\n"
                   f"Use /removechannel to remove a channel")
    )
register_admin_command("channels", "List all force-subscribe channels.")

# ==============================================
# NEW: /vid — PREVIEW VIDEO FOR A STICKER PACK
# Manages ONE optional preview video per sticker pack. The video itself
# is never stored permanently on the VPS — only its Catbox URL is saved
# in MongoDB. Every temp file created during the flow is removed as soon
# as it's no longer needed (see cleanup_temp_files calls throughout).
# Fully additive: packs without a preview keep working exactly as before.
# ==============================================
async def start_vid_flow_for_pack(client, ref_message, uid, pack_index, from_user=None):
    """Show the pack's current preview status, then prompt for a new video."""
    pack = get_pack_by_index(uid, pack_index)
    user = from_user or ref_message.from_user
    is_group = ref_message.chat.type != "private"
    chat_id = ref_message.chat.id

    if pack and pack.get("preview_video_url"):
        info_text = small_caps(f"📦 Pack: {pack['display_title']}\n\n🎬 Preview Available")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch Preview", callback_data=f"vidwatch_{pack_index}")]])
    else:
        info_text = small_caps(f"📦 Pack: {pack['display_title'] if pack else pack_index}\n\n❌ No Preview")
        kb = None
    await send_vid_message(client, chat_id, is_group, user, info_text, reply_markup=kb)

    vid_sessions[uid] = {
        "step": "waiting_video",
        "pack_index": pack_index,
        "started_at": time.time()
    }
    await send_vid_message(
        client, chat_id, is_group, user,
        small_caps(
            "🎥 Send the preview video.\n\n"
            "Requirements:\n"
            "• MP4 format\n"
            "• Maximum 3 seconds\n"
            "• Portrait or landscape accepted (auto-resized to 512x512)"
        )
    )

@app.on_message(filters.command("vid") & (filters.private | filters.group))
async def vid_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    vid_sessions[uid] = {"step": "vid_waiting_destination", "started_at": time.time()}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 New Pack", callback_data="viddest_new")],
        [InlineKeyboardButton("📦 Existing Pack", callback_data="viddest_existing")],
        [InlineKeyboardButton("❌ Cancel", callback_data="viddest_cancel")]
    ])
    await reply_or_dm(client, message, small_caps("📦 Where do you want to save the preview video?"), reply_markup=kb)
register_user_command("vid", "Add or replace a preview video for one of your sticker packs.", category=CATEGORY_STICKER)

@app.on_callback_query(filters.regex(r"^viddest_"))
async def vid_destination_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_destination":
        await callback.answer(small_caps("Session expired. Start over with /vid."), show_alert=True)
        return

    choice = callback.data.replace("viddest_", "")
    await callback.answer()

    if choice == "cancel":
        vid_sessions.pop(uid, None)
        await callback.message.edit_text(small_caps("❌ Cancelled."))
        return

    if choice == "new":
        vid_sessions[uid]["step"] = "vid_waiting_pack_name"
        await callback.message.edit_text(
            small_caps("✏️ Enter the display name for your new sticker pack.\n\nExample:\nAnime Pack")
        )
        return

    packs = get_user_packs(uid)
    if not packs:
        vid_sessions.pop(uid, None)
        await callback.message.edit_text(small_caps("❌ You have no sticker packs yet. Create one with /sticker or /plain."))
        return

    if len(packs) == 1:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await start_vid_flow_for_pack(client, callback.message, uid, packs[0]["pack_index"], from_user=callback.from_user)
        return

    rows, row = [], []
    for pack in packs:
        row.append(InlineKeyboardButton(f"[{pack['pack_index']}] {pack['display_title']}", callback_data=f"vidpack_{pack['pack_index']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    vid_sessions[uid]["step"] = "waiting_pack_selection"
    await callback.message.edit_text(
        small_caps("📦 Select the sticker pack for the preview video:"),
        reply_markup=InlineKeyboardMarkup(rows)
    )

@app.on_callback_query(filters.regex(r"^vidpack_"))
async def vidpack_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in vid_sessions or vid_sessions[uid].get("step") != "waiting_pack_selection":
        await callback.answer(small_caps("Session expired. Start over with /vid."), show_alert=True)
        return

    pack_index = int(callback.data.replace("vidpack_", ""))
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await callback.answer(small_caps("Invalid pack."), show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await start_vid_flow_for_pack(client, callback.message, uid, pack_index, from_user=callback.from_user)

@app.on_message((filters.video | filters.document | filters.animation) & (filters.private | filters.group), group=8)
async def handle_vid_upload(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in vid_sessions or vid_sessions[uid].get("step") != "waiting_video":
        return

    if message.animation:
        await reply_or_dm(client, message, small_caps("❌ GIFs are not supported. Please send an MP4 video (max 3 seconds)."))
        return

    media = message.video or message.document
    if not media:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid MP4 video."))
        return

    mime = (getattr(media, "mime_type", None) or "").lower()
    file_name = (getattr(media, "file_name", None) or "").lower()

    if "webm" in mime or file_name.endswith(".webm"):
        await reply_or_dm(client, message, small_caps("❌ WEBM videos are not supported. Please send an MP4 video."))
        return
    if file_name.endswith(".tgs"):
        await reply_or_dm(client, message, small_caps("❌ TGS files are not supported. Please send an MP4 video."))
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading video..."))

    temp_input = os.path.join(TEMP_DIR, f"vid_in_{uid}_{uuid.uuid4().hex}.mp4")
    try:
        await client.download_media(message, file_name=temp_input)
    except Exception as e:
        cleanup_temp_files(temp_input)
        await proc.edit_text(small_caps(f"❌ Failed to download video: {str(e)}"))
        return

    duration = await probe_video_duration(temp_input)
    if duration is None:
        cleanup_temp_files(temp_input)
        await proc.edit_text(small_caps("❌ Invalid or unsupported video format. Please send a valid MP4 video."))
        return
    if duration > MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
        cleanup_temp_files(temp_input)
        await proc.edit_text(small_caps(f"❌ Video is too long ({duration:.1f}s). Maximum allowed is 3 seconds."))
        return

    vid_sessions[uid]["video_path"] = temp_input
    vid_sessions[uid]["step"] = "waiting_text_choice"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Add Text", callback_data="vidtext_add"),
         InlineKeyboardButton("⏭ Skip", callback_data="vidtext_skip")]
    ])
    await proc.edit_text(small_caps("✅ Video received.\n\nDo you want to add text on the preview?"), reply_markup=kb)

async def generate_and_send_preview(client, chat_id, is_group, from_user, uid, status_message=None):
    """Run FFmpeg on the stored source video (with or without text),
    then send the rendered preview back with Edit Text / Save / Cancel."""
    session = vid_sessions.get(uid)
    if not session or not session.get("video_path"):
        text = small_caps("❌ Session expired. Start over with /vid.")
        if status_message:
            await status_message.edit_text(text)
        else:
            await send_vid_message(client, chat_id, is_group, from_user, text)
        vid_sessions.pop(uid, None)
        return

    input_path = session["video_path"]
    text = session.get("text")

    # Remove any previous render before generating a new one
    cleanup_temp_files(session.get("preview_path"))
    output_path = os.path.join(TEMP_DIR, f"vid_out_{uid}_{uuid.uuid4().hex}.mp4")

    ok, err = await generate_vid_preview(input_path, output_path, text)
    if not ok:
        msg = small_caps(f"❌ Failed to process the video: {err}")
        if status_message:
            await status_message.edit_text(msg)
        else:
            await send_vid_message(client, chat_id, is_group, from_user, msg)
        return

    session["preview_path"] = output_path
    session["step"] = "preview_ready"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Edit Text", callback_data="vidpreview_edit"),
         InlineKeyboardButton("✅ Save", callback_data="vidpreview_save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="vidpreview_cancel")]
    ])

    caption = small_caps("🎬 Preview generated.\n\nEdit the text, save it to the pack, or cancel.")
    if is_group and from_user:
        mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
        caption = f"{mention}\n\n{caption}"

    if status_message:
        try:
            await status_message.delete()
        except Exception:
            pass

    try:
        await client.send_video(chat_id, video=output_path, caption=caption, reply_markup=kb)
    except Exception as e:
        await send_vid_message(client, chat_id, is_group, from_user, small_caps(f"❌ Failed to send the preview: {str(e)}"))

@app.on_callback_query(filters.regex(r"^vidtext_"))
async def vidtext_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in vid_sessions or vid_sessions[uid].get("step") != "waiting_text_choice":
        await callback.answer(small_caps("Session expired. Start over with /vid."), show_alert=True)
        return

    choice = callback.data.replace("vidtext_", "")
    await callback.answer()

    if choice == "add":
        vid_sessions[uid]["step"] = "waiting_vid_text"
        await callback.message.edit_text(
            small_caps("✏️ Send the text you want to overlay on the preview.\n\nExample:\nVIDEO")
        )
        return

    vid_sessions[uid]["text"] = None
    await callback.message.edit_text(small_caps("⚙️ Generating preview..."))
    await generate_and_send_preview(
        client, callback.message.chat.id, callback.message.chat.type != "private", callback.from_user, uid
    )

@app.on_message(filters.text & (filters.private | filters.group) & ~filters.regex(r'^/'), group=9)
async def vid_text_input(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in vid_sessions:
        return

    step = vid_sessions[uid].get("step")

    if step == "vid_waiting_pack_name":
        display_title = message.text.strip()
        if not display_title:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
            return
        pack_index = get_next_pack_index(uid)
        packs_collection.insert_one({
            "user_id": str(uid),
            "pack_index": pack_index,
            "display_title": display_title,
            "telegram_pack_name": None,
            "emoji": DEFAULT_EMOJI,
            "created_at": datetime.now(),
            "total_stickers": 0
        })
        vid_sessions[uid]["pack_index"] = pack_index
        proc = await reply_or_dm(client, message, small_caps(f"✅ Pack Created: {display_title}"))
        await start_vid_flow_for_pack(client, message, uid, pack_index)
        return

    if step not in ("waiting_vid_text", "editing_vid_text"):
        return

    text = message.text.strip()
    if not text:
        await reply_or_dm(client, message, small_caps("❌ Please send some text."))
        return

    vid_sessions[uid]["text"] = text
    proc = await reply_or_dm(client, message, small_caps("⚙️ Generating preview..."))
    await generate_and_send_preview(
        client, message.chat.id, message.chat.type != "private", message.from_user, uid, status_message=proc
    )

@app.on_callback_query(filters.regex(r"^vidpreview_"))
async def vidpreview_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in vid_sessions or vid_sessions[uid].get("step") != "preview_ready":
        await callback.answer(small_caps("Session expired. Start over with /vid."), show_alert=True)
        return

    session = vid_sessions[uid]
    choice = callback.data.replace("vidpreview_", "")
    await callback.answer()

    if choice == "edit":
        session["step"] = "editing_vid_text"
        await callback.message.reply_text(small_caps("✏️ Send the new text for the preview."))
        return

    if choice == "cancel":
        cleanup_temp_files(session.get("video_path"), session.get("preview_path"))
        vid_sessions.pop(uid, None)
        await callback.message.reply_text(small_caps("❌ Cancelled. Temporary files removed."))
        return

    # choice == "save"
    pack_index = session.get("pack_index")
    preview_path = session.get("preview_path")
    pack = get_pack_by_index(uid, pack_index)

    if not preview_path or not pack:
        cleanup_temp_files(session.get("video_path"), session.get("preview_path"))
        vid_sessions.pop(uid, None)
        await callback.message.reply_text(small_caps("❌ Session expired. Start over with /vid."))
        return

    status = await callback.message.reply_text(small_caps("⚙️ Uploading preview..."))

    catbox_url = None
    db_ok = False
    error_text = None

    try:
        catbox_url = await upload_to_catbox(preview_path)
    except Exception as e:
        error_text = f"❌ Catbox upload failed: {str(e)}"

    if catbox_url and not error_text:
        try:
            packs_collection.update_one(
                {"user_id": str(uid), "pack_index": pack_index},
                {"$set": {
                    "preview_video_url": catbox_url,
                    "preview_text": session.get("text"),
                    "preview_updated_at": datetime.now()
                }}
            )
            db_ok = True
        except Exception as e:
            error_text = f"❌ Failed to save preview to the database: {str(e)}"

    # Never keep the source video or the rendered preview on disk beyond this point.
    cleanup_temp_files(session.get("video_path"), session.get("preview_path"))
    vid_sessions.pop(uid, None)

    if db_ok:
        await status.edit_text(
            small_caps(f"✅ Preview saved for Pack Index {pack_index} ({pack['display_title']}).")
        )
    else:
        await status.edit_text(small_caps(error_text or "❌ Failed to save the preview."))

@app.on_callback_query(filters.regex(r"^vidwatch_"))
async def vidwatch_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    pack_index = int(callback.data.replace("vidwatch_", ""))
    pack = get_pack_by_index(uid, pack_index)

    if not pack or not pack.get("preview_video_url"):
        await callback.answer(small_caps("Preview not available."), show_alert=True)
        return

    await callback.answer()
    try:
        await client.send_video(
            callback.message.chat.id,
            video=pack["preview_video_url"],
            caption=small_caps(f"🎬 Preview: {pack['display_title']}")
        )
    except Exception as e:
        await callback.message.reply_text(small_caps(f"❌ Failed to send preview: {str(e)}"))

# ==============================================
# NEW: /plain FLOW CALLBACKS
# ==============================================

@app.on_callback_query(filters.regex(r"^plaindest_"))
async def plain_destination_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_destination":
        await callback.answer(small_caps("Session expired. Start over with /plain."), show_alert=True)
        return

    choice = callback.data.replace("plaindest_", "")
    await callback.answer()

    if choice == "cancel":
        plain_sessions.pop(uid, None)
        await callback.message.edit_text(small_caps("❌ Cancelled."))
        return

    if choice == "new":
        plain_sessions[uid]["step"] = "plain_waiting_pack_name"
        await callback.message.edit_text(
            small_caps("✏️ Enter the display name for your new sticker pack.\n\nExample:\nAnime Pack")
        )
        return

    packs = get_user_packs(uid)
    if not packs:
        plain_sessions.pop(uid, None)
        await callback.message.edit_text(small_caps("❌ You have no sticker packs yet. Create one with /sticker or /plain."))
        return

    if len(packs) == 1:
        pack = packs[0]
        plain_sessions[uid]["pack_index"] = pack["pack_index"]
        plain_sessions[uid]["display_title"] = pack["display_title"]
        plain_sessions[uid]["step"] = "plain_waiting_media"
        try:
            await callback.message.delete()
        except Exception:
            pass
        chat_id = callback.message.chat.id
        is_group = callback.message.chat.type != "private"
        text = small_caps(
            "📸 Send me the media now.\n\n"
            "Supported:\n"
            "• Photo\n• PNG\n• WEBP Sticker\n• Static Sticker\n"
            "• MP4 Video\n• WEBM Video Sticker"
        )
        if is_group:
            mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
            text = f"{mention}\n\n{text}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
        ])
        await client.send_message(chat_id, text, reply_markup=kb)
        return

    rows, row = [], []
    for pack in packs:
        row.append(InlineKeyboardButton(f"[{pack['pack_index']}] {pack['display_title']}", callback_data=f"plainpack_{pack['pack_index']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    plain_sessions[uid]["step"] = "plain_waiting_pack_selection"
    await callback.message.edit_text(
        small_caps("📦 Select the sticker pack:"),
        reply_markup=InlineKeyboardMarkup(rows)
    )

@app.on_callback_query(filters.regex(r"^plainpack_"))
async def plain_pack_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_pack_selection":
        await callback.answer(small_caps("Session expired. Start over with /plain."), show_alert=True)
        return

    pack_index = int(callback.data.replace("plainpack_", ""))
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await callback.answer(small_caps("Invalid pack."), show_alert=True)
        return

    await callback.answer()
    plain_sessions[uid]["pack_index"] = pack_index
    plain_sessions[uid]["display_title"] = pack["display_title"]
    plain_sessions[uid]["step"] = "plain_waiting_media"

    try:
        await callback.message.delete()
    except Exception:
        pass

    chat_id = callback.message.chat.id
    is_group = callback.message.chat.type != "private"
    text = small_caps(
        "📸 Send me the media now.\n\n"
        "Supported:\n"
        "• Photo\n• PNG\n• WEBP Sticker\n• Static Sticker\n"
        "• MP4 Video\n• WEBM Video Sticker"
    )
    if is_group:
        mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
        text = f"{mention}\n\n{text}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
    ])
    await client.send_message(chat_id, text, reply_markup=kb)

@app.on_callback_query(filters.regex(r"^plainflow_cancel$"))
async def plain_flow_cancel_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid in plain_sessions:
        session = plain_sessions[uid]
        cleanup_temp_files(session.get("media_path"))
        plain_sessions.pop(uid, None)
    await callback.answer()
    await callback.message.edit_text(small_caps("❌ Cancelled."))

@app.on_callback_query(filters.regex(r"^plainemoji_"))
async def plain_emoji_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_emoji":
        await callback.answer(small_caps("Session expired. Start over with /plain."), show_alert=True)
        return

    choice = callback.data.replace("plainemoji_", "")
    await callback.answer()

    session = plain_sessions[uid]
    if choice == "default":
        session["emoji"] = "😀"
    else:
        session["step"] = "plain_waiting_emoji_text"
        await callback.message.edit_text(small_caps("✏️ Send an emoji for this sticker."))
        return

    session["step"] = "plain_preview"
    is_group = callback.message.chat.type != "private"
    await send_plain_preview(client, callback.message.chat.id, callback.from_user, uid, is_group=is_group)

@app.on_callback_query(filters.regex(r"^plainpreview_"))
async def plain_preview_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_preview":
        await callback.answer(small_caps("Session expired. Start over with /plain."), show_alert=True)
        return

    choice = callback.data.replace("plainpreview_", "")
    await callback.answer()
    session = plain_sessions[uid]

    if choice == "cancel":
        cleanup_temp_files(session.get("media_path"))
        plain_sessions.pop(uid, None)
        await callback.message.edit_text(small_caps("❌ Cancelled."))
        return

    if choice == "replace":
        session["step"] = "plain_waiting_media"
        await callback.message.edit_text(
            small_caps(
                "📸 Send me the media now.\n\n"
                "Supported:\n"
                "• Photo\n• PNG\n• WEBP Sticker\n• Static Sticker\n"
                "• MP4 Video\n• WEBM Video Sticker"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
            ])
        )
        return

    if choice == "save":
        pack_index = session.get("pack_index")
        display_title = session.get("display_title")
        emoji = session.get("emoji", "😀")
        media_type = session.get("media_type")
        pack = get_pack_by_index(uid, pack_index)

        is_new_pack = pack is None
        telegram_pack_name = pack.get("telegram_pack_name") if pack else None

        proc = await callback.message.reply_text(small_caps("⚙️ Adding sticker to pack..."))

        if media_type == "static":
            sticker_bytes = session.get("sticker_bytes")
            if not sticker_bytes:
                cleanup_temp_files(session.get("media_path"))
                plain_sessions.pop(uid, None)
                await proc.edit_text(small_caps("❌ Session expired. Start over with /plain."))
                return

            if not telegram_pack_name:
                pack_type = "static"
                success, result, telegram_pack_name_new = await create_unique_sticker_pack(client, uid, display_title, sticker_bytes, emoji)
                if not success:
                    cleanup_temp_files(session.get("media_path"))
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))
                    return
                create_pack_record(uid, pack_index, display_title, telegram_pack_name_new, emoji)
                packs_collection.update_one(
                    {"user_id": str(uid), "pack_index": pack_index},
                    {"$set": {"pack_type": pack_type}}
                )
                telegram_pack_name = telegram_pack_name_new
            else:
                if not is_new_pack and pack.get("pack_type", "static") != "static":
                    cleanup_temp_files(session.get("media_path"))
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps("❌ This is a video sticker pack. Static stickers cannot be added."))
                    return
                success, err = await add_sticker_to_pack(client, uid, telegram_pack_name, sticker_bytes, emoji)
                if not success:
                    cleanup_temp_files(session.get("media_path"))
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))
                    return
        else:
            media_path = session.get("media_path")
            if not media_path or not os.path.exists(media_path):
                plain_sessions.pop(uid, None)
                await proc.edit_text(small_caps("❌ Session expired. Start over with /plain."))
                return

            if not telegram_pack_name:
                pack_type = "video"
                with open(media_path, "rb") as f:
                    video_bytes = f.read()
                success, result, telegram_pack_name_new = await create_unique_video_sticker_pack(client, uid, display_title, video_bytes, emoji)
                if not success:
                    cleanup_temp_files(media_path)
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))
                    return
                create_pack_record(uid, pack_index, display_title, telegram_pack_name_new, emoji)
                packs_collection.update_one(
                    {"user_id": str(uid), "pack_index": pack_index},
                    {"$set": {"pack_type": pack_type}}
                )
                telegram_pack_name = telegram_pack_name_new
            else:
                if not is_new_pack and pack.get("pack_type", "static") != "video":
                    cleanup_temp_files(media_path)
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps("❌ This is a static sticker pack. Video stickers cannot be added."))
                    return
                with open(media_path, "rb") as f:
                    video_bytes = f.read()
                success, err = await add_video_sticker_to_pack(client, uid, telegram_pack_name, video_bytes, emoji)
                if not success:
                    cleanup_temp_files(media_path)
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))
                    return

        if not is_new_pack:
            increment_pack_sticker_count(uid, pack_index)
        increment_sticker_count(uid)

        updated_pack = get_pack_by_index(uid, pack_index)
        total = updated_pack.get("total_stickers", 0) if updated_pack else 0

        cleanup_temp_files(session.get("media_path"))
        plain_sessions.pop(uid, None)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Open Sticker Pack", url=f"https://t.me/addstickers/{telegram_pack_name}")]
        ])
        await proc.edit_text(
            small_caps(
                f"✅ Sticker Added Successfully!\n\n"
                f"📦 Pack: {display_title}\n"
                f"🆔 Sticker Number: {total}\n"
                f"🎉 Total Stickers: {total}"
            ),
            reply_markup=kb
        )

# ==============================================
# NEW: /create WIZARD (BOT OWNER / SUDO ADMINS ONLY)
# Purely generates a ready-to-copy /addchannel command. It NEVER calls
# fsub.add_channel() or any /addchannel logic directly — the admin must
# copy and run the generated command themselves.
# ==============================================
CREATE_SESSION_TIMEOUT = 600  # 10 minutes

def _create_session_expired(session):
    return (time.time() - session.get("started_at", 0)) > CREATE_SESSION_TIMEOUT

def _build_addchannel_command(data):
    """Assemble the /addchannel command string from the collected wizard answers."""
    parts = ["/addchannel"]

    chat_id = data.get("chat_id")
    if chat_id:
        parts.append(chat_id)

    username = data.get("username")
    if username:
        parts.append(username)

    if data.get("request"):
        parts.append("--request")

    button_text = data.get("button_text")
    if button_text:
        parts.append(f'--button="{button_text}"')

    title = data.get("title")
    if title:
        parts.append(title)

    return " ".join(parts)

@app.on_message(filters.command("create") & filters.user(ADMIN_IDS) & (filters.private | filters.group))
async def create_wizard_start(client: Client, message: Message):
    uid = message.from_user.id
    create_sessions[uid] = {"step": "waiting_chatid", "started_at": time.time()}
    await reply_or_dm(
        client, message,
        small_caps("🆔 Send the Chat ID of the Channel or Group.\n\nExample:\n-1001234567890")
    )
register_admin_command("create", "Generate a ready-to-copy /addchannel command via a step-by-step wizard.")

@app.on_message(filters.command("cancel") & filters.user(ADMIN_IDS) & (filters.private | filters.group))
async def create_wizard_cancel(client: Client, message: Message):
    uid = message.from_user.id
    if uid in create_sessions:
        create_sessions.pop(uid, None)
        await reply_or_dm(client, message, small_caps("❌ /create wizard cancelled."))
    # If there's no active /create session, do nothing — /cancel shouldn't
    # interfere with anything else in the bot.
register_admin_command("cancel", "Cancel an in-progress /create wizard.")

@app.on_message(
    filters.text & (filters.private | filters.group) & ~filters.regex(r'^/'),
    group=7
)
async def create_wizard_text(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in create_sessions:
        return
    if uid not in ADMIN_IDS:
        create_sessions.pop(uid, None)
        return

    session = create_sessions[uid]
    if _create_session_expired(session):
        create_sessions.pop(uid, None)
        await reply_or_dm(client, message, small_caps("⌛ Your /create session timed out. Start again with /create."))
        return

    step = session.get("step")
    text = message.text.strip()

    if step == "waiting_chatid":
        if not text:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid Chat ID."))
            return
        session["chat_id"] = text
        session["step"] = "waiting_username"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "👤 Send the Username or Join Link.\n\n"
                "Examples:\n@MyChannel\nor\nhttps://t.me/MyChannel\n"
                "or\nhttps://t.me/+AbCdEf12345\n\n"
                "Reply with:\nskip\nif you don't want to provide one."
            )
        )
        return

    if step == "waiting_username":
        if text.lower() != "skip":
            session["username"] = text
        session["step"] = "waiting_title"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "📛 Send a Custom Title.\n\nExample:\nAnime Updates\n\n"
                "Reply with:\nskip\nto use Telegram's title."
            )
        )
        return

    if step == "waiting_title":
        if text.lower() != "skip":
            session["title"] = text
        session["step"] = "waiting_request"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps("❓ Require Join Request?\n\nReply with:\nyes\nor\nno")
        )
        return

    if step == "waiting_request":
        low = text.lower()
        if low not in ("yes", "no"):
            await reply_or_dm(client, message, small_caps("❌ Please reply with:\nyes\nor\nno"))
            return
        session["request"] = (low == "yes")
        session["step"] = "waiting_button"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "🔘 Custom Join Button Text\n\nExample:\n🎬 Join Movies\n\n"
                "Reply with:\nskip\nto use the default button."
            )
        )
        return

    if step == "waiting_button":
        if text.lower() != "skip":
            session["button_text"] = text

        command = _build_addchannel_command(session)
        create_sessions.pop(uid, None)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy Command", callback_data="createcmd_copy")]
        ])
        sent = await reply_or_dm(
            client, message,
            small_caps("✅ Command Generated Successfully\n\nCopy and run the command below:") + f"\n\n`{command}`",
            reply_markup=kb
        )
        generated_commands[sent.id] = command
        return

# ==============================================
# NEW: "📋 Copy Command" CALLBACK
# Telegram Bot API has no universal clipboard-copy button, so this
# resends the exact generated command in a code block for easy copying.
# ==============================================
@app.on_callback_query(filters.regex(r"^createcmd_copy$"))
async def create_copy_callback(client: Client, callback: CallbackQuery):
    command = generated_commands.get(callback.message.id)
    if not command:
        await callback.answer(small_caps("Command no longer available."), show_alert=True)
        return
    await callback.answer()
    await callback.message.reply_text(f"`{command}`")

# ==============================================
# NEW: /help – DYNAMICALLY BUILT FROM THE USER COMMAND REGISTRY
# Never hardcodes the command list; renders whatever is currently in
# USER_COMMAND_CATEGORIES, so any new register_user_command(...) call
# elsewhere in the file shows up here automatically.
# ==============================================
@app.on_message(filters.command("help") & (filters.private | filters.group))
async def help_cmd(client: Client, message: Message):
    lines = ["📋 <b>Help Menu</b>", ""]
    for category, commands in USER_COMMAND_CATEGORIES.items():
        lines.append(f"{category}")
        for command, description in commands:
            lines.append(f"• <code>/{command}</code> — {description}")
        lines.append("")

    text = "\n".join(lines).strip()
    await reply_or_dm(client, message, text, parse_mode=HTML_PARSE_MODE)
register_user_command("help", "View all available user commands.", category=CATEGORY_GENERAL)

# ==============================================
# NEW: /adminhelp – BOT OWNER / SUDO ADMINS ONLY
# Dynamically built from the ADMIN_COMMANDS registry the same way /help
# is built from USER_COMMAND_CATEGORIES. Non-admins get "Unauthorized".
# ==============================================
@app.on_message(filters.command("adminhelp") & (filters.private | filters.group))
async def adminhelp_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        await reply_or_dm(
            client, message,
            small_caps("🚫 Unauthorized. This command is for the Bot Owner and Sudo Admins only.")
        )
        return

    lines = ["🛠 <b>Admin Commands</b>", ""]
    for command, description in ADMIN_COMMANDS:
        lines.append(f"• <code>/{command}</code> — {description}")

    text = "\n".join(lines).strip()
    await reply_or_dm(client, message, text, parse_mode=HTML_PARSE_MODE)
register_admin_command("adminhelp", "View all available admin commands.")

# ==============================================
# INITIALIZE AND RUN BOT
# ==============================================
def main():
    global BOT_USERNAME
    print(small_caps("🚀 Starting Sticker Bot..."))

    with app:
        me = app.get_me()
        BOT_USERNAME = me.username

        print(small_caps(f"📊 Bot Username: @{me.username}"))
        print(small_caps(f"🤖 Bot Name: {me.first_name}"))
        print(small_caps(f"📁 Database: {DB_NAME}"))
        print(small_caps(f"👤 Admin ID(s): {', '.join(str(a) for a in ADMIN_IDS)}"))
        print(small_caps("✅ Bot is ready!"))

        users_collection.create_index("user_id", unique=True)
        users_collection.create_index("last_active")
        packs_collection.create_index([("user_id", 1), ("pack_index", 1)], unique=True)

        idle()

if __name__ == "__main__":
    main()
