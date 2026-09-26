import aiohttp
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.kickalerts")

KICK_AUTH_URL = "https://id.kick.com/oauth/token"
KICK_API_BASE = "https://api.kick.com/public/v2"
KICK_BASE_URL = "https://kick.com"
KICK_COLOR = 0x53FC18


class KickAlerts(commands.Cog):
    """Monitor Kick.com streamers using the official Kick API v2."""

    __version__ = "2.0.0"
    __author__ = "YourName"

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = None
        self.access_token = None
        self.token_expires_at = 0
        self.config = Config.get_conf(
            self, identifier=7274927492, force_registration=True
        )
        default_global = {
            "client_id": None,
            "client_secret": None,
        }
        default_guild = {
            "streamers": {},
            "global_channel_id": None,
            "global_ping_role_id": None,
            "check_interval": 60,
            "embed_style": "detailed",
            "show_viewer_count": True,
            "show_category": True,
            "auto_delete": False,
            "timezone_offset": 1,
        }
        self.config.register_global(**default_global)
        self.config.register_guild(**default_guild)
        self._check_task = None
        self._ready = asyncio.Event()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self._check_task = asyncio.create_task(self._stream_checker_loop())
        self._ready.set()

    async def cog_unload(self):
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()

    def format_help_for_context(self, ctx):
        pre = super().format_help_for_context(ctx)
        return pre + "\n\nCog Version: " + self.__version__

    async def _ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

    # ===================== AUTH =====================

    async def _get_access_token(self):
        """Get or refresh OAuth2 access token using client credentials."""
        import time
        now = time.time()
        if self.access_token and now < self.token_expires_at - 60:
            return self.access_token

        client_id = await self.config.client_id()
        client_secret = await self.config.client_secret()
        if not client_id or not client_secret:
            log.error("Kick API credentials not set.")
            return None

        await self._ensure_session()
        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            async with self.session.post(
                KICK_AUTH_URL,
                data=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.access_token = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    self.token_expires_at = now + expires_in
                    log.info("Kick OAuth2 token obtained, expires in %ds", expires_in)
                    return self.access_token
                else:
                    body = await resp.text()
                    log.error("Auth failed HTTP %d: %s", resp.status, body[:300])
                    return None
        except Exception as exc:
            log.error("Auth request failed: %s", exc)
            return None

    async def _api_headers(self):
        """Get headers with valid bearer token."""
        token = await self._get_access_token()
        if not token:
            return None
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        }

    # ===================== API CALLS =====================

    async def _fetch_channels(self, usernames):
        """Fetch channel data for one or more usernames.

        GET /public/v2/channels?username[]=name1&username[]=name2
        """
        headers = await self._api_headers()
        if not headers:
            return None
        await self._ensure_session()
        params = []
        for u in usernames:
            params.append(("username[]", u.lower()))
        url = KICK_API_BASE + "/channels"
        try:
            async with self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    body = await resp.text()
                    log.error("Channels API HTTP %d: %s", resp.status, body[:300])
                    if resp.status == 401:
                        self.access_token = None
                    return None
        except Exception as exc:
            log.error("Channels API error: %s", exc)
            return None

    async def _fetch_single_channel(self, username):
        """Fetch a single channel and return its data dict or None."""
        result = await self._fetch_channels([username])
        if result is None:
            return None
        channels = result.get("data", [])
        if not channels:
            return None
        return channels[0]

    async def _fetch_livestreams(self, usernames):
        """Fetch livestream data.

        GET /public/v2/channels/livestreams?username[]=name1&username[]=name2
        """
        headers = await self._api_headers()
        if not headers:
            return None
        await self._ensure_session()
        params = []
        for u in usernames:
            params.append(("username[]", u.lower()))
        url = KICK_API_BASE + "/channels/livestreams"
        try:
            async with self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    body = await resp.text()
                    log.error("Livestreams API HTTP %d: %s", resp.status, body[:300])
                    if resp.status == 401:
                        self.access_token = None
                    return None
        except Exception as exc:
            log.error("Livestreams API error: %s", exc)
            return None

    # ===================== PARSING =====================

    def _parse_channel_info(self, channel_data, livestream_data=None):
        """Parse channel and optional livestream data into unified info dict."""
        info = {
            "is_live": False,
            "username": "",
            "display_name": "",
            "avatar_url": None,
            "channel_url": "",
            "followers": 0,
            "is_verified": False,
            "stream_id": None,
            "stream_title": None,
            "viewer_count": 0,
            "category": None,
            "thumbnail_url": None,
            "started_at": None,
            "language": None,
            "is_mature": False,
            "tags": [],
        }
        if channel_data is None:
            return info

        # Channel fields from v2 API
        slug = channel_data.get("slug") or channel_data.get("username") or ""
        info["username"] = slug
        info["display_name"] = channel_data.get("username") or slug
        info["channel_url"] = KICK_BASE_URL + "/" + slug

        # Avatar - try multiple keys
        info["avatar_url"] = (
            channel_data.get("profile_picture")
            or channel_data.get("profile_pic")
            or channel_data.get("avatar")
        )

        # Followers - try multiple keys
        for key in ["followers_count", "followersCount", "follower_count", "followers"]:
            val = channel_data.get(key)
            if val is not None and isinstance(val, (int, float)):
                info["followers"] = int(val)
                break

        info["is_verified"] = channel_data.get("is_verified") or channel_data.get("verified") or False

        # Livestream data
        ls = livestream_data
        if ls is None:
            ls = channel_data.get("livestream")

        if ls and isinstance(ls, dict):
            is_live = ls.get("is_live", False)
            if is_live is None:
                is_live = ls.get("id") is not None
            info["is_live"] = bool(is_live)

            if info["is_live"]:
                info["stream_id"] = ls.get("id")
                info["stream_title"] = ls.get("title") or ls.get("session_title") or "No Title"
                info["viewer_count"] = ls.get("viewer_count") or ls.get("viewers") or 0
                info["started_at"] = ls.get("started_at") or ls.get("created_at")
                info["language"] = ls.get("language") or "en"
                info["is_mature"] = ls.get("is_mature") or ls.get("mature") or False

                # Category
                cat = ls.get("category")
                cats = ls.get("categories")
                if isinstance(cat, dict):
                    info["category"] = cat.get("name") or "Unknown"
                elif isinstance(cat, str):
                    info["category"] = cat
                elif isinstance(cats, list) and len(cats) > 0:
                    first = cats[0]
                    if isinstance(first, dict):
                        info["category"] = first.get("name") or "Unknown"
                    elif isinstance(first, str):
                        info["category"] = first
                else:
                    info["category"] = ls.get("category_name") or "Unknown"

                # Thumbnail
                thumb = ls.get("thumbnail")
                if isinstance(thumb, dict):
                    info["thumbnail_url"] = thumb.get("url") or thumb.get("src")
                elif isinstance(thumb, str):
                    info["thumbnail_url"] = thumb

                # Tags
                raw_tags = ls.get("tags")
                tags = []
                if isinstance(raw_tags, list):
                    for tag in raw_tags:
                        if isinstance(tag, dict):
                            tname = tag.get("name") or tag.get("tag")
                            if tname:
                                tags.append(str(tname))
                        elif isinstance(tag, str):
                            tags.append(tag)
                info["tags"] = tags

        return info

    def _parse_timestamp(self, timestamp_str):
        """Parse timestamp to unix epoch."""
        if not timestamp_str or not isinstance(timestamp_str, str):
            return None
        try:
            s = timestamp_str.replace("Z", "+00:00")
            if "." in s:
                dot_pos = s.index(".")
                plus_pos = s.find("+", dot_pos)
                minus_pos = s.find("-", dot_pos)
                end_pos = -1
                if plus_pos > 0:
                    end_pos = plus_pos
                elif minus_pos > 0:
                    end_pos = minus_pos
                if end_pos < 0:
                    end_pos = len(s)
                micro = s[dot_pos + 1:end_pos]
                if len(micro) > 6:
                    micro = micro[:6]
                s = s[:dot_pos + 1] + micro + s[end_pos:]
            dt = datetime.fromisoformat(s)
            return int(dt.timestamp())
        except (ValueError, TypeError, OSError):
            return None

    # ===================== EMBEDS =====================

    def _build_live_embed(self, info, style="detailed", show_viewers=True, show_category=True, tz_offset=0):
        tz = timezone(timedelta(hours=tz_offset))
        now = datetime.now(tz)
        embed = discord.Embed(color=KICK_COLOR, timestamp=now)
        aname = info["display_name"] + " is LIVE on Kick!"
        embed.set_author(
            name=aname,
            url=info["channel_url"],
            icon_url=info.get("avatar_url"),
        )
        embed.title = info.get("stream_title") or "No Title"
        embed.url = info["channel_url"]

        if style == "detailed":
            parts = []
            if show_category and info.get("category"):
                parts.append("**Category:** " + str(info["category"]))
            if show_viewers:
                vc = info.get("viewer_count", 0)
                parts.append("**Viewers:** " + "{:,}".format(vc))
            ts = self._parse_timestamp(info.get("started_at"))
            if ts is not None:
                parts.append("**Started:** <t:" + str(ts) + ":R> (<t:" + str(ts) + ":t>)")
            followers = info.get("followers", 0)
            if followers > 0:
                parts.append("**Followers:** " + "{:,}".format(followers))
            if info.get("tags"):
                tstr = " ".join("`" + str(t) + "`" for t in info["tags"][:5])
                parts.append("**Tags:** " + tstr)
            if info.get("is_mature"):
                parts.append("**18+ Mature Content**")
            parts.append("")
            parts.append("**[Watch Stream on Kick](" + info["channel_url"] + ")**")
            embed.description = "\n".join(parts)
        else:
            parts = []
            if show_category and info.get("category"):
                parts.append("Playing **" + str(info["category"]) + "**")
            if show_viewers:
                vc = info.get("viewer_count", 0)
                parts.append("{:,}".format(vc) + " viewers")
            link = "\n**[Watch Now](" + info["channel_url"] + ")**"
            if parts:
                embed.description = " | ".join(parts) + link
            else:
                embed.description = link

        if info.get("thumbnail_url"):
            embed.set_image(url=info["thumbnail_url"])
        elif info.get("avatar_url"):
            embed.set_thumbnail(url=info["avatar_url"])
        embed.set_footer(text="Kick.com | Live Stream Alert")
        return embed

    def _build_offline_embed(self, info, tz_offset=0):
        tz = timezone(timedelta(hours=tz_offset))
        now = datetime.now(tz)
        desc = "**" + info["display_name"] + "** has gone offline."
        desc = desc + "\n[Visit Channel](" + info["channel_url"] + ")"
        embed = discord.Embed(color=0x808080, description=desc, timestamp=now)
        embed.set_author(
            name=info["display_name"] + " is now Offline",
            url=info["channel_url"],
            icon_url=info.get("avatar_url"),
        )
        embed.set_footer(text="Kick.com | Stream Ended")
        return embed

    # ===================== CHECKER LOOP =====================

    async def _stream_checker_loop(self):
        await self.bot.wait_until_ready()
        await self._ready.wait()
        while True:
            sleep_time = 60
            try:
                creds_ok = await self.config.client_id()
                if not creds_ok:
                    await asyncio.sleep(60)
                    continue

                all_guilds = await self.config.all_guilds()
                for guild_id, guild_data in all_guilds.items():
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    streamers = guild_data.get("streamers", {})
                    if not streamers:
                        continue

                    e_style = guild_data.get("embed_style", "detailed")
                    s_viewers = guild_data.get("show_viewer_count", True)
                    s_cat = guild_data.get("show_category", True)
                    a_del = guild_data.get("auto_delete", False)
                    g_ch = guild_data.get("global_channel_id")
                    g_role = guild_data.get("global_ping_role_id")
                    tz_off = guild_data.get("timezone_offset", 1)

                    usernames = list(streamers.keys())

                    # Batch fetch livestreams (max 10 per request)
                    batch_size = 10
                    all_livestream_data = {}
                    all_channel_data = {}

                    for i in range(0, len(usernames), batch_size):
                        batch = usernames[i:i + batch_size]

                        # Fetch channels
                        ch_result = await self._fetch_channels(batch)
                        if ch_result and isinstance(ch_result.get("data"), list):
                            for ch in ch_result["data"]:
                                slug = ch.get("slug") or ch.get("username") or ""
                                all_channel_data[slug.lower()] = ch

                        # Fetch livestreams
                        ls_result = await self._fetch_livestreams(batch)
                        if ls_result and isinstance(ls_result.get("data"), list):
                            for ls in ls_result["data"]:
                                slug = ls.get("slug") or ls.get("channel_slug") or ls.get("username") or ""
                                all_livestream_data[slug.lower()] = ls

                        await asyncio.sleep(1)

                    # Process each streamer
                    for uname, scfg in streamers.items():
                        try:
                            ch_data = all_channel_data.get(uname.lower())
                            ls_data = all_livestream_data.get(uname.lower())
                            if ch_data is None:
                                continue
                            info = self._parse_channel_info(ch_data, ls_data)
                            await self._process_streamer_update(
                                guild, uname, scfg, info,
                                e_style, s_viewers, s_cat,
                                a_del, g_ch, g_role, tz_off,
                            )
                        except Exception as exc:
                            log.error("Error processing %s: %s", uname, exc)

                intervals = []
                for g in all_guilds.values():
                    if g.get("streamers"):
                        intervals.append(g.get("check_interval", 60))
                if intervals:
                    sleep_time = min(intervals)
                sleep_time = max(sleep_time, 30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Loop error: %s", exc)
                sleep_time = 60
            await asyncio.sleep(sleep_time)

    async def _process_streamer_update(
        self, guild, username, scfg, info,
        embed_style, show_viewers, show_category,
        auto_delete, global_channel_id, global_ping_role_id,
        tz_offset
    ):
        was_live = scfg.get("is_live", False)
        is_live = info["is_live"]
        last_sid = scfg.get("last_stream_id")
        cur_sid = info.get("stream_id")
        ch_id = scfg.get("channel_id") or global_channel_id
        if ch_id is None:
            return
        channel = guild.get_channel(ch_id)
        if channel is None:
            return
        ping_role_id = scfg.get("ping_role_id") or global_ping_role_id

        new_stream = False
        if is_live:
            if not was_live:
                new_stream = True
            elif cur_sid and cur_sid != last_sid:
                new_stream = True

        if new_stream:
            embed = self._build_live_embed(
                info, embed_style, show_viewers, show_category, tz_offset
            )
            content = None
            if ping_role_id:
                role = guild.get_role(ping_role_id)
                if role is not None:
                    content = role.mention
            cmsg = scfg.get("custom_message")
            if cmsg:
                cmsg = cmsg.replace("{streamer}", info["display_name"])
                cmsg = cmsg.replace("{game}", str(info.get("category") or "Unknown"))
                cmsg = cmsg.replace("{title}", str(info.get("stream_title") or "No Title"))
                cmsg = cmsg.replace("{url}", info["channel_url"])
                cmsg = cmsg.replace("{viewers}", str(info.get("viewer_count", 0)))
                if content:
                    content = content + "\n" + cmsg
                else:
                    content = cmsg
            try:
                msg = await channel.send(content=content, embed=embed)
                async with self.config.guild(guild).streamers() as st:
                    if username in st:
                        st[username]["is_live"] = True
                        st[username]["last_stream_id"] = cur_sid
                        st[username]["last_message_id"] = msg.id
            except (discord.Forbidden, discord.HTTPException):
                pass

        elif not is_live and was_live:
            async with self.config.guild(guild).streamers() as st:
                if username in st:
                    st[username]["is_live"] = False
                    lmid = st[username].get("last_message_id")
                    if lmid is not None and channel is not None:
                        try:
                            old = await channel.fetch_message(lmid)
                            do_del = scfg.get("delete_after_offline", auto_delete)
                            if do_del:
                                await old.delete()
                            else:
                                oe = self._build_offline_embed(info, tz_offset)
                                await old.edit(content=None, embed=oe)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                    st[username]["last_message_id"] = None

        elif is_live and was_live and embed_style == "detailed":
            lmid = scfg.get("last_message_id")
            if lmid is not None and channel is not None:
                try:
                    old = await channel.fetch_message(lmid)
                    embed = self._build_live_embed(
                        info, embed_style, show_viewers, show_category, tz_offset
                    )
                    await old.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    # ===================== COMMANDS =====================

    @commands.group(name="kickalert", aliases=["ka", "kickalerts"], invoke_without_command=False)
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert(self, ctx):
        """Manage Kick.com livestream alerts."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @kickalert.command(name="setcreds", aliases=["credentials", "auth"])
    @checks.is_owner()
    async def kickalert_setcreds(self, ctx, client_id: str, client_secret: str):
        """Set Kick API credentials (Bot Owner only).

        Get these from https://kick.com/settings/developer

        **Example:**
        `[p]kickalert setcreds your_client_id your_client_secret`
        """
        # Try to delete the message with credentials
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self.config.client_id.set(client_id)
        await self.config.client_secret.set(client_secret)
        self.access_token = None

        # Test the credentials
        token = await self._get_access_token()
        if token:
            await ctx.send("API credentials saved and verified! Authentication successful.")
        else:
            await ctx.send(
                "Credentials saved but authentication failed. "
                "Please check your Client ID and Client Secret."
            )

    @kickalert.command(name="authstatus")
    async def kickalert_authstatus(self, ctx):
        """Check if API authentication is working."""
        client_id = await self.config.client_id()
        if not client_id:
            await ctx.send(
                "No API credentials set. Use `"
                + ctx.clean_prefix
                + "kickalert setcreds <client_id> <client_secret>`"
            )
            return
        token = await self._get_access_token()
        if token:
            await ctx.send("Kick API authentication is working!")
        else:
            await ctx.send("Authentication failed. Check your credentials.")

    @kickalert.command(name="add")
    async def kickalert_add(self, ctx, username: str, channel: Optional[discord.TextChannel] = None):
        """Add a Kick streamer to monitor.

        **Example:**
        `[p]kickalert add xqc #stream-alerts`
        """
        username = username.lower().strip().strip("/")
        async with ctx.typing():
            ch_data = await self._fetch_single_channel(username)
        if ch_data is None:
            await ctx.send(
                "Could not find **" + username + "** on Kick.com. "
                "Make sure your API credentials are set and the username is correct."
            )
            return
        info = self._parse_channel_info(ch_data)
        ch_id = None
        if channel is not None:
            ch_id = channel.id
        if ch_id is None:
            gch = await self.config.guild(ctx.guild).global_channel_id()
            if gch is None:
                await ctx.send(
                    "No channel given and no global channel set. Use `"
                    + ctx.clean_prefix
                    + "kickalert setchannel #channel` first."
                )
                return
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username in streamers:
                await ctx.send("**" + info["display_name"] + "** is already monitored!")
                return
            streamers[username] = {
                "channel_id": ch_id,
                "ping_role_id": None,
                "custom_message": None,
                "delete_after_offline": False,
                "last_message_id": None,
                "is_live": info["is_live"],
                "last_stream_id": info.get("stream_id"),
            }
        if info["is_live"]:
            st = "Currently LIVE"
        else:
            st = "Offline"
        if channel is not None:
            cht = channel.mention
        else:
            cht = "Global channel"
        followers = info.get("followers", 0)
        embed = discord.Embed(color=KICK_COLOR, title="Streamer Added")
        embed.description = (
            "Now monitoring **["
            + info["display_name"]
            + "]("
            + info["channel_url"]
            + ")** on Kick.com\n\n"
            + "**Channel:** " + cht + "\n"
            + "**Status:** " + st + "\n"
            + "**Followers:** " + "{:,}".format(followers)
        )
        if info.get("avatar_url"):
            embed.set_thumbnail(url=info["avatar_url"])
        await ctx.send(embed=embed)

    @kickalert.command(name="remove", aliases=["delete", "rm"])
    async def kickalert_remove(self, ctx, username: str):
        """Remove a streamer. Example: `[p]kickalert remove xqc`"""
        username = username.lower().strip()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username not in streamers:
                await ctx.send("**" + username + "** is not monitored.")
                return
            del streamers[username]
        await ctx.send("Removed **" + username + "** from alerts.")

    @kickalert.command(name="list")
    async def kickalert_list(self, ctx):
        """List all monitored streamers."""
        streamers = await self.config.guild(ctx.guild).streamers()
        if not streamers:
            await ctx.send("No streamers monitored yet.")
            return
        gch = await self.config.guild(ctx.guild).global_channel_id()
        tz_off = await self.config.guild(ctx.guild).timezone_offset()
        tz = timezone(timedelta(hours=tz_off))
        embed = discord.Embed(
            color=KICK_COLOR,
            title="Monitored Kick.com Streamers",
            timestamp=datetime.now(tz),
        )
        for uname, cfg in streamers.items():
            cid = cfg.get("channel_id") or gch
            if cid:
                cm = "<#" + str(cid) + ">"
            else:
                cm = "Not set"
            if cfg.get("is_live"):
                status = "LIVE"
            else:
                status = "Offline"
            pr = cfg.get("ping_role_id")
            if pr:
                pt = "<@&" + str(pr) + ">"
            else:
                pt = "None"
            val = "Channel: " + cm + "\nPing: " + pt
            val = val + "\n[Profile](" + KICK_BASE_URL + "/" + uname + ")"
            embed.add_field(name=status + " - " + uname, value=val, inline=True)
        embed.set_footer(text=str(len(streamers)) + " streamer(s)")
        await ctx.send(embed=embed)

    @kickalert.command(name="setchannel", aliases=["channel"])
    async def kickalert_setchannel(self, ctx, channel: discord.TextChannel):
        """Set global alert channel."""
        await self.config.guild(ctx.guild).global_channel_id.set(channel.id)
        await ctx.send("Global channel set to " + channel.mention)

    @kickalert.command(name="setrole", aliases=["pingrole", "role"])
    async def kickalert_setrole(self, ctx, role: discord.Role, username: Optional[str] = None):
        """Set ping role globally or per streamer."""
        if username:
            username = username.lower().strip()
            async with self.config.guild(ctx.guild).streamers() as streamers:
                if username not in streamers:
                    await ctx.send("**" + username + "** is not monitored.")
                    return
                streamers[username]["ping_role_id"] = role.id
            await ctx.send("Ping role for **" + username + "** set to " + role.mention)
        else:
            await self.config.guild(ctx.guild).global_ping_role_id.set(role.id)
            await ctx.send("Global ping role set to " + role.mention)

    @kickalert.command(name="removerole")
    async def kickalert_removerole(self, ctx, username: Optional[str] = None):
        """Remove ping role."""
        if username:
            username = username.lower().strip()
            async with self.config.guild(ctx.guild).streamers() as streamers:
                if username not in streamers:
                    await ctx.send("**" + username + "** is not monitored.")
                    return
                streamers[username]["ping_role_id"] = None
            await ctx.send("Ping role removed for **" + username + "**.")
        else:
            await self.config.guild(ctx.guild).global_ping_role_id.set(None)
            await ctx.send("Global ping role removed.")

    @kickalert.command(name="message", aliases=["custommsg", "msg"])
    async def kickalert_message(self, ctx, username: str, *, message: str = None):
        """Set custom message. Placeholders: {streamer} {game} {title} {url} {viewers}"""
        username = username.lower().strip()
        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username not in streamers:
                await ctx.send("**" + username + "** is not monitored.")
                return
            streamers[username]["custom_message"] = message
        if message:
            await ctx.send("Custom message set:\n" + message)
        else:
            await ctx.send("Custom message cleared.")

    @kickalert.command(name="interval")
    async def kickalert_interval(self, ctx, seconds: int):
        """Set check interval (30-600)."""
        if seconds < 30:
            await ctx.send("Minimum is 30 seconds.")
            return
        if seconds > 600:
            await ctx.send("Maximum is 600 seconds.")
            return
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send("Interval set to **" + str(seconds) + "** seconds.")

    @kickalert.command(name="style")
    async def kickalert_style(self, ctx, style: str):
        """Set embed style: detailed or minimal."""
        style = style.lower()
        if style not in ("detailed", "minimal"):
            await ctx.send("Must be `detailed` or `minimal`.")
            return
        await self.config.guild(ctx.guild).embed_style.set(style)
        await ctx.send("Style set to **" + style + "**.")

    @kickalert.command(name="autodelete")
    async def kickalert_autodelete(self, ctx, toggle: bool):
        """Toggle auto-delete on offline."""
        await self.config.guild(ctx.guild).auto_delete.set(toggle)
        state = "enabled" if toggle else "disabled"
        await ctx.send("Auto-delete **" + state + "**.")

    @kickalert.command(name="timezone", aliases=["tz"])
    async def kickalert_timezone(self, ctx, offset: int):
        """Set timezone offset from UTC.

        **Examples:**
        `[p]kickalert timezone 1` - CET
        `[p]kickalert timezone 2` - CEST
        `[p]kickalert timezone -5` - EST
        """
        if offset < -12 or offset > 14:
            await ctx.send("Must be between -12 and +14.")
            return
        await self.config.guild(ctx.guild).timezone_offset.set(offset)
        if offset >= 0:
            tz_str = "UTC+" + str(offset)
        else:
            tz_str = "UTC" + str(offset)
        await ctx.send("Timezone set to **" + tz_str + "**.")

    @kickalert.command(name="test")
    async def kickalert_test(self, ctx, username: str):
        """Send a test announcement embed."""
        username = username.lower().strip()
        async with ctx.typing():
            ch_data = await self._fetch_single_channel(username)
        if ch_data is None:
            await ctx.send("Could not find **" + username + "**.")
            return
        ls_result = await self._fetch_livestreams([username])
        ls_data = None
        if ls_result and isinstance(ls_result.get("data"), list):
            for ls in ls_result["data"]:
                slug = ls.get("slug") or ls.get("channel_slug") or ls.get("username") or ""
                if slug.lower() == username.lower():
                    ls_data = ls
                    break
        info = self._parse_channel_info(ch_data, ls_data)
        if not info["is_live"]:
            info["is_live"] = True
            info["stream_title"] = info.get("stream_title") or "Test Stream"
            info["viewer_count"] = info.get("viewer_count") or 1234
            info["category"] = info.get("category") or "Just Chatting"
            info["started_at"] = datetime.now(timezone.utc).isoformat()
            info["tags"] = info.get("tags") or ["English", "Test"]
        style = await self.config.guild(ctx.guild).embed_style()
        sv = await self.config.guild(ctx.guild).show_viewer_count()
        sc = await self.config.guild(ctx.guild).show_category()
        tz_off = await self.config.guild(ctx.guild).timezone_offset()
        embed = self._build_live_embed(info, style, sv, sc, tz_off)
        await ctx.send("**Test Announcement:**", embed=embed)

    @kickalert.command(name="check", aliases=["status"])
    async def kickalert_check(self, ctx, username: str):
        """Check live status of a streamer."""
        username = username.lower().strip()
        async with ctx.typing():
            ch_data = await self._fetch_single_channel(username)
        if ch_data is None:
            await ctx.send("Could not find **" + username + "**.")
            return
        ls_result = await self._fetch_livestreams([username])
        ls_data = None
        if ls_result and isinstance(ls_result.get("data"), list):
            for ls in ls_result["data"]:
                slug = ls.get("slug") or ls.get("channel_slug") or ls.get("username") or ""
                if slug.lower() == username.lower():
                    ls_data = ls
                    break
        info = self._parse_channel_info(ch_data, ls_data)
        tz_off = await self.config.guild(ctx.guild).timezone_offset()
        if info["is_live"]:
            embed = self._build_live_embed(info, tz_offset=tz_off)
        else:
            tz = timezone(timedelta(hours=tz_off))
            followers = info.get("followers", 0)
            embed = discord.Embed(
                color=0x808080,
                title=info["display_name"] + " is Offline",
                url=info["channel_url"],
                description=(
                    "**" + info["display_name"] + "** is not streaming.\n\n"
                    + "**Followers:** " + "{:,}".format(followers) + "\n"
                    + "[Visit Channel](" + info["channel_url"] + ")"
                ),
                timestamp=datetime.now(tz),
            )
            if info.get("avatar_url"):
                embed.set_thumbnail(url=info["avatar_url"])
            embed.set_footer(text="Kick.com")
        await ctx.send(embed=embed)

    @kickalert.command(name="debug")
    async def kickalert_debug(self, ctx, username: str):
        """Debug: Show raw API response data."""
        username = username.lower().strip()
        async with ctx.typing():
            ch_data = await self._fetch_single_channel(username)
            ls_result = await self._fetch_livestreams([username])
        embed = discord.Embed(
            color=0xFFAA00,
            title="Debug: " + username,
            timestamp=datetime.now(timezone.utc),
        )
        if ch_data is None:
            embed.description = "Channel API returned no data."
        else:
            lines = "**=== CHANNEL DATA ===**\n"
            for k, v in ch_data.items():
                val_str = str(v)
                if len(val_str) > 100:
                    val_str = val_str[:100] + "..."
                line = "**" + str(k) + ":** " + val_str + "\n"
                if len(lines) + len(line) > 1900:
                    lines = lines + "... truncated\n"
                    break
                lines = lines + line
            lines = lines + "\n**=== LIVESTREAM DATA ===**\n"
            if ls_result and isinstance(ls_result.get("data"), list):
                if ls_result["data"]:
                    for k, v in ls_result["data"][0].items():
                        val_str = str(v)
                        if len(val_str) > 100:
                            val_str = val_str[:100] + "..."
                        line = "**" + str(k) + ":** " + val_str + "\n"
                        if len(lines) + len(line) > 3900:
                            lines = lines + "... truncated"
                            break
                        lines = lines + line
                else:
                    lines = lines + "Not live / no data"
            else:
                lines = lines + "No livestream response"
            embed.description = lines
        embed.set_footer(text="Raw API debug data")
        await ctx.send(embed=embed)

    @kickalert.command(name="settings")
    async def kickalert_settings(self, ctx):
        """View current settings."""
        gd = await self.config.guild(ctx.guild).all()
        g_ch = gd.get("global_channel_id")
        g_role = gd.get("global_ping_role_id")
        interval = gd.get("check_interval", 60)
        style = gd.get("embed_style", "detailed")
        auto_del = gd.get("auto_delete", False)
        sv = gd.get("show_viewer_count", True)
        sc = gd.get("show_category", True)
        scount = len(gd.get("streamers", {}))
        tz_off = gd.get("timezone_offset", 1)
        if tz_off >= 0:
            tz_str = "UTC+" + str(tz_off)
        else:
            tz_str = "UTC" + str(tz_off)
        has_creds = bool(await self.config.client_id())
        tz = timezone(timedelta(hours=tz_off))
        embed = discord.Embed(
            color=KICK_COLOR,
            title="KickAlerts Settings",
            timestamp=datetime.now(tz),
        )
        embed.add_field(name="API Credentials", value="Set" if has_creds else "NOT SET", inline=True)
        embed.add_field(name="Global Channel", value="<#" + str(g_ch) + ">" if g_ch else "Not set", inline=True)
        embed.add_field(name="Global Ping Role", value="<@&" + str(g_role) + ">" if g_role else "None", inline=True)
        embed.add_field(name="Streamers", value=str(scount), inline=True)
        embed.add_field(name="Interval", value=str(interval) + "s", inline=True)
        embed.add_field(name="Style", value=style.capitalize(), inline=True)
        embed.add_field(name="Auto-Delete", value="Yes" if auto_del else "No", inline=True)
        embed.add_field(name="Show Viewers", value="Yes" if sv else "No", inline=True)
        embed.add_field(name="Show Category", value="Yes" if sc else "No", inline=True)
        embed.add_field(name="Timezone", value=tz_str, inline=True)
        await ctx.send(embed=embed)

    @kickalert.command(name="toggleviewers")
    async def kickalert_toggleviewers(self, ctx, toggle: bool):
        """Toggle viewer count in embeds."""
        await self.config.guild(ctx.guild).show_viewer_count.set(toggle)
        state = "shown" if toggle else "hidden"
        await ctx.send("Viewer count: **" + state + "**.")

    @kickalert.command(name="togglecategory")
    async def kickalert_togglecategory(self, ctx, toggle: bool):
        """Toggle category in embeds."""
        await self.config.guild(ctx.guild).show_category.set(toggle)
        state = "shown" if toggle else "hidden"
        await ctx.send("Category: **" + state + "**.")

    @kickalert.command(name="clear")
    async def kickalert_clear(self, ctx, confirm: bool = False):
        """Remove ALL streamers and reset config."""
        if not confirm:
            await ctx.send(
                "This will remove everything. Run `"
                + ctx.clean_prefix
                + "kickalert clear True` to confirm."
            )
            return
        await self.config.guild(ctx.guild).clear()
        await ctx.send("All KickAlerts data cleared.")

    @kickalert.command(name="force", aliases=["forcecheck"])
    async def kickalert_force(self, ctx):
        """Force an immediate check of all streamers."""
        streamers = await self.config.guild(ctx.guild).streamers()
        if not streamers:
            await ctx.send("No streamers to check.")
            return
        gd = await self.config.guild(ctx.guild).all()
        tz_off = gd.get("timezone_offset", 1)
        checked = 0
        live = 0
        async with ctx.typing():
            usernames = list(streamers.keys())
            ch_result = await self._fetch_channels(usernames)
            ls_result = await self._fetch_livestreams(usernames)
            all_ch = {}
            all_ls = {}
            if ch_result and isinstance(ch_result.get("data"), list):
                for ch in ch_result["data"]:
                    slug = (ch.get("slug") or ch.get("username") or "").lower()
                    all_ch[slug] = ch
            if ls_result and isinstance(ls_result.get("data"), list):
                for ls in ls_result["data"]:
                    slug = (ls.get("slug") or ls.get("channel_slug") or ls.get("username") or "").lower()
                    all_ls[slug] = ls
            for uname, scfg in streamers.items():
                ch_data = all_ch.get(uname.lower())
                ls_data = all_ls.get(uname.lower())
                if ch_data is None:
                    continue
                info = self._parse_channel_info(ch_data, ls_data)
                try:
                    await self._process_streamer_update(
                        ctx.guild, uname, scfg, info,
                        gd.get("embed_style", "detailed"),
                        gd.get("show_viewer_count", True),
                        gd.get("show_category", True),
                        gd.get("auto_delete", False),
                        gd.get("global_channel_id"),
                        gd.get("global_ping_role_id"),
                        tz_off,
                    )
                    checked = checked + 1
                    if info["is_live"]:
                        live = live + 1
                except Exception as e:
                    log.error("Force-check error %s: %s", uname, e)
        await ctx.send(
            "Checked **" + str(checked) + "** streamer(s). **"
            + str(live) + "** currently live."
        )