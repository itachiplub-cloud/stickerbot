# -*- coding: utf-8 -*-
"""
force_subscribe.py
===================
Universal "Must Join" / Force-Subscribe module for Pyrogram bots.
Drop this file into ANY Pyrogram + MongoDB project and wire it up in 3 lines.

WHAT IT DOES
------------
- Blocks users from using the bot until they've joined every required
  channel/group.
- Required channels can be pre-seeded via a .env variable, AND the bot
  admin(s) can add/remove more channels at runtime with no restart needed.
- When a user is missing a join, they get a message with one button per
  channel (auto-resolved invite link) + an "✅ I've Joined" recheck button.
- Admins are always exempt.
- If the bot isn't an admin in a target chat (so it can't verify members),
  that one channel is skipped instead of locking every user out.

SETUP
-----
1. Add to your .env:
       FORCE_SUB_CHANNELS=@yourchannel,@yourgroup
   (comma-separated usernames or chat IDs. Can be left empty and managed
   entirely via admin commands instead.)

2. In your main bot file:

       from force_subscribe import ForceSubscribe

       fsub = ForceSubscribe(app, db, admin_ids=[ADMIN_USER_ID])

3. At the top of any handler you want gated:

       @app.on_message(filters.command("sticker"))
       async def sticker_start(client, message):
           if not await fsub.check(client, message):
               return
           ... your existing logic ...

That's it. Admins can now run /addchannel, /removechannel, /channels
to manage the list live.

/addchannel INPUT FORMATS (upgraded)
-------------------------------------
/addchannel now accepts three kinds of channel references, plus two
optional flags, in any order after the reference:

    /addchannel <@username | https://t.me/... link | numeric chat id> \
                [--request] [--button="Custom Label"] [Optional Title]

  • @username or bare username           -> public channel/group
  • https://t.me/username                -> normalized to @username
  • https://t.me/+HASH or /joinchat/HASH  -> private invite link; the bot
    will attempt to JOIN that chat via the link to resolve its chat ID.
    If the bot is already a member and can't re-join, use /getid inside
    that chat (or /mychats) to grab its ID and pass that instead.
  • numeric chat id (e.g. -1001234567890) -> used as-is, same as before

  --request              Mark this channel as "Request to Join". The bot
                          will generate an approval-required invite link
                          instead of a direct-join one.
  --button="Text"         Custom label for this channel's join button in
                          the "must join" prompt. Falls back to
                          "➡️ Join <title>" (or "🔒 Request to Join <title>"
                          for --request channels) if omitted.

Examples:
    /addchannel @MyChannel
    /addchannel https://t.me/+AbCdEf12345 --request My Private Group
    /addchannel -1001234567890 --button="🎬 Join Movies"

/removechannel now also matches by invite link, not just the exact
stored ref, so you can paste back whatever you originally used to add it.

FINDING CHAT IDs
-----------------------
You no longer need to hunt for chat IDs by hand:

- /mychats  -> lists every chat the bot is CURRENTLY an admin in, with its
               ID/username, ready to paste straight into /addchannel.
               (Only tracks chats joined/promoted AFTER this feature was
               installed -- see /getid below for older ones.)

- /getid    -> run this directly inside a group, or post it in a channel
               (typed by an admin account, not forwarded). The bot replies
               with that chat's ID, type, title, and the exact ref to use
               with /addchannel. Works even if the bot isn't admin there
               yet or joined before /mychats existed.

IMPORTANT: the bot must be an ADMIN in every channel/group you want to
gate on, both to verify membership and to auto-generate invite links for
private chats. Public channels work even without admin rights for the
invite link (falls back to https://t.me/<username>), but membership
checks still need the bot to be a member/admin of that chat.
"""

import os
import re
from pyrogram import filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from pyrogram.errors import UserNotParticipant

# ------------------------------------------------------------------
# Recognized https://t.me/... link shapes for /addchannel parsing.
# ------------------------------------------------------------------
_INVITE_LINK_RE = re.compile(r'(?:https?://)?t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+', re.IGNORECASE)
_USERNAME_LINK_RE = re.compile(r'(?:https?://)?t\.me/([A-Za-z]\w{3,31})/?$', re.IGNORECASE)
_BARE_USERNAME_RE = re.compile(r'^[A-Za-z]\w{3,31}$')


