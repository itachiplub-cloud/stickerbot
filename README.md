# 🎨 Telegram Sticker Maker Bot

A powerful Telegram Sticker Maker Bot built with **Pyrogram** and **Pillow**.

Create high-quality Telegram stickers directly from photos, images, or text with automatic background removal, resizing, and optimization.

---

## ✨ Features

- 🖼️ Create static Telegram stickers
- 🎭 PNG image support
- 📝 Text to Sticker
- 🎨 Custom fonts
- ⚡ Fast processing
- 🤖 Built with Pyrogram v2
- 💾 MongoDB Support
- 🔒 Owner/Admin System
- 📦 Easy Deployment
- 🌐 Works on Windows, Linux & VPS

---

# 📂 Project Structure

```text
stickermaker/
│
├── bot.py
├── config.py
├── requirements.txt
├── .env
├── handlers/
├── utils/
├── fonts/
├── assets/
├── database/
└── README.md
```

---

# 🚀 Installation

## Clone Repository

```bash
git clone https://github.com/itachiplub-cloud/stickerbot.git

cd stickerbot
```

---

## Create Virtual Environment

### Windows

```bash
python -m venv .venv

.venv\Scripts\activate
```

### Linux / Ubuntu / WSL

```bash
python3 -m venv .venv

source .venv/bin/activate
```

---

## Install Requirements

```bash
pip install -r requirements.txt
```

---

# ⚙ Environment Variables

Create a file named:

```text
.env
```

Example:

```env
API_ID=12345678
API_HASH=xxxxxxxxxxxxxxxxxxxxxxxx
BOT_TOKEN=123456:ABCDEF
MONGO_URI=mongodb://localhost:27017
OWNER_ID=123456789
```

---

# ▶ Run Bot

Windows

```bash
python bot.py
```

Linux

```bash
python3 bot.py
```

---

# 📦 Install FFmpeg (Optional)

Ubuntu

```bash
sudo apt update
sudo apt install ffmpeg
```

Windows

Download FFmpeg

https://ffmpeg.org/download.html

---

# 🛠 Commands

| Command | Description |
|----------|-------------|
| /start | Start Bot |
| /help | Help Menu |
| /sticker | Create Sticker |
| /text | Text to Sticker |

---

# 📋 Requirements

- Python 3.11+
- MongoDB
- Pillow
- Pyrogram v2
- TgCrypto

---

# Install Dependencies

```bash
pip install -r requirements.txt
```

---

# ❤️ Support

If you like this project don't forget to ⭐ the repository.

---

# 👨‍💻 Developer

**GitHub**

https://github.com/itachiplub-cloud

---

# 📜 License

MIT License
