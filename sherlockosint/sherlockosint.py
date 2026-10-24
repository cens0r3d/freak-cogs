import aiohttp
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.sherlockosint")

SHERLOCK_COLOR = 0x00D4FF
SHERLOCK_DATA_URL = "https://raw.githubusercontent.com/sherlock-project/sherlock/master/sherlock_project/resources/data.json"


class SherlockOsint(commands.Cog):
    """Search usernames across hundreds of social networks."""

    __version__ = "1.0.0"
    __author__ = "YourName"

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = None
        self.sites_data = {}
        self.sites_loaded = False
        self.config = Config.get_conf(
            self, identifier=8183649201, force_registration=True
        )
        default_guild = {
            "cooldown": 30,
            "max_results_per_page": 15,
            "enabled": True,
            "timeout": 10,
            "show_not_found": False,
            "nsfw_filter": True,
            "allowed_channels": [],
        }
        self.config.register_guild(**default_guild)
        self._cooldowns = {}

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/json",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        )
        asyncio.create_task(self._load_sites_data())

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def format_help_for_context(self, ctx):
        pre = super().format_help_for_context(ctx)
        return pre + "\n\nCog Version: " + self.__version__

    async def _ensure_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "text/html,application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def _load_sites_data(self):
        await self._ensure_session()
        try:
            async with self.session.get(SHERLOCK_DATA_URL) as resp:
                if resp.status == 200:
                    self.sites_data = await resp.json(content_type=None)
                    self.sites_loaded = True
                    log.info(
                        "Loaded %d sites from Sherlock data.",
                        len(self.sites_data),
                    )
                else:
                    log.error(
                        "Failed to load Sherlock data: HTTP %d", resp.status
                    )
        except Exception as exc:
            log.error("Failed to load Sherlock data: %s", exc)

    def _check_cooldown(self, guild_id, user_id, cooldown_seconds):
        key = str(guild_id) + ":" + str(user_id)
        now = time.time()
        last = self._cooldowns.get(key, 0)
        diff = now - last
        if diff < cooldown_seconds:
            remaining = cooldown_seconds - diff
            return remaining
        self._cooldowns[key] = now
        return 0

    async def _check_site(self, site_name, site_info, username, req_timeout):
        url_template = site_info.get("url")
        if not url_template:
            return None
        url = url_template.replace("{}", username)
        error_type = site_info.get("errorType", "")
        error_msg = site_info.get("errorMsg")
        error_url = site_info.get("errorUrl")
        result = {
            "site": site_name,
            "url": url,
            "found": False,
            "http_status": None,
            "error": None,
        }
        try:
            async with self.session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=req_timeout),
                allow_redirects=True,
                ssl=False,
            ) as resp:
                result["http_status"] = resp.status
                if error_type == "status_code":
                    if resp.status >= 200 and resp.status < 300:
                        result["found"] = True
                    else:
                        result["found"] = False
                elif error_type == "message":
                    body = ""
                    try:
                        body = await resp.text(errors="ignore")
                    except Exception:
                        pass
                    if error_msg and isinstance(error_msg, str):
                        if error_msg in body:
                            result["found"] = False
                        else:
                            result["found"] = True
                    elif error_msg and isinstance(error_msg, list):
                        found_error = False
                        for em in error_msg:
                            if em in body:
                                found_error = True
                                break
                        result["found"] = not found_error
                    else:
                        if resp.status >= 200 and resp.status < 300:
                            result["found"] = True
                elif error_type == "response_url":
                    final_url = str(resp.url)
                    if error_url:
                        if error_url in final_url:
                            result["found"] = False
                        else:
                            result["found"] = True
                    else:
                        if username.lower() in final_url.lower():
                            result["found"] = True
                        else:
                            result["found"] = False
                else:
                    if resp.status >= 200 and resp.status < 300:
                        result["found"] = True
        except asyncio.TimeoutError:
            result["error"] = "Timeout"
        except aiohttp.ClientError:
            result["error"] = "Connection error"
        except Exception as exc:
            result["error"] = str(exc)[:50]
        return result

    def _build_progress_embed(self, username, checked, total, found_count):
        pct = int((checked / total) * 100) if total > 0 else 0
        bar_len = 20
        filled = int(bar_len * checked / total) if total > 0 else 0
        bar = ""
        for i in range(bar_len):
            if i < filled:
                bar = bar + "\u2588"
            else:
                bar = bar + "\u2591"
        embed = discord.Embed(
            color=SHERLOCK_COLOR,
            title="Sherlock Search: " + username,
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = (
            "```\n" + bar + " " + str(pct) + "%\n```\n"
            + "**Checked:** " + str(checked) + " / " + str(total) + "\n"
            + "**Found:** " + str(found_count) + " accounts so far"
        )
        embed.set_footer(text="Searching across social networks...")
        return embed

    def _build_result_pages(self, username, results, per_page, show_not_found):
        found = []
        not_found = []
        errors = []
        for r in results:
            if r is None:
                continue
            if r.get("found"):
                found.append(r)
            elif r.get("error"):
                errors.append(r)
            else:
                not_found.append(r)
        found.sort(key=lambda x: x["site"].lower())
        pages = []
        # Summary page
        summary = discord.Embed(
            color=SHERLOCK_COLOR,
            title="Sherlock Results: " + username,
            timestamp=datetime.now(timezone.utc),
        )
        summary.description = (
            "**Username:** `" + username + "`\n"
            + "**Accounts Found:** " + str(len(found)) + "\n"
            + "**Sites Checked:** " + str(len(results)) + "\n"
            + "**Not Found:** " + str(len(not_found)) + "\n"
            + "**Errors:** " + str(len(errors))
        )
        if found:
            top_sites = found[:10]
            preview = ""
            for r in top_sites:
                preview = preview + "[" + r["site"] + "](" + r["url"] + ")\n"
            if len(found) > 10:
                preview = preview + "... and " + str(len(found) - 10) + " more"
            summary.add_field(
                name="Top Results",
                value=preview,
                inline=False,
            )
        summary.set_footer(
            text="Page 1 | Use reactions to navigate"
        )
        pages.append(summary)
        # Found pages
        if found:
            chunks = []
            current_chunk = []
            for r in found:
                current_chunk.append(r)
                if len(current_chunk) >= per_page:
                    chunks.append(current_chunk)
                    current_chunk = []
            if current_chunk:
                chunks.append(current_chunk)
            for ci, chunk in enumerate(chunks):
                embed = discord.Embed(
                    color=0x2ECC71,
                    title="Found Accounts: " + username,
                    timestamp=datetime.now(timezone.utc),
                )
                lines = ""
                for r in chunk:
                    lines = lines + "**" + r["site"] + "**\n"
                    lines = lines + r["url"] + "\n\n"
                embed.description = lines
                pnum = str(len(pages) + 1)
                embed.set_footer(
                    text="Page " + pnum + " | Found accounts"
                )
                pages.append(embed)
        # Not found page
        if show_not_found and not_found:
            nf_names = []
            for r in not_found:
                nf_names.append(r["site"])
            nf_names.sort()
            nf_text = ", ".join(nf_names)
            if len(nf_text) > 4000:
                nf_text = nf_text[:3990] + "..."
            embed = discord.Embed(
                color=0xE74C3C,
                title="Not Found: " + username,
                description=nf_text,
                timestamp=datetime.now(timezone.utc),
            )
            pnum = str(len(pages) + 1)
            embed.set_footer(
                text="Page " + pnum + " | Sites where username was not found"
            )
            pages.append(embed)
        # Error page
        if errors:
            err_lines = ""
            for r in errors[:30]:
                err_lines = err_lines + "**" + r["site"] + ":** " + str(r.get("error", "Unknown")) + "\n"
            if len(errors) > 30:
                err_lines = err_lines + "... and " + str(len(errors) - 30) + " more errors"
            embed = discord.Embed(
                color=0xF39C12,
                title="Errors: " + username,
                description=err_lines,
                timestamp=datetime.now(timezone.utc),
            )
            pnum = str(len(pages) + 1)
            embed.set_footer(
                text="Page " + pnum + " | Sites that had errors"
            )
            pages.append(embed)
        # Number all pages
        total_pages = len(pages)
        for i, page in enumerate(pages):
            old_footer = page.footer.text or ""
            page.set_footer(
                text=old_footer + " | " + str(i + 1) + "/" + str(total_pages)
            )
        return pages

    @commands.group(name="sherlock", aliases=["sk"])
    @commands.guild_only()
    async def sherlock(self, ctx):
        """Search usernames across social networks using Sherlock."""
        pass

    @sherlock.command(name="search", aliases=["find", "lookup"])
    async def sherlock_search(self, ctx, *, username: str):
        """Search for a username across hundreds of websites.

        **Example:**
        `[p]sherlock search johndoe`
        """
        guild_conf = await self.config.guild(ctx.guild).all()
        if not guild_conf.get("enabled", True):
            await ctx.send("Sherlock is disabled on this server.")
            return
        allowed = guild_conf.get("allowed_channels", [])
        if allowed and ctx.channel.id not in allowed:
            await ctx.send("Sherlock is not allowed in this channel.")
            return
        cooldown = guild_conf.get("cooldown", 30)
        remaining = self._check_cooldown(ctx.guild.id, ctx.author.id, cooldown)
        if remaining > 0:
            await ctx.send(
                "Please wait **"
                + str(int(remaining))
                + "** seconds before searching again."
            )
            return
        username = username.strip()
        if " " in username:
            await ctx.send("Username cannot contain spaces.")
            return
        if len(username) < 2:
            await ctx.send("Username must be at least 2 characters.")
            return
        if len(username) > 50:
            await ctx.send("Username must be 50 characters or less.")
            return
        if not self.sites_loaded or not self.sites_data:
            await ctx.send("Loading site database, please try again in a moment...")
            await self._load_sites_data()
            if not self.sites_loaded:
                await ctx.send("Failed to load site database.")
                return
        req_timeout = guild_conf.get("timeout", 10)
        per_page = guild_conf.get("max_results_per_page", 15)
        show_nf = guild_conf.get("show_not_found", False)
        nsfw_filter = guild_conf.get("nsfw_filter", True)
        # Filter sites
        sites_to_check = {}
        for site_name, site_info in self.sites_data.items():
            if not isinstance(site_info, dict):
                continue
            if not site_info.get("url"):
                continue
            if nsfw_filter and site_info.get("isNSFW", False):
                if not getattr(ctx.channel, "nsfw", False):
                    continue
            sites_to_check[site_name] = site_info
        total = len(sites_to_check)
        if total == 0:
            await ctx.send("No sites available to check.")
            return
        # Send initial progress embed
        progress_embed = self._build_progress_embed(username, 0, total, 0)
        progress_msg = await ctx.send(embed=progress_embed)
        # Run checks with concurrency limit
        results = []
        found_count = 0
        checked_count = 0
        semaphore = asyncio.Semaphore(20)
        update_interval = max(total // 10, 5)

        async def check_with_sem(sname, sinfo):
            async with semaphore:
                return await self._check_site(sname, sinfo, username, req_timeout)

        tasks = []
        for sname, sinfo in sites_to_check.items():
            task = asyncio.create_task(check_with_sem(sname, sinfo))
            tasks.append(task)

        last_update = 0
        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)
            checked_count = checked_count + 1
            if result and result.get("found"):
                found_count = found_count + 1
            since_update = checked_count - last_update
            if since_update >= update_interval or checked_count == total:
                try:
                    pe = self._build_progress_embed(
                        username, checked_count, total, found_count
                    )
                    await progress_msg.edit(embed=pe)
                    last_update = checked_count
                except (discord.NotFound, discord.HTTPException):
                    pass

        # Build result pages
        pages = self._build_result_pages(
            username, results, per_page, show_nf
        )

        if len(pages) == 1:
            try:
                await progress_msg.edit(embed=pages[0])
            except (discord.NotFound, discord.HTTPException):
                await ctx.send(embed=pages[0])
            return

        # Paginated view
        current_page = 0
        try:
            await progress_msg.edit(embed=pages[0])
        except (discord.NotFound, discord.HTTPException):
            progress_msg = await ctx.send(embed=pages[0])

        left_arrow = "\u25c0"
        right_arrow = "\u25b6"
        stop_btn = "\u23f9"

        await progress_msg.add_reaction(left_arrow)
        await progress_msg.add_reaction(right_arrow)
        await progress_msg.add_reaction(stop_btn)

        def check(reaction, user):
            if user.id != ctx.author.id:
                return False
            if reaction.message.id != progress_msg.id:
                return False
            return str(reaction.emoji) in [left_arrow, right_arrow, stop_btn]

        while True:
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=120.0, check=check
                )
            except asyncio.TimeoutError:
                try:
                    await progress_msg.clear_reactions()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break

            emoji = str(reaction.emoji)
            if emoji == right_arrow:
                current_page = current_page + 1
                if current_page >= len(pages):
                    current_page = 0
            elif emoji == left_arrow:
                current_page = current_page - 1
                if current_page < 0:
                    current_page = len(pages) - 1
            elif emoji == stop_btn:
                try:
                    await progress_msg.clear_reactions()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break

            try:
                await progress_msg.edit(embed=pages[current_page])
            except (discord.NotFound, discord.HTTPException):
                break

            try:
                await progress_msg.remove_reaction(reaction.emoji, user)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @sherlock.command(name="quick")
    async def sherlock_quick(self, ctx, *, username: str):
        """Quick search - only shows found accounts, no progress updates.

        **Example:**
        `[p]sherlock quick johndoe`
        """
        guild_conf = await self.config.guild(ctx.guild).all()
        if not guild_conf.get("enabled", True):
            await ctx.send("Sherlock is disabled on this server.")
            return
        cooldown = guild_conf.get("cooldown", 30)
        remaining = self._check_cooldown(ctx.guild.id, ctx.author.id, cooldown)
        if remaining > 0:
            await ctx.send(
                "Please wait **"
                + str(int(remaining))
                + "** seconds before searching again."
            )
            return
        username = username.strip()
        if " " in username:
            await ctx.send("Username cannot contain spaces.")
            return
        if len(username) < 2 or len(username) > 50:
            await ctx.send("Username must be 2-50 characters.")
            return
        if not self.sites_loaded or not self.sites_data:
            await ctx.send("Loading site database...")
            await self._load_sites_data()
            if not self.sites_loaded:
                await ctx.send("Failed to load site database.")
                return
        req_timeout = guild_conf.get("timeout", 10)
        nsfw_filter = guild_conf.get("nsfw_filter", True)
        sites_to_check = {}
        for sname, sinfo in self.sites_data.items():
            if not isinstance(sinfo, dict):
                continue
            if not sinfo.get("url"):
                continue
            if nsfw_filter and sinfo.get("isNSFW", False):
                if not getattr(ctx.channel, "nsfw", False):
                    continue
            sites_to_check[sname] = sinfo
        msg = await ctx.send("Searching for **" + username + "** across " + str(len(sites_to_check)) + " sites...")
        semaphore = asyncio.Semaphore(25)

        async def check_with_sem(sn, si):
            async with semaphore:
                return await self._check_site(sn, si, username, req_timeout)

        tasks = []
        for sn, si in sites_to_check.items():
            tasks.append(asyncio.create_task(check_with_sem(sn, si)))
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        found = []
        total_checked = 0
        for r in raw_results:
            if isinstance(r, Exception):
                total_checked = total_checked + 1
                continue
            if r is None:
                continue
            total_checked = total_checked + 1
            if r.get("found"):
                found.append(r)
        found.sort(key=lambda x: x["site"].lower())
        embed = discord.Embed(
            color=SHERLOCK_COLOR,
            title="Sherlock Results: " + username,
            timestamp=datetime.now(timezone.utc),
        )
        if found:
            desc = "**Found " + str(len(found)) + " accounts** across " + str(total_checked) + " sites:\n\n"
            for r in found:
                line = "**" + r["site"] + "** - " + r["url"] + "\n"
                if len(desc) + len(line) > 3900:
                    desc = desc + "... and " + str(len(found) - found.index(r)) + " more"
                    break
                desc = desc + line
            embed.description = desc
            embed.color = 0x2ECC71
        else:
            embed.description = (
                "No accounts found for **" + username + "** across "
                + str(total_checked) + " sites."
            )
            embed.color = 0xE74C3C
        embed.set_footer(text="Sherlock OSINT | " + str(total_checked) + " sites checked")
        try:
            await msg.edit(content=None, embed=embed)
        except (discord.NotFound, discord.HTTPException):
            await ctx.send(embed=embed)

    @sherlock.command(name="site")
    async def sherlock_site(self, ctx, site_name: str, *, username: str):
        """Check a specific site for a username.

        **Example:**
        `[p]sherlock site GitHub johndoe`
        """
        username = username.strip()
        if not self.sites_loaded or not self.sites_data:
            await ctx.send("Loading site database...")
            await self._load_sites_data()
            if not self.sites_loaded:
                await ctx.send("Failed to load site database.")
                return
        matched_site = None
        matched_info = None
        for sname, sinfo in self.sites_data.items():
            if sname.lower() == site_name.lower():
                matched_site = sname
                matched_info = sinfo
                break
        if matched_site is None:
            # Fuzzy match
            candidates = []
            for sname in self.sites_data.keys():
                if site_name.lower() in sname.lower():
                    candidates.append(sname)
            if len(candidates) == 1:
                matched_site = candidates[0]
                matched_info = self.sites_data[matched_site]
            elif len(candidates) > 1:
                clist = ", ".join("`" + c + "`" for c in candidates[:20])
                await ctx.send("Multiple matches found: " + clist)
                return
            else:
                await ctx.send("Site **" + site_name + "** not found in database.")
                return
        if not isinstance(matched_info, dict):
            await ctx.send("Invalid site data.")
            return
        async with ctx.typing():
            timeout = await self.config.guild(ctx.guild).timeout()
            result = await self._check_site(
                matched_site, matched_info, username, timeout
            )
        if result is None:
            await ctx.send("Could not check this site.")
            return
        embed = discord.Embed(timestamp=datetime.now(timezone.utc))
        if result.get("found"):
            embed.color = 0x2ECC71
            embed.title = "Found: " + username + " on " + matched_site
            embed.description = "**Profile URL:**\n" + result["url"]
        elif result.get("error"):
            embed.color = 0xF39C12
            embed.title = "Error checking " + matched_site
            embed.description = "**Error:** " + str(result["error"]) + "\n**URL:** " + result["url"]
        else:
            embed.color = 0xE74C3C
            embed.title = "Not Found: " + username + " on " + matched_site
            embed.description = "Username does not appear to exist on this site."
        if result.get("http_status"):
            embed.add_field(
                name="HTTP Status",
                value=str(result["http_status"]),
                inline=True,
            )
        embed.set_footer(text="Sherlock OSINT")
        await ctx.send(embed=embed)

    @sherlock.command(name="sites")
    async def sherlock_sites(self, ctx, search: Optional[str] = None):
        """List all available sites or search for a specific one.

        **Examples:**
        `[p]sherlock sites` - list all
        `[p]sherlock sites git` - search for sites containing "git"
        """
        if not self.sites_loaded or not self.sites_data:
            await ctx.send("Loading site database...")
            await self._load_sites_data()
            if not self.sites_loaded:
                await ctx.send("Failed to load site database.")
                return
        site_names = sorted(self.sites_data.keys(), key=str.lower)
        if search:
            search_lower = search.lower()
            site_names = [s for s in site_names if search_lower in s.lower()]
        if not site_names:
            await ctx.send("No sites found matching your search.")
            return
        total = len(site_names)
        chunk_size = 40
        chunks = []
        current = []
        for s in site_names:
            current.append(s)
            if len(current) >= chunk_size:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)
        pages = []
        for ci, chunk in enumerate(chunks):
            embed = discord.Embed(
                color=SHERLOCK_COLOR,
                title="Available Sites",
                timestamp=datetime.now(timezone.utc),
            )
            text = ", ".join("`" + s + "`" for s in chunk)
            if len(text) > 4000:
                text = text[:3990] + "..."
            embed.description = text
            pnum = str(ci + 1)
            ptotal = str(len(chunks))
            embed.set_footer(
                text="Page " + pnum + "/" + ptotal + " | " + str(total) + " sites total"
            )
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
            return
        current_page = 0
        msg = await ctx.send(embed=pages[0])
        left_arrow = "\u25c0"
        right_arrow = "\u25b6"
        await msg.add_reaction(left_arrow)
        await msg.add_reaction(right_arrow)

        def check(reaction, user):
            if user.id != ctx.author.id:
                return False
            if reaction.message.id != msg.id:
                return False
            return str(reaction.emoji) in [left_arrow, right_arrow]

        while True:
            try:
                reaction, user = await self.bot.wait_for(
                    "reaction_add", timeout=60.0, check=check
                )
            except asyncio.TimeoutError:
                try:
                    await msg.clear_reactions()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break
            emoji = str(reaction.emoji)
            if emoji == right_arrow:
                current_page = current_page + 1
                if current_page >= len(pages):
                    current_page = 0
            elif emoji == left_arrow:
                current_page = current_page - 1
                if current_page < 0:
                    current_page = len(pages) - 1
            try:
                await msg.edit(embed=pages[current_page])
            except (discord.NotFound, discord.HTTPException):
                break
            try:
                await msg.remove_reaction(reaction.emoji, user)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @sherlock.command(name="reload")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_reload(self, ctx):
        """Reload the site database from GitHub.

        **Example:**
        `[p]sherlock reload`
        """
        self.sites_loaded = False
        self.sites_data = {}
        async with ctx.typing():
            await self._load_sites_data()
        if self.sites_loaded:
            count = len(self.sites_data)
            await ctx.send("Reloaded **" + str(count) + "** sites from Sherlock database.")
        else:
            await ctx.send("Failed to reload site database.")

    @sherlock.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_settings(self, ctx):
        """View current Sherlock settings."""
        gd = await self.config.guild(ctx.guild).all()
        embed = discord.Embed(
            color=SHERLOCK_COLOR,
            title="Sherlock Settings",
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Enabled", value="Yes" if gd.get("enabled") else "No", inline=True)
        embed.add_field(name="Cooldown", value=str(gd.get("cooldown", 30)) + "s", inline=True)
        embed.add_field(name="Timeout", value=str(gd.get("timeout", 10)) + "s", inline=True)
        embed.add_field(name="Results/Page", value=str(gd.get("max_results_per_page", 15)), inline=True)
        embed.add_field(name="Show Not Found", value="Yes" if gd.get("show_not_found") else "No", inline=True)
        embed.add_field(name="NSFW Filter", value="Yes" if gd.get("nsfw_filter") else "No", inline=True)
        ac = gd.get("allowed_channels", [])
        if ac:
            ch_text = " ".join("<#" + str(c) + ">" for c in ac)
        else:
            ch_text = "All channels"
        embed.add_field(name="Allowed Channels", value=ch_text, inline=False)
        sc = len(self.sites_data) if self.sites_loaded else 0
        embed.add_field(name="Sites Loaded", value=str(sc), inline=True)
        await ctx.send(embed=embed)

    @sherlock.command(name="set")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_set(self, ctx, setting: str, *, value: str):
        """Change a Sherlock setting.

        **Settings:**
        `cooldown` - Seconds between searches per user (default: 30)
        `timeout` - HTTP request timeout in seconds (default: 10)
        `results_per_page` - Results per embed page (default: 15)
        `show_not_found` - Show not-found sites (true/false)
        `nsfw_filter` - Filter NSFW sites in non-NSFW channels (true/false)
        `enabled` - Enable/disable sherlock (true/false)

        **Examples:**
        `[p]sherlock set cooldown 60`
        `[p]sherlock set nsfw_filter false`
        """
        setting = setting.lower().strip()
        value = value.strip()
        if setting == "cooldown":
            try:
                val = int(value)
            except ValueError:
                await ctx.send("Cooldown must be a number.")
                return
            if val < 0 or val > 300:
                await ctx.send("Cooldown must be between 0 and 300.")
                return
            await self.config.guild(ctx.guild).cooldown.set(val)
            await ctx.send("Cooldown set to **" + str(val) + "** seconds.")
        elif setting == "timeout":
            try:
                val = int(value)
            except ValueError:
                await ctx.send("Timeout must be a number.")
                return
            if val < 3 or val > 30:
                await ctx.send("Timeout must be between 3 and 30.")
                return
            await self.config.guild(ctx.guild).timeout.set(val)
            await ctx.send("Request timeout set to **" + str(val) + "** seconds.")
        elif setting == "results_per_page":
            try:
                val = int(value)
            except ValueError:
                await ctx.send("Must be a number.")
                return
            if val < 5 or val > 30:
                await ctx.send("Must be between 5 and 30.")
                return
            await self.config.guild(ctx.guild).max_results_per_page.set(val)
            await ctx.send("Results per page set to **" + str(val) + "**.")
        elif setting == "show_not_found":
            val = value.lower() in ("true", "yes", "1", "on")
            await self.config.guild(ctx.guild).show_not_found.set(val)
            state = "enabled" if val else "disabled"
            await ctx.send("Show not-found **" + state + "**.")
        elif setting == "nsfw_filter":
            val = value.lower() in ("true", "yes", "1", "on")
            await self.config.guild(ctx.guild).nsfw_filter.set(val)
            state = "enabled" if val else "disabled"
            await ctx.send("NSFW filter **" + state + "**.")
        elif setting == "enabled":
            val = value.lower() in ("true", "yes", "1", "on")
            await self.config.guild(ctx.guild).enabled.set(val)
            state = "enabled" if val else "disabled"
            await ctx.send("Sherlock **" + state + "**.")
        else:
            await ctx.send(
                "Unknown setting. Available: `cooldown`, `timeout`, "
                "`results_per_page`, `show_not_found`, `nsfw_filter`, `enabled`"
            )

    @sherlock.command(name="addchannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_addchannel(self, ctx, channel: discord.TextChannel):
        """Restrict sherlock to a specific channel.

        **Example:**
        `[p]sherlock addchannel #osint`
        """
        async with self.config.guild(ctx.guild).allowed_channels() as ac:
            if channel.id not in ac:
                ac.append(channel.id)
        await ctx.send("Sherlock allowed in " + channel.mention)

    @sherlock.command(name="removechannel")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_removechannel(self, ctx, channel: discord.TextChannel):
        """Remove a channel restriction.

        **Example:**
        `[p]sherlock removechannel #osint`
        """
        async with self.config.guild(ctx.guild).allowed_channels() as ac:
            if channel.id in ac:
                ac.remove(channel.id)
        await ctx.send("Removed " + channel.mention + " from allowed channels.")

    @sherlock.command(name="clearchannels")
    @checks.admin_or_permissions(manage_guild=True)
    async def sherlock_clearchannels(self, ctx):
        """Clear channel restrictions (allow all channels).

        **Example:**
        `[p]sherlock clearchannels`
        """
        await self.config.guild(ctx.guild).allowed_channels.set([])
        await ctx.send("Channel restrictions cleared. Sherlock works in all channels now.")