class ForceSubscribe:
    def __init__(self, app, db, admin_ids, collection_name="force_sub_channels",
                 env_var="FORCE_SUB_CHANNELS"):
        """
        app          : your pyrogram Client instance
        db           : your pymongo database handle (e.g. `db` in stick.py)
        admin_ids    : list[int] of Telegram user IDs allowed to manage channels
                       and who are always exempt from the check
        """
        if not admin_ids:
            raise ValueError("ForceSubscribe requires at least one admin_id.")

        self.app = app
        self.col = db[collection_name]

        cleaned_ids = []
        for a in admin_ids:
            try:
                cleaned_ids.append(int(a))
            except (TypeError, ValueError):
                raise ValueError(
                    f"ForceSubscribe got an invalid admin_id: {a!r}. "
                    "Check that ADMIN_USER_ID is set correctly in your .env "
                    "(a plain numeric Telegram user ID, e.g. ADMIN_USER_ID=805508459)."
                )
        if not cleaned_ids:
            raise ValueError("ForceSubscribe requires at least one valid admin_id.")
        self.admin_ids = cleaned_ids

        self.col.create_index("ref", unique=True)

        # Tracks every chat (group/channel) the bot is currently a member of,
        # and whether it's an admin there. Populated live via chat_member
        # updates -- see _register_chat_tracking(). This is what powers
        # /mychats, so you don't have to hunt down chat IDs by hand.
        self.chats_col = db["bot_known_chats"]
        self.chats_col.create_index("chat_id", unique=True)

        self._seed_from_env(env_var)
        self._register_admin_commands()
        self._register_recheck_callback()
        self._register_chat_tracking()
        self._register_utility_commands()

    # ------------------------------------------------------------------
    # SETUP HELPERS
    # ------------------------------------------------------------------
    def _seed_from_env(self, env_var):
        raw = os.getenv(env_var, "").strip()
        if not raw:
            return
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                self.col.update_one(
                    {"ref": entry},
                    {"$setOnInsert": {"ref": entry, "title": entry,
                                       "invite_link": None, "added_by": "env"}},
                    upsert=True
                )
            except Exception:
                pass

    @staticmethod
    def _to_client_ref(ref: str):
        """Convert a stored ref string to what pyrogram expects: int id or username."""
        stripped = ref.lstrip("-")
        if stripped.isdigit():
            return int(ref)
        return ref

    @staticmethod
    def _classify_ref(raw: str):
        """
        Classify a raw admin-typed channel reference for /addchannel.
        Returns (kind, value):
          "username"     -> value is "@username"
          "invite_link"  -> value is the raw https://t.me/+... or /joinchat/... link
          "chat_id"      -> value is the numeric id string (e.g. "-1001234567890")
        Returns (None, raw) if nothing recognizable matched.
        """
        raw = raw.strip()

        if _INVITE_LINK_RE.search(raw):
            return "invite_link", raw

        m = _USERNAME_LINK_RE.match(raw)
        if m:
            return "username", f"@{m.group(1)}"

        stripped = raw.lstrip("-")
        if stripped.isdigit():
            return "chat_id", raw

        if raw.startswith("@") and _BARE_USERNAME_RE.match(raw[1:]):
            return "username", raw

        if _BARE_USERNAME_RE.match(raw):
            return "username", f"@{raw}"

        return None, raw

    def _channels(self):
        return list(self.col.find())

    # ------------------------------------------------------------------
    # CORE CHECK
    # ------------------------------------------------------------------
    async def check(self, client, message_or_callback) -> bool:
        """
        Call this first in any handler you want gated.
        Returns True  -> user is clear, proceed normally.
        Returns False -> user was shown a "must join" prompt; bail out.
        """
        user = message_or_callback.from_user
        if not user:
            return True

        uid = user.id
        if uid in self.admin_ids:
            return True

        channels = self._channels()
        if not channels:
            return True

        not_joined = []
        for ch in channels:
            client_ref = self._to_client_ref(ch["ref"])
            try:
                member = await client.get_chat_member(client_ref, uid)
                if member.status in ("kicked", "banned", "left"):
                    not_joined.append(ch)
            except UserNotParticipant:
                not_joined.append(ch)
            except Exception as e:
                # Bot can't verify this chat (not admin / invalid / etc).
                # Skip it rather than locking out every user over one
                # misconfigured channel -- but LOG it, so a broken channel
                # doesn't fail silently forever.
                print(f"[force_subscribe] Could not check membership for "
                      f"'{ch['ref']}' (uid={uid}): {type(e).__name__}: {e}")
                continue

        if not not_joined:
            return True

        buttons = []
        for ch in not_joined:
            link = await self._get_invite_link(client, ch)
            if not link:
                continue
            title = ch.get("title") or ch["ref"]
            custom_text = ch.get("button_text")
            if custom_text:
                label = custom_text
            elif ch.get("join_mode") == "request":
                label = f"🔒 Request to Join {title}"
            else:
                label = f"➡️ Join {title}"
            buttons.append([InlineKeyboardButton(label, url=link)])
        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="fsub_recheck")])

        text = (
            "🔒 **Join to continue**\n\n"
            "You need to join the channel(s)/group(s) below before using this bot. "
            "Tap each one, then press **I've Joined**."
        )
        markup = InlineKeyboardMarkup(buttons)

        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer()
            await message_or_callback.message.reply_text(text, reply_markup=markup)
        else:
            await message_or_callback.reply_text(text, reply_markup=markup)

        return False

    async def _build_invite_link(self, client, chat, join_mode, prefer_public=True, fallback_link=None):
        """
        Central place that decides what kind of invite link to generate
        for a resolved `chat` object, based on join_mode:
          - "request" -> approval-required link (creates_join_request=True)
          - otherwise -> public @username link if available, else a normal
                         exported invite link
        Used both when a channel is first added and whenever a cached
        invite_link needs to be (re)generated.
        """
        if join_mode == "request":
            try:
                invite = await client.create_chat_invite_link(chat.id, creates_join_request=True)
                return invite.invite_link
            except Exception:
                return fallback_link

        if prefer_public and getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"

        try:
            return await client.export_chat_invite_link(chat.id)
        except Exception:
            return fallback_link

    async def _get_invite_link(self, client, ch):
        if ch.get("invite_link"):
            return ch["invite_link"]

        client_ref = self._to_client_ref(ch["ref"])
        try:
            chat = await client.get_chat(client_ref)
            link = await self._build_invite_link(client, chat, ch.get("join_mode", "direct"))
            if link:
                self.col.update_one(
                    {"ref": ch["ref"]},
                    {"$set": {"invite_link": link, "title": chat.title or ch["ref"]}}
                )
            return link
        except Exception:
            return None

    # ------------------------------------------------------------------
    # RECHECK BUTTON
    # ------------------------------------------------------------------
    def _register_recheck_callback(self):
        @self.app.on_callback_query(filters.regex("^fsub_recheck$"))
        async def recheck(client, callback: CallbackQuery):
            ok = await self.check(client, callback)
            if ok:
                await callback.message.edit_text("✅ Thanks for joining! You can now use the bot.")

    # ------------------------------------------------------------------
    # CHAT TRACKING (so you can find chat IDs without hunting for them)
    # ------------------------------------------------------------------
    def _register_chat_tracking(self):
        """
        Fires whenever the BOT's own membership/role changes in any chat --
        added, removed, promoted to admin, demoted, kicked, etc. (Telegram's
        `my_chat_member` update). We upsert a record so /mychats can list
        every chat the bot is currently admin in, with its chat_id ready
        to paste into /addchannel.

        NOTE: this only fires going forward from whenever the bot starts
        polling with this handler registered. Chats the bot already joined
        BEFORE this feature existed won't show up automatically -- for
        those, just run /getid once inside that chat (see below), or
        remove and re-add the bot to that chat to re-trigger the event.
        """
        @self.app.on_chat_member_updated()
        async def track_self_membership(client, update: ChatMemberUpdated):
            me_id = client.me.id if client.me else (await client.get_me()).id
            new = update.new_chat_member
            if not new or not new.user or new.user.id != me_id:
                return  # this update is about some other member, not the bot

            chat = update.chat
            is_admin = new.status in ("administrator", "creator")
            self.chats_col.update_one(
                {"chat_id": chat.id},
                {"$set": {
                    "chat_id": chat.id,
                    "title": chat.title or chat.first_name or str(chat.id),
                    "username": chat.username,
                    "type": str(chat.type),
                    "status": str(new.status),
                    "is_admin": is_admin,
                }},
                upsert=True
            )

    # ------------------------------------------------------------------
    # UTILITY COMMANDS: /mychats, /getid
    # ------------------------------------------------------------------
    def _register_utility_commands(self):
        admin_filter = filters.user(self.admin_ids)

        @self.app.on_message(filters.command("mychats") & admin_filter)
        async def my_chats(client, message: Message):
            admin_chats = list(self.chats_col.find({"is_admin": True}))
            if not admin_chats:
                await message.reply_text(
                    "No chats tracked yet where the bot is admin.\n\n"
                    "If the bot was added to a group/channel *before* this "
                    "feature was installed, that chat won't show up here "
                    "automatically -- run `/getid` inside that chat instead "
                    "(or as a channel post, if it's a channel), or kick and "
                    "re-add the bot there to trigger tracking."
                )
                return

            lines = []
            for c in admin_chats:
                ref = f"@{c['username']}" if c.get("username") else str(c["chat_id"])
                lines.append(
                    f"• **{c.get('title', ref)}**\n"
                    f"   Type: `{c.get('type')}`  |  Ref for /addchannel: `{ref}`"
                )
            await message.reply_text(
                "🤖 **Chats where the bot is admin:**\n\n" + "\n\n".join(lines) +
                "\n\nCopy the `ref` value straight into `/addchannel <ref>`."
            )

        @self.app.on_message(filters.command("getid") & admin_filter)
        async def get_id(client, message: Message):
            """
            Run this command directly inside the target group, or as a
            post in the target channel (typed by an admin account, not
            forwarded) -- the bot replies with that chat's ID/type/title,
            useful for chats not yet in /mychats (e.g. joined before this
            feature existed, or the bot isn't admin there yet).
            """
            chat = message.chat
            ref = f"@{chat.username}" if chat.username else str(chat.id)
            await message.reply_text(
                f"📌 **Chat info**\n\n"
                f"Title: {chat.title or chat.first_name or 'N/A'}\n"
                f"Type: `{chat.type}`\n"
                f"Chat ID: `{chat.id}`\n"
                f"Username: {'@' + chat.username if chat.username else 'None (private)'}\n\n"
                f"Ref to use with `/addchannel`: `{ref}`"
            )

        @self.app.on_message(filters.command("fsubdebug") & admin_filter)
        async def fsub_debug(client, message: Message):
            """
            Diagnostic: for each configured must-join channel, shows exactly
            what the bot sees when it checks membership -- so you can tell
            whether a channel is silently being skipped (bot not admin /
            can't resolve it) vs. correctly detecting joined/not-joined.
            Usage: /fsubdebug            -> checks yourself
                   /fsubdebug 123456789  -> checks a specific user id
            """
            args = message.text.split(maxsplit=1)
            target_uid = message.from_user.id
            if len(args) > 1 and args[1].strip().isdigit():
                target_uid = int(args[1].strip())

            channels = self._channels()
            if not channels:
                await message.reply_text("No must-join channels are configured. Add one with /addchannel.")
                return

            lines = [f"🔍 **Debug for user `{target_uid}`:**\n"]
            for ch in channels:
                client_ref = self._to_client_ref(ch["ref"])
                label = ch.get("title") or ch["ref"]
                try:
                    member = await client.get_chat_member(client_ref, target_uid)
                    lines.append(f"• **{label}** (`{ch['ref']}`) → status: `{member.status}`")
                except UserNotParticipant:
                    lines.append(f"• **{label}** (`{ch['ref']}`) → ❌ NOT a participant (would be blocked)")
                except Exception as e:
                    lines.append(
                        f"• **{label}** (`{ch['ref']}`) → ⚠️ CHECK FAILED "
                        f"({type(e).__name__}: {e})\n"
                        f"   ↳ This channel is being SKIPPED (not enforced). "
                        f"Usually means the bot isn't admin/member there, "
                        f"or the ref is wrong."
                    )
            await message.reply_text("\n\n".join(lines))

    # ------------------------------------------------------------------
    # ADMIN COMMANDS
    # ------------------------------------------------------------------
    def _register_admin_commands(self):
        admin_filter = filters.user(self.admin_ids)

        @self.app.on_message(filters.command("addchannel") & admin_filter)
        async def add_channel(client, message: Message):
            raw_args = message.text.split(maxsplit=1)
            if len(raw_args) < 2:
                await message.reply_text(
                    "Usage: `/addchannel <@username | invite_link | chat_id> "
                    "[--request] [--button=\"Text\"] [Optional Title]`\n\n"
                    "Examples:\n"
                    "`/addchannel @MyChannel`\n"
                    "`/addchannel https://t.me/+AbCdEf12345 --request My Private Group`\n"
                    "`/addchannel -1001234567890 --button=\"🎬 Join Movies\"`\n\n"
                    "⚠️ Make sure the bot is an **admin** in that channel/group first."
                )
                return

            remainder = raw_args[1].strip()

            # --- extract optional flags (order-independent) ---
            join_mode = "direct"
            if re.search(r'(?:^|\s)--request(?:\s|$)', remainder):
                join_mode = "request"
                remainder = re.sub(r'(?:^|\s)--request(?:\s|$)', ' ', remainder).strip()

            button_text = None
            btn_match = re.search(r'--button=("([^"]+)"|(\S+))', remainder)
            if btn_match:
                button_text = btn_match.group(2) or btn_match.group(3)
                remainder = (remainder[:btn_match.start()] + remainder[btn_match.end():]).strip()

            parts = remainder.split(maxsplit=1)
            if not parts or not parts[0]:
                await message.reply_text("❌ Please provide a channel reference.")
                return
            ref_input = parts[0].strip()
            title_override = parts[1].strip() if len(parts) > 1 else None

            kind, normalized = self._classify_ref(ref_input)
            if kind is None:
                await message.reply_text(
                    "❌ Invalid channel reference.\n\n"
                    "Send a public @username, a https://t.me/... link (public "
                    "or private invite), or a numeric chat ID."
                )
                return

            # Early dedupe check for refs we already know (username/chat_id).
            # Invite links can't be deduped until after we join and learn
            # the real chat id, so those get a second check right before insert.
            if kind != "invite_link" and self.col.find_one({"ref": normalized}):
                await message.reply_text("That channel is already in the must-join list.")
                return

            resolved_title = title_override
            invite_link = None
            ref = normalized
            warning_suffix = ""

            if kind == "invite_link":
                try:
                    chat = await client.join_chat(normalized)
                    ref = str(chat.id)
                    resolved_title = resolved_title or chat.title
                    invite_link = await self._build_invite_link(
                        client, chat, join_mode, prefer_public=False, fallback_link=normalized
                    )
                except Exception as e:
                    await message.reply_text(
                        f"❌ Couldn't join via that invite link ({e}).\n\n"
                        "If the bot is already a member of that chat, run `/getid` "
                        "directly inside it (or `/mychats`) to get its chat ID, "
                        "then use that ID with `/addchannel` instead."
                    )
                    return
            else:
                client_ref = self._to_client_ref(ref)
                try:
                    chat = await client.get_chat(client_ref)
                    resolved_title = resolved_title or chat.title
                    invite_link = await self._build_invite_link(client, chat, join_mode)
                except Exception as e:
                    resolved_title = resolved_title or ref_input
                    warning_suffix = (
                        f"\n\n⚠️ Couldn't fully verify this chat ({e}). Added anyway, "
                        "but make sure the bot is an admin there or the join check "
                        "will silently skip it."
                    )

            if self.col.find_one({"ref": ref}):
                await message.reply_text("That channel is already in the must-join list.")
                return

            self.col.insert_one({
                "ref": ref,
                "title": resolved_title or ref,
                "invite_link": invite_link,
                "link_type": kind,
                "join_mode": join_mode,
                "button_text": button_text,
                "added_by": str(message.from_user.id),
            })

            mode_label = "🔒 Request-to-Join" if join_mode == "request" else "✅ Direct Join"
            extra = f"\nButton text: {button_text}" if button_text else ""
            await message.reply_text(
                f"✅ Added **{resolved_title or ref}** to the must-join list.\n"
                f"Mode: {mode_label}{extra}{warning_suffix}"
            )

        @self.app.on_message(filters.command("removechannel") & admin_filter)
        async def remove_channel(client, message: Message):
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.reply_text("Usage: `/removechannel @username_or_chatid_or_link`")
                return
            raw = args[1].strip()

            result = self.col.delete_one({"ref": raw})
            if not result.deleted_count:
                kind, normalized = self._classify_ref(raw)
                if kind and normalized != raw:
                    result = self.col.delete_one({"ref": normalized})
            if not result.deleted_count:
                result = self.col.delete_one({"invite_link": raw})

            if result.deleted_count:
                await message.reply_text(f"✅ Removed `{raw}` from the must-join list.")
            else:
                await message.reply_text(
                    "That channel isn't in the list. Use /channels to see the current refs."
                )

        @self.app.on_message(filters.command("channels") & admin_filter)
        async def list_channels_cmd(client, message: Message):
            channels = self._channels()
            if not channels:
                await message.reply_text("No must-join channels configured yet.")
                return

            lines = []
            for i, c in enumerate(channels, start=1):
                mode_badge = "🔒 Request" if c.get("join_mode") == "request" else "✅ Direct"
                btn = c.get("button_text")
                btn_line = f"\n   Button: {btn}" if btn else ""
                lines.append(
                    f"{i}. **{c.get('title', c['ref'])}** — `{c['ref']}`\n"
                    f"   Mode: {mode_badge}{btn_line}"
                )
            await message.reply_text("📋 **Must-join channels:**\n\n" + "\n\n".join(lines))
