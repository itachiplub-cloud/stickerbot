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

FINDING CHAT IDs (new)
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
from pyrogram import filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from pyrogram.errors import UserNotParticipant


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
            title = ch.get("title") or ch["ref"]
            if link:
                buttons.append([InlineKeyboardButton(f"➡️ Join {title}", url=link)])
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

    async def _get_invite_link(self, client, ch):
        if ch.get("invite_link"):
            return ch["invite_link"]

        client_ref = self._to_client_ref(ch["ref"])
        try:
            chat = await client.get_chat(client_ref)
            if chat.username:
                link = f"https://t.me/{chat.username}"
            else:
                link = await client.export_chat_invite_link(chat.id)
            # cache it so we don't re-fetch every time
            self.col.update_one({"ref": ch["ref"]}, {"$set": {"invite_link": link, "title": chat.title or ch["ref"]}})
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
            args = message.text.split(maxsplit=2)
            if len(args) < 2:
                await message.reply_text(
                    "Usage: `/addchannel @username_or_chatid [Optional Title]`\n\n"
                    "⚠️ Make sure the bot is an **admin** in that channel/group first."
                )
                return

            ref = args[1].strip()
            title = args[2].strip() if len(args) > 2 else ref

            if self.col.find_one({"ref": ref}):
                await message.reply_text("That channel is already in the must-join list.")
                return

            invite_link = None
            client_ref = self._to_client_ref(ref)
            warning = ""
            try:
                chat = await client.get_chat(client_ref)
                title = chat.title or title
                if chat.username:
                    invite_link = f"https://t.me/{chat.username}"
                else:
                    invite_link = await client.export_chat_invite_link(chat.id)
            except Exception as e:
                warning = (
                    f"\n\n⚠️ Couldn't fully verify this chat ({e}). Added anyway, "
                    "but make sure the bot is an admin there or the join check "
                    "will silently skip it."
                )

            self.col.insert_one({
                "ref": ref, "title": title, "invite_link": invite_link,
                "added_by": str(message.from_user.id)
            })
            await message.reply_text(f"✅ Added **{title}** to the must-join list.{warning}")

        @self.app.on_message(filters.command("removechannel") & admin_filter)
        async def remove_channel(client, message: Message):
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.reply_text("Usage: `/removechannel @username_or_chatid`")
                return
            ref = args[1].strip()
            result = self.col.delete_one({"ref": ref})
            if result.deleted_count:
                await message.reply_text(f"✅ Removed `{ref}` from the must-join list.")
            else:
                await message.reply_text("That channel isn't in the list.")

        @self.app.on_message(filters.command("channels") & admin_filter)
        async def list_channels_cmd(client, message: Message):
            channels = self._channels()
            if not channels:
                await message.reply_text("No must-join channels configured yet.")
                return
            lines = [f"• {c.get('title', c['ref'])} — `{c['ref']}`" for c in channels]
            await message.reply_text("📋 **Must-join channels:**\n\n" + "\n".join(lines))
