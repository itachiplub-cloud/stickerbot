def register(app, fsub):
    from stick import (
        small_caps, reply_or_dm, vid_sessions, TEMP_DIR, DEFAULT_EMOJI,
        BOT_USERNAME, get_user_packs, get_pack_by_index, get_user_pack,
        get_next_pack_index, create_pack_record, create_unique_video_sticker_pack,
        add_video_sticker_to_pack, increment_pack_sticker_count,
        increment_sticker_count, generate_vid_preview, upload_to_catbox,
        normalize_video_sticker, cleanup_temp_files, probe_video_duration,
        cancel_user_session, MAX_PREVIEW_SECONDS, DURATION_TOLERANCE,
        register_user_command, CATEGORY_STICKER, packs_collection
    )
    import os, uuid, time
    from io import BytesIO
    from pyrogram import Client, filters, StopPropagation
    from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

    # ==============================================
    # COMMAND: /vid
    # ==============================================
    @app.on_message(filters.command("vid") & (filters.private | filters.group))
    async def vid_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        uid = message.from_user.id
        if uid in vid_sessions:
            cleanup_temp_files(vid_sessions[uid].get("video_path"), vid_sessions[uid].get("webm_path"), vid_sessions[uid].get("preview_path"))
            vid_sessions.pop(uid, None)

        vid_sessions[uid] = {"step": "vid_waiting_destination", "started_at": time.time()}

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Pack", callback_data="viddest_new")],
            [InlineKeyboardButton("📦 Existing Pack", callback_data="viddest_existing")],
            [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
        ])
        await reply_or_dm(
            client, message,
            small_caps("🎥 **Create Video Sticker**\n\nWhere do you want to save it?"),
            reply_markup=kb
        )
    register_user_command("vid", "Create a video sticker from an MP4 video or WEBM sticker.", category=CATEGORY_STICKER)

    # ==============================================
    # HANDLE MEDIA (/vid Flow)
    # ==============================================
    @app.on_message((filters.video | filters.document | filters.sticker | filters.photo) & (filters.private | filters.group), group=11)
    async def handle_vid_media(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_media":
            return

        session = vid_sessions[uid]
        pack_index = session.get("pack_index")
        if pack_index is None:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /vid again."))
            cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
            vid_sessions.pop(uid, None)
            return

        pack = get_pack_by_index(uid, pack_index)
        if pack and pack.get("pack_type", "static") == "static":
            await reply_or_dm(client, message, small_caps("❌ This is a static sticker pack. Video stickers cannot be added. Use /vid to create a video pack."))
            return

        if message.photo:
            await reply_or_dm(client, message, small_caps("❌ Photos are not supported in /vid. Please send an MP4 video or WEBM Video Sticker."))
            return

        is_webm_input = False
        file_name = ""
        mime = ""

        if message.sticker:
            if message.sticker.is_animated:
                await reply_or_dm(client, message, small_caps("❌ Animated .TGS stickers are not supported. Send an MP4 video or WEBM Video Sticker."))
                return
            if message.sticker.is_video:
                is_webm_input = True
            else:
                await reply_or_dm(client, message, small_caps("❌ Static stickers are not supported in /vid. Send an MP4 video or WEBM Video Sticker."))
                return
        elif message.video:
            file_name = (message.video.file_name or "").lower()
            mime = (message.video.mime_type or "").lower()
            if "webm" in mime or file_name.endswith(".webm"):
                is_webm_input = True
        elif message.document:
            mime = (message.document.mime_type or "").lower()
            file_name = (message.document.file_name or "").lower()
            if mime.startswith("video/") or file_name.endswith(".mp4") or file_name.endswith(".webm"):
                if "webm" in mime or file_name.endswith(".webm"):
                    is_webm_input = True
            else:
                await reply_or_dm(client, message, small_caps("❌ Invalid file. Please upload an MP4 video or WEBM Video Sticker only."))
                return
        else:
            await reply_or_dm(client, message, small_caps("❌ Invalid file. Please upload an MP4 video or WEBM Video Sticker only."))
            return

        old_preview = session.pop("preview_message", None)
        if old_preview:
            try:
                await old_preview.delete()
            except Exception:
                pass

        proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading..."))

        ext = ".webm" if is_webm_input else ".mp4"
        temp_raw = os.path.join(TEMP_DIR, f"vid_raw_{uid}_{uuid.uuid4().hex}{ext}")
        webm_out = os.path.join(TEMP_DIR, f"vid_norm_{uid}_{uuid.uuid4().hex}.webm")

        try:
            await client.download_media(message, file_name=temp_raw)
            await proc.edit_text(small_caps("⚙️ Processing..."))

            if not os.path.exists(temp_raw) or os.path.getsize(temp_raw) == 0:
                cleanup_temp_files(temp_raw)
                await proc.edit_text(small_caps("❌ Download failed or empty file received."))
                return

            duration = await probe_video_duration(temp_raw)
            if duration is None:
                cleanup_temp_files(temp_raw)
                await proc.edit_text(small_caps("❌ Video probe failed: Invalid codec, unsupported media format, or corrupted file."))
                return
            if duration > MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
                cleanup_temp_files(temp_raw)
                await proc.edit_text(small_caps(f"❌ Video is too long ({duration:.1f}s). Maximum allowed duration is 3 seconds."))
                return

            ok, err = await normalize_video_sticker(temp_raw, webm_out)
            if not ok:
                cleanup_temp_files(temp_raw)
                await proc.edit_text(small_caps(f"❌ Failed to process video: {err}"))
                return

            cleanup_temp_files(session.get("video_path"), session.get("webm_path"))
            session["video_path"] = temp_raw
            session["webm_path"] = webm_out

            if "text_option_done" in session:
                session["step"] = "vid_preview"
                await proc.delete()
                await send_vid_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")
            else:
                session["step"] = "vid_waiting_text_option"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏ Add Text", callback_data="vidtext_add"),
                     InlineKeyboardButton("⏭ Skip", callback_data="vidtext_skip")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
                ])
                await proc.edit_text(small_caps("Do you want to add text?"), reply_markup=kb)

        except Exception as e:
            cleanup_temp_files(temp_raw, webm_out)
            await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))
            # BUG 3 FIX: Do NOT pop session on transient errors

    # ==============================================
    # TEXT ROUTING FOR /vid
    # ==============================================
    @app.on_message(filters.text & (filters.private | filters.group), group=5)
    async def handle_vid_text(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        if uid not in vid_sessions:
            return
        step = vid_sessions[uid].get("step")
        if step == "vid_waiting_pack_name":
            await handle_vid_new_pack_name(client, message, uid)
        elif step in ("vid_waiting_text_input", "vid_editing_text_input"):
            await handle_vid_text_input(client, message, uid)
        elif step in ("vid_waiting_emoji_text", "vid_waiting_emoji"):
            await handle_vid_emoji_input(client, message, uid)

    # ==============================================
    # HANDLE NEW PACK NAME
    # ==============================================
    async def handle_vid_new_pack_name(client: Client, message: Message, uid: int):
        display_title = message.text.strip()
        if not display_title:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
            return

        session = vid_sessions.get(uid)
        if not session:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /vid again."))
            return

        pack_index = get_next_pack_index(uid)
        session["display_title"] = display_title
        session["pack_index"] = pack_index
        session["step"] = "vid_waiting_media"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
        ])
        await reply_or_dm(
            client, message,
            small_caps(
                "✅ Pack name saved.\n\n"
                "Now send your MP4 video or Telegram Video Sticker (WEBM).\n\n"
                "Supported: MP4, WEBM Sticker"
            ),
            reply_markup=kb
        )

    # ==============================================
    # HANDLE TEXT INPUT
    # ==============================================
    async def handle_vid_text_input(client: Client, message: Message, uid: int):
        text = message.text.strip()
        if not text:
            await reply_or_dm(client, message, small_caps("❌ Please send valid text."))
            return

        session = vid_sessions.get(uid)
        if not session:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /vid again."))
            return

        session["text"] = text
        session["text_option_done"] = True
        session["step"] = "vid_preview"
        await send_vid_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")

    # ==============================================
    # HANDLE EMOJI INPUT
    # ==============================================
    async def handle_vid_emoji_input(client: Client, message: Message, uid: int):
        emoji = message.text.strip()
        if not emoji:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid emoji."))
            return

        session = vid_sessions.get(uid)
        if not session:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /vid again."))
            return

        session["emoji"] = emoji
        await save_vid_sticker(client, message.chat.id, message.from_user, uid)

    # ==============================================
    # SEND VID PREVIEW
    # ==============================================
    async def send_vid_preview(client, chat_id, from_user, uid, is_group=False):
        session = vid_sessions.get(uid)
        if not session or not session.get("video_path"):
            # BUG 2 FIX: send_vid_message was not defined - use client.send_message
            await client.send_message(chat_id, small_caps("Session Expired\nPlease run /vid again."))
            cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
            vid_sessions.pop(uid, None)
            return

        old_prev = session.pop("preview_message", None)
        if old_prev:
            try:
                await old_prev.delete()
            except Exception:
                pass

        # BUG 2 FIX: send_vid_message was not defined - use client.send_message
        status_msg = await client.send_message(chat_id, small_caps("⚙️ Generating Sticker..."))

        input_path = session["video_path"]
        overlay_text = session.get("text")

        cleanup_temp_files(session.get("preview_path"))
        output_preview = os.path.join(TEMP_DIR, f"vid_prev_{uid}_{uuid.uuid4().hex}.mp4")

        ok, err = await generate_vid_preview(input_path, output_preview, overlay_text)
        if not ok:
            await status_msg.edit_text(small_caps(f"❌ Failed to generate video preview: {err}"))
            return

        await status_msg.edit_text(small_caps("⚙️ Uploading..."))

        try:
            catbox_url = await upload_to_catbox(output_preview)
            session["catbox_url"] = catbox_url
            session["preview_path"] = output_preview
            session["step"] = "vid_preview"
        except Exception as e:
            await status_msg.edit_text(small_caps(f"❌ Catbox Upload Error: {str(e)}"))
            return

        try:
            await status_msg.delete()
        except Exception:
            pass

        pack_title = session.get("display_title", "Unknown Pack")
        text_val = session.get("text") or "None"

        caption = small_caps(
            f"📦 Pack: {pack_title}\n"
            f"📝 Text: {text_val}\n"
            f"🎭 Type: Video Sticker\n\n"
            f"🎬 Preview Generated!"
        )
        if is_group and from_user:
            mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
            caption = f"{mention}\n\n{caption}"

        rows = [
            [InlineKeyboardButton("✅ Save To Pack", callback_data="vidpreview_save"),
             InlineKeyboardButton("🔄 Replace Video", callback_data="vidpreview_replace")],
            [InlineKeyboardButton("✏ Edit Text", callback_data="vidpreview_edit"),
             InlineKeyboardButton("🗑 Remove Text", callback_data="vidpreview_removetext")],
        ]
        if catbox_url:
            rows.append([InlineKeyboardButton("👀 Watch Preview", url=catbox_url)])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")])
        kb = InlineKeyboardMarkup(rows)

        sent_msg = await client.send_video(chat_id=chat_id, video=output_preview, caption=caption, reply_markup=kb)
        session["preview_message"] = sent_msg

    # ==============================================
    # SAVE VIDEO STICKER
    # ==============================================
    async def save_vid_sticker(client, chat_id, from_user, uid):
        session = vid_sessions.get(uid)
        if not session:
            await client.send_message(chat_id, small_caps("Session Expired\nPlease run /vid again."))
            return

        pack_index = session.get("pack_index")
        display_title = session.get("display_title")
        emoji = session.get("emoji", DEFAULT_EMOJI)
        webm_path = session.get("webm_path")
        catbox_url = session.get("catbox_url")

        pack = get_pack_by_index(uid, pack_index)
        is_new_pack = pack is None
        telegram_pack_name = pack.get("telegram_pack_name") if pack else None

        proc = await client.send_message(chat_id, small_caps("⚙️ Uploading..."))

        if not webm_path or not os.path.exists(webm_path):
            cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
            vid_sessions.pop(uid, None)
            await proc.edit_text(small_caps("Session Expired\nPlease run /vid again."))
            return

        with open(webm_path, "rb") as f:
            video_bytes = f.read()

        if not telegram_pack_name:
            await proc.edit_text(small_caps("⚙️ Creating Pack..."))
            success, result, telegram_pack_name_new = await create_unique_video_sticker_pack(client, uid, display_title, video_bytes, emoji)
            if not success:
                cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
                vid_sessions.pop(uid, None)
                await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))
                return
            create_pack_record(uid, pack_index, display_title, telegram_pack_name_new, emoji, pack_type="video")
            packs_collection.update_one(
                {"user_id": str(uid), "pack_index": pack_index},
                {"$set": {"preview_video_url": catbox_url}}
            )
            telegram_pack_name = telegram_pack_name_new
        else:
            if not is_new_pack and pack.get("pack_type", "static") != "video":
                cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
                # BUG 3 FIX: Do NOT pop session on transient errors
                await proc.edit_text(small_caps("❌ This is a static sticker pack. Video stickers cannot be added."))
                return
            success, err = await add_video_sticker_to_pack(client, uid, telegram_pack_name, video_bytes, emoji)
            if not success:
                cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
                # BUG 3 FIX: Do NOT pop session on transient errors
                await proc.edit_text(small_caps(f"❌ Failed to add video sticker: {err}"))
                return

        if not is_new_pack:
            increment_pack_sticker_count(uid, pack_index)
        increment_sticker_count(uid)

        updated_pack = get_pack_by_index(uid, pack_index)
        total_count = updated_pack.get("total_stickers", 1) if updated_pack else 1

        cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
        vid_sessions.pop(uid, None)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Open Pack", url=f"https://t.me/addstickers/{telegram_pack_name}")],
            [InlineKeyboardButton("➕ Add Another", callback_data=f"vid_add_another_{pack_index}"),
             InlineKeyboardButton("🏠 Main Menu", callback_data="vid_main_menu")]
        ])
        await proc.edit_text(
            small_caps(
                f"✅ Video Sticker Added Successfully\n\n"
                f"📦 Pack Name: {display_title}\n"
                f"🆔 Pack Index: {pack_index}\n"
                f"🎭 Sticker Type: Video Sticker\n"
                f"😀 Emoji: {emoji}\n"
                f"🎉 Total Stickers: {total_count}"
            ),
            reply_markup=kb
        )

    # ==============================================
    # CALLBACK: Destination (New / Existing)
    # ==============================================
    @app.on_callback_query(filters.regex(r"^viddest_"))
    async def vid_destination_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_destination":
            await callback.answer(small_caps("Session Expired\nPlease run /vid again."), show_alert=True)
            return

        choice = callback.data.replace("viddest_", "")
        await callback.answer()

        if choice == "cancel":
            await cancel_user_session(client, user_id=uid, callback=callback)
            return

        if choice == "new":
            vid_sessions[uid]["step"] = "vid_waiting_pack_name"
            await callback.message.edit_text(
                small_caps("Please send a name for your new video sticker pack.")
            )
            return

        packs = get_user_packs(uid)
        video_packs = [p for p in packs if p.get("pack_type") == "video"]

        if not video_packs:
            session = vid_sessions.pop(uid, None)
            if session:
                cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
            await callback.message.edit_text(small_caps("❌ You have no video sticker packs yet. Select New Pack to create one."))
            return

        if len(video_packs) == 1:
            pack = video_packs[0]
            vid_sessions[uid]["pack_index"] = pack["pack_index"]
            vid_sessions[uid]["display_title"] = pack["display_title"]
            vid_sessions[uid]["step"] = "vid_waiting_media"

            chat_id = callback.message.chat.id
            is_group = callback.message.chat.type != "private"
            try:
                await callback.message.delete()
            except Exception:
                pass

            text = small_caps(
                "Now send your MP4 video or Telegram Video Sticker (WEBM).\n\n"
                "Supported: MP4, WEBM Sticker"
            )
            if is_group:
                mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
                text = f"{mention}\n\n{text}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
            ])
            await client.send_message(chat_id, text, reply_markup=kb)
            return

        rows = []
        row = []
        for pack in video_packs:
            row.append(InlineKeyboardButton(f"{pack['display_title']}", callback_data=f"vidpack_{pack['pack_index']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")])

        vid_sessions[uid]["step"] = "vid_waiting_pack_selection"
        await callback.message.edit_text(
            small_caps("📦 Select Your Pack"),
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ==============================================
    # CALLBACK: Select existing pack
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vidpack_"))
    async def vidpack_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_pack_selection":
            await callback.answer(small_caps("Session Expired\nPlease run /vid again."), show_alert=True)
            return

        pack_index = int(callback.data.replace("vidpack_", ""))
        pack = get_pack_by_index(uid, pack_index)
        if not pack:
            await callback.answer(small_caps("Invalid pack."), show_alert=True)
            return

        await callback.answer()
        vid_sessions[uid]["pack_index"] = pack_index
        vid_sessions[uid]["display_title"] = pack["display_title"]
        vid_sessions[uid]["step"] = "vid_waiting_media"

        try:
            await callback.message.delete()
        except Exception:
            pass

        chat_id = callback.message.chat.id
        is_group = callback.message.chat.type != "private"
        text = small_caps(
            "Now send your MP4 video or Telegram Video Sticker (WEBM).\n\n"
            "Supported: MP4, WEBM Sticker"
        )
        if is_group:
            mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
            text = f"{mention}\n\n{text}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
        ])
        await client.send_message(chat_id, text, reply_markup=kb)

    # ==============================================
    # CALLBACK: Text option (add/skip)
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vidtext_"))
    async def vidtext_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_text_option":
            await callback.answer(small_caps("Session Expired\nPlease run /vid again."), show_alert=True)
            return

        choice = callback.data.replace("vidtext_", "")
        await callback.answer()
        session = vid_sessions[uid]

        if choice == "skip":
            session["text"] = None
            session["text_option_done"] = True
            session["step"] = "vid_preview"
            await send_vid_preview(client, callback.message.chat.id, callback.from_user, uid, is_group=callback.message.chat.type != "private")
            return

        if choice == "add":
            session["step"] = "vid_waiting_text_input"
            await callback.message.edit_text(small_caps("Send your text."))
            return

    # ==============================================
    # CALLBACK: Preview actions
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vidpreview_"))
    async def vidpreview_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_preview":
            await callback.answer(small_caps("Session Expired\nPlease run /vid again."), show_alert=True)
            return

        choice = callback.data.replace("vidpreview_", "")
        await callback.answer()
        session = vid_sessions[uid]

        if choice == "cancel":
            await cancel_user_session(client, user_id=uid, callback=callback)
            return

        if choice == "removetext":
            session["text"] = None
            await send_vid_preview(client, callback.message.chat.id, callback.from_user, uid, is_group=callback.message.chat.type != "private")
            return

        if choice == "edit":
            session["step"] = "vid_editing_text_input"
            await callback.message.reply_text(small_caps("Send your text."))
            return

        if choice == "replace":
            session["step"] = "vid_waiting_media"
            await callback.message.reply_text(
                small_caps("Send another MP4 video or WEBM Video Sticker."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
                ])
            )
            return

        if choice == "save":
            if session.get("emoji"):
                await save_vid_sticker(client, callback.message.chat.id, callback.from_user, uid)
            else:
                session["step"] = "vid_waiting_emoji"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("😀 Default Emoji", callback_data="videmoji_default"),
                     InlineKeyboardButton("✏ Custom Emoji", callback_data="videmoji_custom")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
                ])
                await callback.message.reply_text(small_caps("Choose an emoji for this video sticker."), reply_markup=kb)
            return

    # ==============================================
    # CALLBACK: Emoji selection
    # ==============================================
    @app.on_callback_query(filters.regex(r"^videmoji_"))
    async def videmoji_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in vid_sessions or vid_sessions[uid].get("step") != "vid_waiting_emoji":
            await callback.answer(small_caps("Session Expired\nPlease run /vid again."), show_alert=True)
            return

        choice = callback.data.replace("videmoji_", "")
        await callback.answer()
        session = vid_sessions[uid]

        if choice == "default":
            user_pack_info = get_user_pack(uid)
            session["emoji"] = user_pack_info.get("emoji", DEFAULT_EMOJI) or DEFAULT_EMOJI
            await save_vid_sticker(client, callback.message.chat.id, callback.from_user, uid)
        else:
            session["step"] = "vid_waiting_emoji_text"
            await callback.message.edit_text(small_caps("Send your custom emoji."))

    # ==============================================
    # CALLBACK: Cancel
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vidflow_cancel$"))
    async def vid_flow_cancel_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        await cancel_user_session(client, user_id=uid, callback=callback)

    # ==============================================
    # CALLBACK: Add another
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vid_add_another_"))
    async def vid_add_another_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        pack_index = int(callback.data.replace("vid_add_another_", ""))
        pack = get_pack_by_index(uid, pack_index)

        if not pack:
            await callback.answer(small_caps("Pack not found."), show_alert=True)
            return

        await callback.answer()
        vid_sessions[uid] = {
            "step": "vid_waiting_media",
            "pack_index": pack_index,
            "display_title": pack["display_title"],
            "emoji": pack.get("emoji", DEFAULT_EMOJI),
            "started_at": time.time()
        }

        try:
            await callback.message.delete()
        except Exception:
            pass

        chat_id = callback.message.chat.id
        is_group = callback.message.chat.type != "private"
        text = small_caps("Send another MP4 or WEBM Sticker.")
        if is_group:
            mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
            text = f"{mention}\n\n{text}"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="vidflow_cancel")]
        ])
        await client.send_message(chat_id, text, reply_markup=kb)

    # ==============================================
    # CALLBACK: Main menu
    # ==============================================
    @app.on_callback_query(filters.regex(r"^vid_main_menu$"))
    async def vid_main_menu_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid in vid_sessions:
            session = vid_sessions.pop(uid, None)
            if session:
                cleanup_temp_files(session.get("video_path"), session.get("webm_path"), session.get("preview_path"))
        await callback.answer()
        await callback.message.edit_text(
            small_caps("👋 Returned to main menu. Use /help to see available commands.")
        )
