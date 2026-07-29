def register(app, fsub):
    from stick import (
        small_caps, reply_or_dm, plain_sessions, TEMP_DIR, DEFAULT_EMOJI,
        BOT_USERNAME, get_user_packs, get_pack_by_index, get_user_pack,
        get_next_pack_index, create_pack_record, create_unique_sticker_pack,
        create_unique_video_sticker_pack, add_sticker_to_pack,
        add_video_sticker_to_pack, increment_pack_sticker_count,
        increment_sticker_count, normalize_video_sticker, cleanup_temp_files,
        probe_video_duration, prepare_plain_image_sticker, convert_webp_to_png,
        normalize_sticker_png, cancel_user_session, MAX_PREVIEW_SECONDS,
        DURATION_TOLERANCE, register_user_command, CATEGORY_STICKER,
        generate_telegram_pack_name, validate_telegram_pack_name
    )
    import os, uuid, time
    from io import BytesIO
    from pyrogram import Client, filters, StopPropagation
    from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

    # ==============================================
    # COMMAND: /plain
    # ==============================================
    @app.on_message(filters.command("plain") & (filters.private | filters.group))
    async def plain_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        uid = message.from_user.id
        if uid in plain_sessions:
            cleanup_temp_files(plain_sessions[uid].get("media_path"))
            plain_sessions.pop(uid, None)
        plain_sessions[uid] = {"step": "plain_waiting_destination", "started_at": time.time()}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 New Pack", callback_data="plaindest_new")],
            [InlineKeyboardButton("📦 Existing Pack", callback_data="plaindest_existing")],
            [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
        ])
        await reply_or_dm(
            client, message,
            small_caps("🖼 **Create Sticker Without Text**\n\nWhere would you like to save your sticker?"),
            reply_markup=kb
        )
    register_user_command("plain", "Create a sticker without adding text.", category=CATEGORY_STICKER)

    # ==============================================
    # HANDLE MEDIA (/plain Flow)
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
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /plain again."))
            cleanup_temp_files(session.get("media_path"))
            plain_sessions.pop(uid, None)
            return

        pack = get_pack_by_index(uid, pack_index)

        # BUG 1 FIX: Only validate pack type for EXISTING packs.
        # For new packs (pack is None), accept any media type.
        if pack is not None:
            existing_pack_type = pack.get("pack_type", "static")
        else:
            existing_pack_type = None

        is_video_input = False
        is_sticker_input = False
        file_id = None

        if message.sticker:
            if message.sticker.is_animated:
                await reply_or_dm(client, message, small_caps("❌ Animated .TGS stickers are not supported."))
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
                await reply_or_dm(client, message, small_caps("❌ Unsupported file type. Send a Photo, PNG, WEBP, Static Sticker, MP4 Video, or WEBM Sticker."))
                return
        else:
            await reply_or_dm(client, message, small_caps("❌ Unsupported media. Send a Photo, PNG, WEBP, Static Sticker, MP4 Video, or WEBM Sticker."))
            return

        if pack is not None:
            if existing_pack_type == "static" and is_video_input:
                await reply_or_dm(client, message, small_caps("❌ This is a static sticker pack. Video stickers cannot be added. Use /plain with an image instead."))
                return
            if existing_pack_type == "video" and not is_video_input:
                await reply_or_dm(client, message, small_caps("❌ This is a video sticker pack. Static stickers cannot be added. Use /plain with a video instead."))
                return

        old_preview = session.pop("preview_message", None)
        if old_preview:
            try:
                await old_preview.delete()
            except Exception:
                pass

        proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading..."))

        try:
            if is_video_input:
                media_path = os.path.join(TEMP_DIR, f"plain_vid_{uid}_{uuid.uuid4().hex}.webm")
                temp_input = os.path.join(TEMP_DIR, f"plain_vid_in_{uid}_{uuid.uuid4().hex}.mp4")
                await client.download_media(message, file_name=temp_input)

                file_name_lower = (getattr(message.video or message.document, "file_name", "") or "").lower()
                mime_lower = (getattr(message.video or message.document, "mime_type", "") or "").lower()

                await proc.edit_text(small_caps("⚙️ Processing..."))

                if file_name_lower.endswith(".webm") or "webm" in mime_lower:
                    if message.sticker and message.sticker.is_video:
                        duration = await probe_video_duration(temp_input)
                        if duration and duration <= MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
                            sticker_size = os.path.getsize(temp_input)
                            if sticker_size <= 256 * 1024:
                                os.rename(temp_input, media_path)
                                session["media_path"] = media_path
                                session["media_type"] = "video"
                                await proc.edit_text(small_caps("⚙️ Generating Sticker..."))
                                if session.get("emoji"):
                                    session["step"] = "plain_preview"
                                    await proc.delete()
                                    await send_plain_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")
                                else:
                                    session["step"] = "plain_waiting_emoji"
                                    await proc.edit_text(
                                        small_caps("Choose an emoji for this sticker."),
                                        reply_markup=InlineKeyboardMarkup([
                                            [InlineKeyboardButton("😀 Default Emoji", callback_data="plainemoji_default"),
                                             InlineKeyboardButton("✏ Custom Emoji", callback_data="plainemoji_send")],
                                            [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
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
                        await proc.edit_text(small_caps("❌ Video probe failed: Invalid codec, unsupported media format, or corrupted file."))
                        return
                    if duration > MAX_PREVIEW_SECONDS + DURATION_TOLERANCE:
                        cleanup_temp_files(temp_input)
                        await proc.edit_text(small_caps(f"❌ Video is too long ({duration:.1f}s). Maximum allowed duration is 3 seconds."))
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
                await proc.edit_text(small_caps("⚙️ Processing..."))
                sticker_bytes = prepare_plain_image_sticker(img_bytes)
                session["media_bytes"] = sticker_bytes
                session["media_type"] = "static"

            await proc.edit_text(small_caps("⚙️ Generating Sticker..."))
            if session.get("emoji"):
                session["step"] = "plain_preview"
                await proc.delete()
                await send_plain_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")
            else:
                session["step"] = "plain_waiting_emoji"
                await proc.edit_text(
                    small_caps("Choose an emoji for this sticker."),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("😀 Default Emoji", callback_data="plainemoji_default"),
                         InlineKeyboardButton("✏ Custom Emoji", callback_data="plainemoji_send")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
                    ])
                )
        except Exception as e:
            cleanup_temp_files(session.get("media_path"))
            await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))
            # BUG 3 FIX: Do NOT pop session on transient errors

    # ==============================================
    # TEXT ROUTING FOR /plain
    # ==============================================
    @app.on_message(filters.text & (filters.private | filters.group), group=4)
    async def handle_plain_text(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        if uid not in plain_sessions:
            return
        step = plain_sessions[uid].get("step")
        if step == "plain_waiting_pack_name":
            await handle_plain_new_pack_name(client, message, uid)
        elif step in ("plain_waiting_emoji", "plain_waiting_emoji_text", "plain_waiting_emoji_change"):
            await handle_plain_emoji_input(client, message, uid)

    # ==============================================
    # HANDLE NEW PACK NAME
    # ==============================================
    async def handle_plain_new_pack_name(client: Client, message: Message, uid: int):
        display_title = message.text.strip()
        if not display_title:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
            return

        session = plain_sessions.get(uid)
        if not session:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /plain again."))
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
                "✅ Pack name saved.\n\n"
                "Now send your media.\n\n"
                "Supported media:\n"
                "• Photo\n• PNG\n• WEBP Image\n• Static Sticker\n"
                "• MP4 Video\n• WEBM Sticker"
            ),
            reply_markup=kb
        )

    # ==============================================
    # HANDLE EMOJI INPUT
    # ==============================================
    async def handle_plain_emoji_input(client: Client, message: Message, uid: int):
        emoji = message.text.strip()
        if not emoji:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid emoji."))
            return

        session = plain_sessions.get(uid)
        if not session:
            await reply_or_dm(client, message, small_caps("Session Expired\nPlease run /plain again."))
            return

        session["emoji"] = emoji
        session["step"] = "plain_preview"
        await send_plain_preview(client, message.chat.id, message.from_user, uid, is_group=message.chat.type != "private")

    # ==============================================
    # SEND PLAIN PREVIEW
    # ==============================================
    async def send_plain_preview(client, chat_id, from_user, uid, is_group=False):
        session = plain_sessions.get(uid)
        if not session:
            return

        pack_title = session.get("display_title", "Unknown Pack")
        emoji = session.get("emoji", DEFAULT_EMOJI)
        media_type = session.get("media_type", "static")
        sticker_type_str = "Video Sticker" if media_type == "video" else "Static Sticker"
        pack_type_str = "Video Pack" if media_type == "video" else "Static Pack"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Add To Pack", callback_data="plainpreview_save"),
             InlineKeyboardButton("🔄 Replace Media", callback_data="plainpreview_replace")],
            [InlineKeyboardButton("😀 Change Emoji", callback_data="plainpreview_emoji"),
             InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
        ])

        caption = small_caps(
            f"📦 Pack: {pack_title}\n"
            f"📁 Pack Type: {pack_type_str}\n"
            f"😀 Emoji: {emoji}\n"
            f"🎭 Sticker Type: {sticker_type_str}\n\n"
            f"🎨 Sticker Preview"
        )
        if is_group and from_user:
            mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
            caption = f"{mention}\n\n{caption}"

        if media_type == "static":
            sticker_bytes = session.get("media_bytes")
            if not sticker_bytes:
                return
            photo_file = BytesIO(sticker_bytes)
            photo_file.name = "preview.png"
            sent_msg = await client.send_photo(chat_id=chat_id, photo=photo_file, caption=caption, reply_markup=kb)
            session["preview_message"] = sent_msg
        else:
            media_path = session.get("media_path")
            if not media_path or not os.path.exists(media_path):
                return
            sent_msg = await client.send_video(chat_id=chat_id, video=media_path, caption=caption, reply_markup=kb)
            session["preview_message"] = sent_msg

    # ==============================================
    # CALLBACK: Destination (New / Existing)
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plaindest_"))
    async def plain_destination_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_destination":
            await callback.answer(small_caps("Session Expired\nPlease run /plain again."), show_alert=True)
            return

        choice = callback.data.replace("plaindest_", "")
        await callback.answer()

        if choice == "cancel":
            await cancel_user_session(client, user_id=uid, callback=callback)
            return

        if choice == "new":
            plain_sessions[uid]["step"] = "plain_waiting_pack_name"
            await callback.message.edit_text(
                small_caps("Please send a name for your new sticker pack.")
            )
            return

        packs = get_user_packs(uid)
        if not packs:
            session = plain_sessions.pop(uid, None)
            if session:
                cleanup_temp_files(session.get("media_path"))
            await callback.message.edit_text(small_caps("❌ You have no sticker packs yet. Create one with /sticker or /plain."))
            return

        if len(packs) == 1:
            pack = packs[0]
            plain_sessions[uid]["pack_index"] = pack["pack_index"]
            plain_sessions[uid]["display_title"] = pack["display_title"]
            plain_sessions[uid]["step"] = "plain_waiting_media"

            chat_id = callback.message.chat.id
            is_group = callback.message.chat.type != "private"
            try:
                await callback.message.delete()
            except Exception:
                pass

            text = small_caps(
                "Now send your media.\n\n"
                "Supported media:\n"
                "• Photo\n• PNG\n• WEBP Image\n• Static Sticker\n"
                "• MP4 Video\n• WEBM Sticker"
            )
            if is_group:
                mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
                text = f"{mention}\n\n{text}"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
            ])
            await client.send_message(chat_id, text, reply_markup=kb)
            return

        rows = []
        row = []
        for pack in packs:
            row.append(InlineKeyboardButton(f"{pack['display_title']}", callback_data=f"plainpack_{pack['pack_index']}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")])

        plain_sessions[uid]["step"] = "plain_waiting_pack_selection"
        await callback.message.edit_text(
            small_caps("📦 Select Your Pack"),
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # ==============================================
    # CALLBACK: Select existing pack
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plainpack_"))
    async def plain_pack_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_pack_selection":
            await callback.answer(small_caps("Session Expired\nPlease run /plain again."), show_alert=True)
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
            "Now send your media.\n\n"
            "Supported media:\n"
            "• Photo\n• PNG\n• WEBP Image\n• Static Sticker\n"
            "• MP4 Video\n• WEBM Sticker"
        )
        if is_group:
            mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
            text = f"{mention}\n\n{text}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
        ])
        await client.send_message(chat_id, text, reply_markup=kb)

    # ==============================================
    # CALLBACK: Cancel
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plainflow_cancel$"))
    async def plain_flow_cancel_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        await cancel_user_session(client, user_id=uid, callback=callback)

    # ==============================================
    # CALLBACK: Emoji selection
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plainemoji_"))
    async def plain_emoji_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_waiting_emoji":
            await callback.answer(small_caps("Session Expired\nPlease run /plain again."), show_alert=True)
            return

        choice = callback.data.replace("plainemoji_", "")
        await callback.answer()

        session = plain_sessions[uid]
        if choice == "default":
            user_pack_info = get_user_pack(uid)
            session["emoji"] = user_pack_info.get("emoji", DEFAULT_EMOJI) or DEFAULT_EMOJI
        else:
            session["step"] = "plain_waiting_emoji_text"
            await callback.message.edit_text(small_caps("Send your emoji."))
            return

        session["step"] = "plain_preview"
        is_group = callback.message.chat.type != "private"
        await send_plain_preview(client, callback.message.chat.id, callback.from_user, uid, is_group=is_group)

    # ==============================================
    # CALLBACK: Preview actions (save, replace, emoji)
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plainpreview_"))
    async def plain_preview_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid not in plain_sessions or plain_sessions[uid].get("step") != "plain_preview":
            await callback.answer(small_caps("Session Expired\nPlease run /plain again."), show_alert=True)
            return

        choice = callback.data.replace("plainpreview_", "")
        await callback.answer()
        session = plain_sessions[uid]

        if choice == "cancel":
            await cancel_user_session(client, user_id=uid, callback=callback)
            return

        if choice == "replace":
            session["step"] = "plain_waiting_media"
            await callback.message.reply_text(
                small_caps("Send another supported image or video."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
                ])
            )
            return

        if choice == "emoji":
            session["step"] = "plain_waiting_emoji_change"
            await callback.message.reply_text(small_caps("Send your new emoji."))
            return

        if choice == "save":
            pack_index = session.get("pack_index")
            display_title = session.get("display_title")
            emoji = session.get("emoji", DEFAULT_EMOJI)
            media_type = session.get("media_type")
            pack = get_pack_by_index(uid, pack_index)

            is_new_pack = pack is None
            telegram_pack_name = pack.get("telegram_pack_name") if pack else None

            proc = await callback.message.reply_text(small_caps("⚙️ Uploading..."))

            if media_type == "static":
                sticker_bytes = session.get("media_bytes")
                if not sticker_bytes:
                    cleanup_temp_files(session.get("media_path"))
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps("Session Expired\nPlease run /plain again."))
                    return

                if not telegram_pack_name:
                    pack_type = "static"
                    test_name = generate_telegram_pack_name(display_title, BOT_USERNAME)
                    if validate_telegram_pack_name(test_name):
                        final_title = display_title
                    else:
                        print(f"[PACK NAME] Original: {display_title}")
                        print(f"[PACK NAME] Bot Username: {BOT_USERNAME}")
                        print(f"[PACK NAME] Generated: {test_name}")
                        print(f"[PACK NAME] Invalid - using safe fallback")
                        final_title = "Sticker Pack"
                    await proc.edit_text(small_caps("⚙️ Creating Pack..."))
                    success, result, telegram_pack_name_new = await create_unique_sticker_pack(client, uid, final_title, sticker_bytes, emoji)
                    if not success:
                        cleanup_temp_files(session.get("media_path"))
                        plain_sessions.pop(uid, None)
                        await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))
                        return
                    create_pack_record(uid, pack_index, display_title, telegram_pack_name_new, emoji, pack_type=pack_type)
                    telegram_pack_name = telegram_pack_name_new
                else:
                    if not is_new_pack and pack.get("pack_type", "static") != "static":
                        cleanup_temp_files(session.get("media_path"))
                        # BUG 3 FIX: Do NOT pop session - user can retry
                        await proc.edit_text(small_caps("❌ This is a video sticker pack. Static stickers cannot be added."))
                        return
                    success, err = await add_sticker_to_pack(client, uid, telegram_pack_name, sticker_bytes, emoji)
                    if not success:
                        cleanup_temp_files(session.get("media_path"))
                        # BUG 3 FIX: Do NOT pop session on transient errors
                        await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))
                        return
            else:
                media_path = session.get("media_path")
                if not media_path or not os.path.exists(media_path):
                    plain_sessions.pop(uid, None)
                    await proc.edit_text(small_caps("Session Expired\nPlease run /plain again."))
                    return

                if not telegram_pack_name:
                    pack_type = "video"
                    test_name = generate_telegram_pack_name(display_title, BOT_USERNAME)
                    if validate_telegram_pack_name(test_name):
                        final_title = display_title
                    else:
                        print(f"[PACK NAME] Original: {display_title}")
                        print(f"[PACK NAME] Bot Username: {BOT_USERNAME}")
                        print(f"[PACK NAME] Generated: {test_name}")
                        print(f"[PACK NAME] Invalid - using safe fallback")
                        final_title = "Sticker Pack"
                    await proc.edit_text(small_caps("⚙️ Creating Pack..."))
                    with open(media_path, "rb") as f:
                        video_bytes = f.read()
                    success, result, telegram_pack_name_new = await create_unique_video_sticker_pack(client, uid, final_title, video_bytes, emoji)
                    if not success:
                        cleanup_temp_files(media_path)
                        plain_sessions.pop(uid, None)
                        await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))
                        return
                    create_pack_record(uid, pack_index, display_title, telegram_pack_name_new, emoji, pack_type=pack_type)
                    telegram_pack_name = telegram_pack_name_new
                else:
                    if not is_new_pack and pack.get("pack_type", "static") != "video":
                        cleanup_temp_files(media_path)
                        # BUG 3 FIX: Do NOT pop session
                        await proc.edit_text(small_caps("❌ This is a static sticker pack. Video stickers cannot be added."))
                        return
                    with open(media_path, "rb") as f:
                        video_bytes = f.read()
                    success, err = await add_video_sticker_to_pack(client, uid, telegram_pack_name, video_bytes, emoji)
                    if not success:
                        cleanup_temp_files(media_path)
                        # BUG 3 FIX: Do NOT pop session on transient errors
                        await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))
                        return

            if not is_new_pack:
                increment_pack_sticker_count(uid, pack_index)
            increment_sticker_count(uid)

            updated_pack = get_pack_by_index(uid, pack_index)
            total_count = updated_pack.get("total_stickers", 1) if updated_pack else 1
            sticker_type_str = "Video Sticker" if media_type == "video" else "Static Sticker"

            cleanup_temp_files(session.get("media_path"))
            plain_sessions.pop(uid, None)

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌟 Open Pack", url=f"https://t.me/addstickers/{telegram_pack_name}")],
                [InlineKeyboardButton("➕ Add Another", callback_data=f"plain_add_another_{pack_index}"),
                 InlineKeyboardButton("🏠 Main Menu", callback_data="plain_main_menu")]
            ])
            await proc.edit_text(
                small_caps(
                    f"✅ Sticker Added Successfully\n\n"
                    f"📦 Pack Name: {display_title}\n"
                    f"🆔 Pack Index: {pack_index}\n"
                    f"🎭 Sticker Type: {sticker_type_str}\n"
                    f"😀 Emoji: {emoji}\n"
                    f"🎉 Total Stickers: {total_count}"
                ),
                reply_markup=kb
            )

    # ==============================================
    # CALLBACK: Add another
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plain_add_another_"))
    async def plain_add_another_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        pack_index = int(callback.data.replace("plain_add_another_", ""))
        pack = get_pack_by_index(uid, pack_index)

        if not pack:
            await callback.answer(small_caps("Pack not found."), show_alert=True)
            return

        await callback.answer()
        plain_sessions[uid] = {
            "step": "plain_waiting_media",
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
        text = small_caps("Send another image or video.")
        if is_group:
            mention = f"[{callback.from_user.first_name}](tg://user?id={callback.from_user.id})"
            text = f"{mention}\n\n{text}"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="plainflow_cancel")]
        ])
        await client.send_message(chat_id, text, reply_markup=kb)

    # ==============================================
    # CALLBACK: Main menu
    # ==============================================
    @app.on_callback_query(filters.regex(r"^plain_main_menu$"))
    async def plain_main_menu_callback(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        if uid in plain_sessions:
            session = plain_sessions.pop(uid, None)
            if session:
                cleanup_temp_files(session.get("media_path"))
        await callback.answer()
        await callback.message.edit_text(
            small_caps("👋 Returned to main menu. Use /help to see available commands.")
        )
