"""
Naminter — OSINT username enumeration for Red-DiscordBot.

Runs lookups against the WhatsMyName dataset (700+ sites) using the
`naminter` library, and reports the profiles it finds as paginated embeds
or an exported report file.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu

#: Export formats offered by the export button: value -> modal description.
EXPORT_FORMATS: Tuple[Tuple[str, str, str], ...] = (
    ("json", "JSON", "Every field, best for further processing"),
    ("csv", "CSV", "One row per site, opens in a spreadsheet"),
    ("txt", "TXT", "Plain readable lines"),
)

#: Which results an export can contain: value -> (modal label, description).
EXPORT_SCOPES: Tuple[Tuple[str, str, str], ...] = (
    ("found", "Hits only", "Found, partial and ambiguous"),
    ("everything", "Everything", "Also misses, unknown and errors"),
)


def _import_naminter_library():
    """Import the naminter package, refusing a copy of this cog as a stand-in.

    Red puts the directory that holds a loaded cog on ``sys.path``, so any
    folder in there named ``naminter`` is imported instead of the installed
    library. The cog then dies with "cannot import name ... from partially
    initialized module" - which looks like a missing dependency but is a name
    collision. Say what to delete instead of leaving a cryptic traceback.
    """
    try:
        import naminter
        from naminter.core import exceptions as naminter_exceptions
        from naminter.core import models as naminter_models
    except ImportError as exc:
        raise RuntimeError(
            f"The naminter package could not be imported ({exc}). "
            "Install it with `[p]pipinstall naminter==1.0.9` and reload the cog. "
            "If a folder named `naminter` still sits in your cogs directory (an "
            "older copy of this cog), delete it first - Red puts that directory "
            "on sys.path, so such a folder shadows the library."
        ) from exc

    library_dir = os.path.dirname(os.path.abspath(naminter.__file__))
    cog_dir = os.path.dirname(os.path.abspath(__file__))
    if library_dir == cog_dir or os.path.dirname(library_dir) == os.path.dirname(
        cog_dir
    ):
        raise RuntimeError(
            f"`import naminter` resolved to {naminter.__file__} instead of the "
            "installed library. A folder named `naminter` inside your cogs "
            "directory shadows the package - delete it and reload the cog."
        )
    return naminter, naminter_exceptions, naminter_models


_lib, _lib_exceptions, _lib_models = _import_naminter_library()

CurlCFFISession = _lib.CurlCFFISession
WMNEngine = _lib.Naminter
WMN_DATA_URL = _lib.WMN_DATA_URL
NaminterError = _lib_exceptions.NaminterError
WMNUnknownCategoriesError = _lib_exceptions.WMNUnknownCategoriesError
WMNUnknownSiteError = _lib_exceptions.WMNUnknownSiteError
WMNMode = _lib_models.WMNMode
WMNStatus = _lib_models.WMNStatus

log = logging.getLogger("red.freak_cogs.namint")

#: Statuses that count as "the account exists".
FOUND_STATUSES: frozenset = frozenset(
    {WMNStatus.EXISTS, WMNStatus.PARTIAL_EXISTS, WMNStatus.CONFLICTING}
)

#: How long a downloaded dataset is considered fresh, in seconds.
DATA_TTL = 6 * 60 * 60

#: Statuses that mirror the upstream CLI's emoji language.
STATUS_EMOJI: Dict[Any, str] = {
    WMNStatus.EXISTS: "✅",
    WMNStatus.PARTIAL_EXISTS: "🟡",
    WMNStatus.CONFLICTING: "⚠️",
    WMNStatus.MISSING: "❌",
    WMNStatus.PARTIAL_MISSING: "➖",
    WMNStatus.UNKNOWN: "❔",
    WMNStatus.ERROR: "💥",
    WMNStatus.NOT_VALID: "⛔",
}

#: Human readable status labels used in exports.
STATUS_LABEL: Dict[Any, str] = {
    WMNStatus.EXISTS: "exists",
    WMNStatus.PARTIAL_EXISTS: "partial exists",
    WMNStatus.CONFLICTING: "conflicting",
    WMNStatus.MISSING: "missing",
    WMNStatus.PARTIAL_MISSING: "partial missing",
    WMNStatus.UNKNOWN: "unknown",
    WMNStatus.ERROR: "error",
    WMNStatus.NOT_VALID: "not valid",
}

#: Embed colours, in one place so the look stays consistent.
EMBED_COLOUR = discord.Colour.teal()
EMBED_COLOUR_DARK = discord.Colour.dark_teal()
EMBED_COLOUR_ERROR = discord.Colour.red()


DEFAULT_GLOBAL: Dict[str, Any] = {
    "http_timeout": 30,
    "max_concurrency": 25,
    "run_timeout": 240,
    "impersonate": "chrome",
    "default_limit": 0,
}

DEFAULT_GUILD: Dict[str, Any] = {
    "mode": "all",
    "categories": [],
    "exclude_categories": [],
    "show_missing": False,
    "allowed_roles": [],
    "max_pages": 12,
    "per_page": 12,
}

IMPERSONATE_CHOICES: Tuple[str, ...] = (
    "chrome",
    "chrome110",
    "firefox",
    "safari",
    "edge",
    "none",
)

HELP_TEXT = (
    "**Usage**\n"
    "`[p]naminter check <username> [options]`\n\n"
    "**Options**\n"
    "`-s`, `--sites a,b` — only these sites (exact names from `[p]naminter sites`)\n"
    "`-c`, `--category a,b` — only sites in these categories\n"
    "`-x`, `--exclude-category a,b` — skip these categories\n"
    "`-m`, `--mode all|any` — strict (AND) or loose (OR) detection\n"
    "`-l`, `--limit <n>` — check at most n sites\n"
    "`-e`, `--export json|csv|txt` — attach the report right away\n"
    "`-a`, `--all` — also list misses, unknowns and errors\n\n"
    "After a run, the **Export** button under the summary asks for format and "
    "scope and sends the file only to you.\n\n"
    "Access: everyone by default; server managers can limit it to roles with "
    "`[p]naminterset role add @role`.\n\n"
    "**Examples**\n"
    "`[p]naminter check torvalds`\n"
    "`[p]naminter check torvalds -c coding,social -l 100`\n"
    "`[p]naminter check neo -x dating -e json`\n"
)


@dataclass
class CheckArgs:
    """Parsed arguments of `[p]naminter check`."""

    usernames: List[str] = field(default_factory=list)
    sites: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    exclude_categories: List[str] = field(default_factory=list)
    mode: Optional[str] = None
    limit: Optional[int] = None
    export: Optional[str] = None
    show_all: bool = False


def _can_lookup():
    """Command check: open to everyone unless the server whitelisted roles."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.cog._lookup_allowed(ctx):
            return True
        raise commands.UserFeedbackCheckFailure(
            "Lookups in this server are limited to certain roles. Ask a server "
            "manager to give you one of the roles from `[p]naminterset role list`."
        )

    return commands.check(predicate)


#: Category settings and how they read in Discord: key -> (label, "nothing set" text).
CATEGORY_SETTINGS: Dict[str, Tuple[str, str]] = {
    "categories": ("Default categories", "all"),
    "exclude_categories": ("Excluded categories", "none"),
}


def _category_line(key: str, values: Sequence[str]) -> str:
    """Render one category setting as a single line for Discord."""
    label, empty = CATEGORY_SETTINGS[key]
    listed = ", ".join(f"`{item}`" for item in values) if values else f"*{empty}*"
    return f"{label}: {listed}"


async def answer_interaction_error(interaction: discord.Interaction) -> None:
    """Tell the user that something went wrong, whenever the reply is still open.

    Without this a failing component callback is only logged, Discord never gets
    an acknowledgement and the user sees "The application didn't respond in
    time" instead of an error.
    """
    message = "❌ Export failed — the bot log has the details."
    with contextlib.suppress(discord.HTTPException):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _export_options(
    choices: Tuple[Tuple[str, str, str], ...],
) -> List[discord.SelectOption]:
    """Turn a value/label/description table into select options."""
    return [
        discord.SelectOption(label=label, value=value, description=description)
        for value, label, description in choices
    ]


async def deliver_export(
    interaction: discord.Interaction, export_view: "ExportView", fmt: str, scope: str
) -> None:
    """Acknowledge, build the report and hand it to the requester only.

    A big report (hundreds of sites) plus the upload takes longer than the three
    seconds Discord allows for the first response, so the interaction is
    acknowledged with ``defer()`` first and the file follows as a followup.
    """
    results = export_view.results if scope == "everything" else export_view.found
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        payload, filename = export_view.cog._build_export(
            fmt,
            results=results,
            usernames=export_view.usernames,
            counts=export_view.counts,
            sites_checked=export_view.sites_checked,
        )
        await interaction.followup.send(
            content=(
                f"📄 `{filename}` — {len(results)} of {export_view.sites_checked} "
                f"checked sites, format `{fmt}`."
            ),
            file=discord.File(io.BytesIO(payload), filename=filename),
            ephemeral=True,
        )
    except discord.HTTPException:
        await interaction.followup.send(
            "❌ Could not send the file here — the bot is missing "
            "**Attach Files** in this channel.",
            ephemeral=True,
        )


class ExportModal(discord.ui.Modal):
    """Ask how the last lookup should be exported, then send the file privately.

    Nothing is attached to the channel message: the file only goes to the person
    who pressed the button, as an ephemeral reply.

    The selects sit inside ``Label`` containers on purpose - Discord accepts
    only bare text inputs in a modal and rejects an action-row-wrapped select
    with error 50035, so a select has to be wrapped (component type 18).
    ``ExportView.export_button`` falls back to :class:`ExportPickerView` if the
    API refuses the modal anyway.
    """

    format_select = discord.ui.Label(
        text="Format",
        description="How the report is written",
        component=discord.ui.Select(
            placeholder="Format", options=_export_options(EXPORT_FORMATS)
        ),
    )
    scope_select = discord.ui.Label(
        text="Which results",
        description="Hits only, or everything the run produced",
        component=discord.ui.Select(
            placeholder="Which results", options=_export_options(EXPORT_SCOPES)
        ),
    )

    def __init__(self, export_view: "ExportView") -> None:
        super().__init__(title="Naminter export", timeout=300)
        self.export_view = export_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Check the two choices and hand off to the delivery helper."""
        formats = self.format_select.component.values
        scopes = self.scope_select.component.values
        if not formats or not scopes:
            await interaction.response.send_message(
                "❌ No format or scope selected — press **Export** again.",
                ephemeral=True,
            )
            return
        if self.export_view.results is None:
            await interaction.response.send_message(
                "⏳ These results are no longer available.",
                ephemeral=True,
            )
            return
        await deliver_export(interaction, self.export_view, formats[0], scopes[0])

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        """Answer the user instead of letting the interaction run out of time."""
        log.exception("Namint: export modal failed", exc_info=error)
        await answer_interaction_error(interaction)


class ExportPickerView(discord.ui.View):
    """Fallback picker: the same two choices as selects in a normal message.

    Selects in a plain message are supported by every API version, so this is
    what :meth:`ExportView.export_button` falls back to when the modal is
    refused - the export never depends on the modal working.
    """

    def __init__(self, export_view: "ExportView") -> None:
        super().__init__(timeout=180)
        self.export_view = export_view
        self.format_select = discord.ui.Select(
            placeholder="Format", options=_export_options(EXPORT_FORMATS)
        )
        self.scope_select = discord.ui.Select(
            placeholder="Which results", options=_export_options(EXPORT_SCOPES)
        )
        self.add_item(self.format_select)
        self.add_item(self.scope_select)

    @discord.ui.button(
        label="Send file", emoji="📄", style=discord.ButtonStyle.primary, row=2
    )
    async def send_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Deliver the export with the two selected values."""
        if not self.format_select.values or not self.scope_select.values:
            await interaction.response.send_message(
                "❌ Pick a format and a scope first.", ephemeral=True
            )
            return
        await deliver_export(
            interaction,
            self.export_view,
            self.format_select.values[0],
            self.scope_select.values[0],
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: Any
    ) -> None:
        """Answer the user instead of letting the interaction run out of time."""
        log.exception("Namint: export picker failed", exc_info=error)
        await answer_interaction_error(interaction)


class ExportView(discord.ui.View):
    """One lookup's results plus the button that exports them on demand.

    The button carries a fixed ``custom_id`` and a data-less instance is
    registered in ``cog_load``, so a click on an old message (after a restart,
    a reconnect or a cog reload) still gets an answer instead of running into
    Discord's three second timeout.
    """

    BUTTON_ID = "namint:export"

    def __init__(
        self,
        cog: "Naminter",
        *,
        author_id: Optional[int] = None,
        usernames: Sequence[str] = (),
        results: Optional[Sequence[Any]] = None,
        found: Sequence[Any] = (),
        counts: Optional[Dict[str, int]] = None,
        sites_checked: int = 0,
        timeout: Optional[int] = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.usernames = list(usernames)
        self.results = None if results is None else list(results)
        self.found = list(found)
        self.counts = dict(counts or {})
        self.sites_checked = sites_checked

    @classmethod
    def placeholder(cls, cog: "Naminter") -> "ExportView":
        """The data-less instance registered at startup for leftover buttons."""
        return cls(cog, timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the person who started the lookup may export its results."""
        if self.author_id is None:  # startup placeholder, its callback explains
            return True
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"Only <@{self.author_id}> can export this run. Start your own "
                "with `[p]naminter check <username>`.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Export",
        emoji="📄",
        style=discord.ButtonStyle.primary,
        custom_id=BUTTON_ID,
    )
    async def export_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        """Open the export form, or explain that the run is gone."""
        if self.results is None:
            await interaction.response.send_message(
                "⏳ These results are no longer available — the bot restarted, the "
                "cog was reloaded, or the message is older than ten minutes. Run "
                "`[p]naminter check <username>` again.",
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(ExportModal(self))
        except discord.HTTPException as exc:
            # 50035 means Discord refused the modal payload itself (its form
            # validation). Anything else is a real error and belongs in the log.
            if getattr(exc, "code", None) != 50035:
                raise
            log.info(
                "Namint: API declined the export modal (%s), using the picker", exc
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    content=(
                        "Pick the **format** and the **scope**, then press "
                        "**Send file**."
                    ),
                    view=ExportPickerView(self),
                    ephemeral=True,
                )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: Any
    ) -> None:
        """Answer the user instead of letting the interaction run out of time."""
        log.exception("Namint: export button failed", exc_info=error)
        await answer_interaction_error(interaction)


class Naminter(commands.Cog):
    """Enumerate usernames across 700+ sites with the WhatsMyName dataset."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x4E414D4954, force_registration=True
        )
        self.config.register_global(**DEFAULT_GLOBAL)
        self.config.register_guild(**DEFAULT_GUILD)

        self._data: Optional[Dict[str, Any]] = None
        self._data_stamp: float = 0.0
        self._loaded_at: Optional[float] = None
        self._data_source: str = "not loaded"
        self._load_lock = asyncio.Lock()
        self._load_task: Optional[asyncio.Task] = None

        self._engine: Optional[WMNEngine] = None
        self._http: Optional[CurlCFFISession] = None
        self._engine_stamp: float = -1.0
        self._engine_settings: Tuple[Any, ...] = ()
        self._engine_lock = asyncio.Lock()

        self._active_runs: int = 0
        self._idle = asyncio.Event()
        self._idle.set()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        """Warm the dataset cache and register the export button for old messages."""
        self._load_task = asyncio.create_task(self._background_load())
        # Persistent, data-less instance: a click on a button whose run is gone
        # (restart, reconnect, reload) gets an explanation instead of Discord's
        # "The application didn't respond in time".
        self.bot.add_view(ExportView.placeholder(self))

    async def cog_unload(self) -> None:
        """Cancel background work and release the HTTP session."""
        if self._load_task is not None:
            self._load_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._load_task
            self._load_task = None
        await self._close_engine(force=True)

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Nothing is stored per user; lookups are not persisted anywhere."""
        return

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Refuse lookups outside a server: every setting here is per guild."""
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def _background_load(self) -> None:
        """Download the dataset without blocking cog loading."""
        try:
            await self._load_dataset()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning(
                "Naminter: could not preload the WhatsMyName dataset; "
                "will retry on the first lookup.",
                exc_info=True,
            )

    # ------------------------------------------------------------------ #
    # dataset handling
    # ------------------------------------------------------------------ #

    @property
    def _cache_file(self) -> Path:
        return self.bot.cog_data_path(self) / "wmn-data.json"

    async def _load_dataset(self, *, force: bool = False) -> Dict[str, Any]:
        """Return the WhatsMyName dataset, downloading it at most once per TTL."""
        async with self._load_lock:
            fresh = (
                not force
                and self._data is not None
                and self._loaded_at is not None
                and (time.time() - self._loaded_at) < DATA_TTL
            )
            if fresh:
                return self._data

            data: Optional[Dict[str, Any]] = None
            source = "network"
            try:
                data = await self._download(WMN_DATA_URL)
            except Exception as exc:
                log.warning("Naminter: dataset download failed (%s), falling back", exc)
                data = await self._read_cache()
                source = "disk cache"
                if data is None:
                    raise

            if not isinstance(data, dict) or not data.get("sites"):
                raise ValueError("The WhatsMyName dataset did not contain any sites.")

            self._data = data
            self._loaded_at = time.time()
            self._data_stamp = self._loaded_at
            self._data_source = source
            if source == "network":
                await self._write_cache(data)
            log.info(
                "Naminter: loaded %d sites from %s", len(data.get("sites", [])), source
            )
            return data

    async def _download(self, url: str) -> Dict[str, Any]:
        """Fetch and parse a JSON document with browser impersonation."""
        settings = await self.config.all()
        session = CurlCFFISession(
            verify=False,
            timeout=settings["http_timeout"],
            allow_redirects=True,
            impersonate=(
                None if settings["impersonate"] == "none" else settings["impersonate"]
            ),
        )
        await session.open()
        try:
            response = await session.get(url)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code} for {url}")
            if not response.text or not response.text.strip():
                raise RuntimeError(f"empty response from {url}")
            return json.loads(response.text)
        finally:
            await session.close()

    async def _read_cache(self) -> Optional[Dict[str, Any]]:
        """Read the on-disk dataset copy, or return ``None`` if there is none."""
        path = self._cache_file
        if not path.exists():
            return None
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            log.warning("Naminter: on-disk dataset copy is unreadable", exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    async def _write_cache(self, data: Dict[str, Any]) -> None:
        """Persist the dataset so a failed download still leaves a usable copy."""
        path = self._cache_file
        try:
            payload = json.dumps(data)
            await asyncio.to_thread(path.write_text, payload, encoding="utf-8")
        except OSError:
            log.warning("Naminter: could not write the dataset cache", exc_info=True)

    # ------------------------------------------------------------------ #
    # engine handling
    # ------------------------------------------------------------------ #

    async def _current_settings(self) -> Tuple[Any, ...]:
        """Engine-relevant global settings, used to detect configuration changes."""
        settings = await self.config.all()
        return (
            settings["http_timeout"],
            settings["max_concurrency"],
            settings["impersonate"],
        )

    async def _ensure_engine(self) -> WMNEngine:
        """Return a ready engine, rebuilding it when data or settings changed."""
        async with self._engine_lock:
            settings = await self._current_settings()
            if (
                self._engine is not None
                and self._engine_stamp == self._data_stamp
                and self._engine_settings == settings
            ):
                return self._engine

            if self._data is None or (
                self._loaded_at is not None
                and (time.time() - self._loaded_at) > DATA_TTL
            ):
                try:
                    await self._load_dataset()
                except Exception as exc:
                    raise commands.UserFeedbackCheckFailure(
                        "The WhatsMyName dataset is unavailable right now "
                        f"({exc}). Try `[p]naminter refresh` in a moment."
                    ) from exc

            await self._close_engine(force=False)

            impersonate = settings[2]
            session = CurlCFFISession(
                verify=False,
                timeout=settings[0],
                allow_redirects=False,
                impersonate=None if impersonate == "none" else impersonate,
            )
            engine = WMNEngine(
                http_client=session,
                data=self._data,
                schema=None,
                max_tasks=settings[1],
            )
            try:
                await engine.open()
            except NaminterError as exc:
                with contextlib.suppress(Exception):
                    await session.close()
                raise commands.UserFeedbackCheckFailure(
                    f"The WhatsMyName dataset failed validation: {exc}"
                ) from exc
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await session.close()
                raise commands.UserFeedbackCheckFailure(
                    f"Could not open an HTTP session for the lookups: {exc}"
                ) from exc

            self._engine = engine
            self._http = session
            self._engine_settings = settings
            self._engine_stamp = self._data_stamp
            return engine

    async def _close_engine(self, *, force: bool) -> None:
        """Close the engine once no lookup is using it."""
        if self._engine is None and self._http is None:
            return
        if self._active_runs and not force:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=300)
        engine, self._engine = self._engine, None
        session, self._http = self._http, None
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        elif session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        self._engine_stamp = -1.0
        self._engine_settings = ()

    @contextlib.asynccontextmanager
    async def _running(self):
        """Track how many lookups are in flight so the engine is not yanked away."""
        self._active_runs += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._active_runs -= 1
            if not self._active_runs:
                self._idle.set()

    # ------------------------------------------------------------------ #
    # permissions
    # ------------------------------------------------------------------ #

    async def _lookup_allowed(self, ctx: commands.Context) -> bool:
        """Whether the author may run OSINT lookups in this context.

        Lookups are open to every member by default. Adding at least one role
        with `[p]naminterset role add` switches the server to whitelist mode:
        then only server managers and those roles may look up.
        """
        if await self.bot.is_owner(ctx.author):
            return True
        guild = ctx.guild
        if guild is None:
            return False
        member = ctx.author
        is_manager = isinstance(member, discord.Member) and (
            member.guild_permissions.manage_guild
            or member.guild_permissions.administrator
        )
        allowed = await self.config.guild(guild).allowed_roles()
        if not allowed:
            return True
        if is_manager:
            return True
        return any(
            getattr(role, "id", None) in allowed
            for role in getattr(member, "roles", [])
        )

    # ------------------------------------------------------------------ #
    # argument parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_check_args(raw: str) -> CheckArgs:
        """Parse the free-form argument string of `[p]naminter check`."""
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

        flags = {
            "-s": "sites",
            "--sites": "sites",
            "--site": "sites",
            "-c": "categories",
            "--category": "categories",
            "--categories": "categories",
            "-x": "exclude_categories",
            "--exclude-category": "exclude_categories",
            "--exclude-categories": "exclude_categories",
            "-m": "mode",
            "--mode": "mode",
            "-l": "limit",
            "--limit": "limit",
            "-e": "export",
            "--export": "export",
        }
        bools = {"-a": "show_all", "--all": "show_all"}

        args = CheckArgs()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in bools:
                setattr(args, bools[token], True)
                index += 1
                continue
            if token in flags:
                target = flags[token]
                if index + 1 >= len(tokens):
                    raise ValueError(f"`{token}` needs a value.")
                value = tokens[index + 1]
                if target in ("sites", "categories", "exclude_categories"):
                    values = [item.strip() for item in value.split(",") if item.strip()]
                    getattr(args, target).extend(values)
                elif target == "mode":
                    value = value.lower()
                    if value not in ("all", "any"):
                        raise ValueError("`--mode` only accepts `all` or `any`.")
                    args.mode = value
                elif target == "limit":
                    try:
                        limit = int(value)
                    except ValueError:
                        raise ValueError("`--limit` needs a whole number.") from None
                    if limit < 1:
                        raise ValueError("`--limit` must be at least 1.")
                    args.limit = limit
                elif target == "export":
                    value = value.lower()
                    if value not in ("json", "csv", "txt"):
                        raise ValueError(
                            "`--export` only accepts `json`, `csv` or `txt`."
                        )
                    args.export = value
                index += 2
                continue
            if token.startswith("-") and len(token) > 1:
                raise ValueError(f"Unknown option `{token}`.")
            args.usernames.extend(
                name.strip() for name in token.split(",") if name.strip()
            )
            index += 1

        args.usernames = list(dict.fromkeys(args.usernames))
        if not args.usernames:
            raise ValueError("You need to give me at least one username.")
        if len(args.usernames) > 3:
            raise ValueError("Please check at most 3 usernames per run.")
        too_long = [name for name in args.usernames if len(name) > 64]
        if too_long:
            raise ValueError("Usernames must be 64 characters or fewer.")
        return args

    # ------------------------------------------------------------------ #
    # rendering helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _status_line(result: Any, *, with_username: bool = False) -> str:
        """One markdown line for a single enumeration result."""
        emoji = STATUS_EMOJI.get(result.status, "❔")
        label = STATUS_LABEL.get(result.status, str(result.status))
        url = result.uri_pretty or result.uri_check
        if url:
            line = f"{emoji} [{result.name}]({url})"
        else:
            line = f"{emoji} {result.name}"
        details = [f"`{result.category}`"]
        if result.status not in (WMNStatus.EXISTS, WMNStatus.MISSING):
            details.append(label)
        if with_username:
            details.append(f"`{result.username}`")
        return f"{line} · {' · '.join(details)}"

    def _result_pages(
        self,
        results: Sequence[Any],
        *,
        per_page: int,
        max_pages: int,
        usernames: Sequence[str],
        mode: str,
    ) -> Tuple[List[discord.Embed], int]:
        """Build the paginated embeds for the result list."""
        with_username = len(usernames) > 1
        per_page = max(5, min(per_page, 20))
        chunks = [
            results[index : index + per_page]
            for index in range(0, len(results), per_page)
        ]
        shown = min(len(chunks), max_pages)
        pages: List[discord.Embed] = []
        for number, chunk in enumerate(chunks[:shown], start=1):
            embed = discord.Embed(
                title=f"Naminter · {' '.join(usernames)}",
                description="\n".join(
                    self._status_line(item, with_username=with_username)
                    for item in chunk
                ),
                colour=EMBED_COLOUR,
            )
            embed.set_footer(
                text=(
                    f"Page {number}/{shown} · {len(results)} entries · "
                    f"mode {mode} · source: WhatsMyName dataset"
                )
            )
            pages.append(embed)
        return pages, len(results) - min(len(results), shown * per_page)

    def _summary_embed(
        self,
        *,
        usernames: Sequence[str],
        counts: Dict[str, int],
        sites_checked: int,
        total_sites: int,
        duration: float,
        mode: str,
        timed_out: bool,
    ) -> discord.Embed:
        """Header embed describing what was checked and what came back."""
        embed = discord.Embed(
            title=f"Naminter · {' '.join(usernames)}",
            colour=EMBED_COLOUR_DARK,
        )
        embed.add_field(
            name="Sites checked",
            value=(
                f"{sites_checked}/{total_sites}"
                if sites_checked != total_sites
                else str(sites_checked)
            ),
            inline=True,
        )
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(name="Duration", value=f"{duration:.1f}s", inline=True)

        found = sum(counts.get(status.value, 0) for status in FOUND_STATUSES)
        lines = [f"✅ **{found}** found"]
        extra = [
            (status, counts.get(status.value, 0))
            for status in (
                WMNStatus.PARTIAL_EXISTS,
                WMNStatus.CONFLICTING,
                WMNStatus.MISSING,
                WMNStatus.PARTIAL_MISSING,
                WMNStatus.UNKNOWN,
                WMNStatus.ERROR,
            )
            if counts.get(status.value, 0)
        ]
        for status, count in extra:
            lines.append(f"{STATUS_EMOJI[status]} {count} {STATUS_LABEL[status]}")
        embed.add_field(name="Results", value="\n".join(lines), inline=False)

        if timed_out:
            embed.add_field(
                name="Timed out",
                value=(
                    "The run hit the wall-clock limit, so the list is incomplete. "
                    "Narrow it down with `--limit`, `--category` or "
                    "`[p]naminterset runtimeout`."
                ),
                inline=False,
            )
        embed.set_footer(text=f"Dataset source: {self._data_source}")
        return embed

    @staticmethod
    def _build_export(
        fmt: str,
        *,
        results: Sequence[Any],
        usernames: Sequence[str],
        counts: Dict[str, int],
        sites_checked: int,
    ) -> Tuple[bytes, str]:
        """Render the full result set as a downloadable file."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rows = [
            {
                "site": item.name,
                "category": item.category,
                "username": item.username,
                "status": item.status.value,
                "url": item.uri_pretty or item.uri_check,
                "status_code": item.status_code,
                "elapsed_ms": (
                    round(item.elapsed.total_seconds() * 1000) if item.elapsed else None
                ),
                "error": item.error,
            }
            for item in results
        ]
        if fmt == "json":
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "usernames": list(usernames),
                "sites_checked": sites_checked,
                "counts": counts,
                "results": rows,
            }
            body = json.dumps(payload, indent=2, ensure_ascii=False)
        elif fmt == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer, fieldnames=list(rows[0].keys()) if rows else []
            )
            if rows:
                writer.writeheader()
                writer.writerows(rows)
            body = buffer.getvalue()
        else:
            lines = [
                f"Naminter report for {', '.join(usernames)}",
                f"generated: {datetime.now(timezone.utc).isoformat()}",
                f"sites checked: {sites_checked}",
                f"counts: {counts}",
                "",
            ]
            for item in sorted(rows, key=lambda row: (row["username"], row["site"])):
                lines.append(
                    f"[{item['status']}] {item['username']} @ {item['site']} "
                    f"({item['category']}) {item['url'] or ''}"
                    + (f" err={item['error']}" if item["error"] else "")
                )
            body = "\n".join(lines)
        name = f"naminter_{'_'.join(usernames)[:40]}_{stamp}.{fmt}"
        return body.encode("utf-8"), name

    # ------------------------------------------------------------------ #
    # lookup commands
    # ------------------------------------------------------------------ #

    @commands.group(
        name="naminter", aliases=["nim", "wmn"], invoke_without_command=True
    )
    async def naminter(self, ctx: commands.Context) -> None:
        """OSINT username enumeration with the WhatsMyName dataset."""
        if ctx.invoked_subcommand is None:
            await ctx.send(HELP_TEXT)

    @naminter.command(name="check", aliases=["lookup", "scan", "enumerate"])
    @_can_lookup()
    @commands.bot_has_permissions(
        embed_links=True, add_reactions=True, attach_files=True
    )
    @commands.max_concurrency(2, commands.BucketType.default, wait=True)
    @commands.cooldown(5, 60, commands.BucketType.user)
    async def naminter_check(
        self, ctx: commands.Context, *, arguments: str = ""
    ) -> None:
        """Check a username against the WhatsMyName sites.

        See `[p]naminter` for the options, or use e.g.
        `[p]naminter check torvalds -c coding -l 100`.
        """
        if not arguments.strip():
            await ctx.send(HELP_TEXT)
            return
        try:
            args = self._parse_check_args(arguments)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}\n\n{HELP_TEXT}")
            return

        guild_settings = await self.config.guild(ctx.guild).all()
        global_settings = await self.config.all()
        mode_name = args.mode or guild_settings["mode"]
        mode = WMNMode.ANY if mode_name == "any" else WMNMode.ALL
        show_missing = args.show_all or guild_settings["show_missing"]

        include = args.categories or guild_settings["categories"]
        exclude = args.exclude_categories or guild_settings["exclude_categories"]

        engine = await self._ensure_engine()
        try:
            summary = engine.summary(
                site_names=args.sites or None,
                include_categories=include or None,
                exclude_categories=exclude or None,
            )
        except WMNUnknownSiteError as exc:
            await ctx.send(
                f"❌ Unknown site(s): `{', '.join(sorted(getattr(exc, 'site_names', []) or []))}`. "
                "Search them with `[p]naminter sites <text>`."
            )
            return
        except WMNUnknownCategoriesError as exc:
            await ctx.send(
                f"❌ Unknown category: "
                f"`{', '.join(sorted(getattr(exc, 'categories', []) or []))}`. "
                "See `[p]naminter categories`."
            )
            return

        all_sites = list(summary.site_names)
        limit = args.limit or global_settings["default_limit"] or 0
        site_names = (
            all_sites[:limit] if limit and len(all_sites) > limit else all_sites
        )
        if not site_names:
            await ctx.send("❌ No site matches those filters — nothing to check.")
            return

        with contextlib.suppress(discord.HTTPException):
            await ctx.typing()
        progress = await ctx.send(
            embed=discord.Embed(
                title=f"Naminter · {' '.join(args.usernames)}",
                description=f"Checking {len(site_names)} sites…",
                colour=EMBED_COLOUR_DARK,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        started = time.monotonic()
        try:
            async with self._running():
                results, timed_out = await self._run_enumeration(
                    engine,
                    args.usernames,
                    site_names,
                    mode,
                    timeout=global_settings["run_timeout"],
                    progress=progress,
                )
        except (NaminterError, OSError) as exc:
            log.exception("Naminter: enumeration failed")
            await progress.edit(
                embed=discord.Embed(
                    title="Naminter · lookup failed",
                    description=f"```{exc}```",
                    colour=EMBED_COLOUR_ERROR,
                )
            )
            return
        duration = time.monotonic() - started

        counts: Dict[str, int] = {}
        for item in results:
            counts[item.status.value] = counts.get(item.status.value, 0) + 1

        if show_missing:
            displayed = [item for item in results if item.status != WMNStatus.NOT_VALID]
        else:
            displayed = [item for item in results if item.status in FOUND_STATUSES]
        displayed.sort(key=lambda item: (item.username, item.name.lower()))

        header = self._summary_embed(
            usernames=args.usernames,
            counts=counts,
            sites_checked=len(site_names),
            total_sites=len(all_sites),
            duration=duration,
            mode=mode_name,
            timed_out=timed_out,
        )
        pages, not_shown = self._result_pages(
            displayed,
            per_page=guild_settings["per_page"],
            max_pages=guild_settings["max_pages"],
            usernames=args.usernames,
            mode=mode_name,
        )
        if not_shown:
            header.add_field(
                name="Not shown",
                value=(
                    f"{not_shown} more entries — export everything with `--export json`"
                    " or raise `[p]naminterset maxpages`."
                ),
                inline=False,
            )

        await progress.delete()
        mentions = discord.AllowedMentions.none()
        export_view = self._export_view(
            author=ctx.author,
            usernames=args.usernames,
            results=results,
            found=[item for item in results if item.status in FOUND_STATUSES],
            counts=counts,
            sites_checked=len(site_names),
        )
        if args.export:
            try:
                payload, filename = self._build_export(
                    args.export,
                    results=results,
                    usernames=args.usernames,
                    counts=counts,
                    sites_checked=len(site_names),
                )
                await ctx.send(
                    embed=header,
                    file=discord.File(io.BytesIO(payload), filename=filename),
                    view=export_view,
                    allowed_mentions=mentions,
                )
            except discord.HTTPException:
                await ctx.send(
                    embed=header, view=export_view, allowed_mentions=mentions
                )
                await ctx.send(
                    "⚠️ Could not attach the export file — press **Export** instead, "
                    "or ask an admin to give the bot **Attach Files** here."
                )
        else:
            await ctx.send(embed=header, view=export_view, allowed_mentions=mentions)

        if pages:
            await menu(ctx, pages, DEFAULT_CONTROLS, timeout=180)
        elif not displayed:
            note = (
                "No hits — but the full run is in the export."
                if args.export
                else "No hits. Add `--all` to see misses and errors."
            )
            await ctx.send(note, allowed_mentions=mentions)

    def _export_view(
        self,
        *,
        author: discord.abc.User,
        usernames: Sequence[str],
        results: Sequence[Any],
        found: Sequence[Any],
        counts: Dict[str, int],
        sites_checked: int,
    ) -> ExportView:
        """Build the view that carries the run's results behind an export button."""
        return ExportView(
            self,
            author_id=author.id,
            usernames=usernames,
            results=results,
            found=found,
            counts=counts,
            sites_checked=sites_checked,
        )

    async def _run_enumeration(
        self,
        engine: WMNEngine,
        usernames: Sequence[str],
        site_names: Sequence[str],
        mode: WMNMode,
        *,
        timeout: int,
        progress: Optional[discord.Message],
    ) -> Tuple[List[Any], bool]:
        """Stream the enumeration, editing ``progress`` while it runs."""
        results: List[Any] = []
        timed_out = False
        last_update = 0.0
        label = " ".join(usernames)

        def progress_embed(done: int) -> discord.Embed:
            return discord.Embed(
                title=f"Naminter · {label}",
                description=(
                    f"Checked **{done}/{len(site_names)}** sites…\n"
                    f"Found so far: **{sum(1 for r in results if r.status in FOUND_STATUSES)}**"
                ),
                colour=EMBED_COLOUR_DARK,
            )

        try:
            async with asyncio.timeout(timeout):
                async for result in engine.enumerate_usernames(
                    list(usernames),
                    site_names=list(site_names),
                    mode=mode,
                    exclude_text=True,
                ):
                    results.append(result)
                    if progress is None:
                        continue
                    now = time.monotonic()
                    if now - last_update < 3:
                        continue
                    last_update = now
                    with contextlib.suppress(discord.HTTPException):
                        await progress.edit(embed=progress_embed(len(results)))
        except asyncio.TimeoutError:
            timed_out = True
            log.info(
                "Naminter: run for %s timed out after %ss (%d results)",
                label,
                timeout,
                len(results),
            )
        return results, timed_out

    @naminter.command(name="sites", aliases=["list"])
    @_can_lookup()
    @commands.bot_has_permissions(embed_links=True, add_reactions=True)
    async def naminter_sites(self, ctx: commands.Context, *, query: str = "") -> None:
        """List the sites of the WhatsMyName dataset.

        Optionally filter by a name or category fragment, e.g.
        `[p]naminter sites github`.
        """
        if self._data is None:
            try:
                await self._load_dataset()
            except Exception as exc:
                await ctx.send(f"❌ Dataset unavailable: {exc}")
                return
        needle = query.strip().lower()
        max_pages = await self.config.guild(ctx.guild).max_pages()
        entries = [
            (site.get("name", "?"), site.get("cat", "?"))
            for site in self._data.get("sites", [])
            if not needle
            or needle in site.get("name", "").lower()
            or needle in site.get("cat", "").lower()
        ]
        entries.sort(key=lambda item: (item[1].lower(), item[0].lower()))
        if not entries:
            await ctx.send(f"No site matches `{query}`.")
            return

        lines = [f"{name} · {category}" for name, category in entries]
        body = "\n".join(lines)
        chunks = list(pagify(body, page_length=1800, delims=["\n"]))
        pages = chunks[:max_pages]
        embeds = [
            discord.Embed(
                title=f"Naminter sites ({len(entries)} match{'' if len(entries) == 1 else 'es'})",
                description=chunk,
                colour=EMBED_COLOUR,
            ).set_footer(text=f"Page {number}/{len(pages)}")
            for number, chunk in enumerate(pages, start=1)
        ]
        if len(chunks) > max_pages:
            embeds[-1].set_footer(
                text=f"Page {len(pages)}/{len(chunks)} · truncated, refine the filter"
            )
        if len(embeds) == 1:
            await ctx.send(embed=embeds[0])
        else:
            await menu(ctx, embeds, DEFAULT_CONTROLS, timeout=180)

    @naminter.command(name="categories", aliases=["cats"])
    @_can_lookup()
    @commands.bot_has_permissions(embed_links=True)
    async def naminter_categories(self, ctx: commands.Context) -> None:
        """Show every category of the dataset with its site count."""
        if self._data is None:
            try:
                await self._load_dataset()
            except Exception as exc:
                await ctx.send(f"❌ Dataset unavailable: {exc}")
                return
        counter: Dict[str, int] = {}
        for site in self._data.get("sites", []):
            category = site.get("cat", "?")
            counter[category] = counter.get(category, 0) + 1
        if not counter:
            await ctx.send("The dataset did not contain any categories.")
            return
        lines = [
            f"{category} · {count}"
            for category, count in sorted(
                counter.items(), key=lambda item: (-item[1], item[0])
            )
        ]
        embed = discord.Embed(
            title=f"Naminter categories ({len(counter)})",
            description=box("\n".join(lines), lang="yaml"),
            colour=EMBED_COLOUR,
        )
        embed.set_footer(text="Use `-c <category>` to limit a check to one category")
        await ctx.send(embed=embed)

    @naminter.command(name="stats", aliases=["info", "dataset"])
    @_can_lookup()
    @commands.bot_has_permissions(embed_links=True)
    async def naminter_stats(self, ctx: commands.Context) -> None:
        """Show dataset and engine information."""
        if self._data is None:
            try:
                await self._load_dataset()
            except Exception as exc:
                await ctx.send(f"❌ Dataset unavailable: {exc}")
                return
        global_settings = await self.config.all()
        guild_settings = await self.config.guild(ctx.guild).all()
        sites = self._data.get("sites", [])
        categories = {site.get("cat", "?") for site in sites}
        known = sum(len(site.get("known", [])) for site in sites)
        embed = discord.Embed(
            title="Naminter dataset",
            description="Username enumeration over the WhatsMyName list.",
            colour=EMBED_COLOUR,
        )
        embed.add_field(name="Sites", value=str(len(sites)), inline=True)
        embed.add_field(name="Categories", value=str(len(categories)), inline=True)
        embed.add_field(name="Known usernames", value=str(known), inline=True)
        embed.add_field(
            name="Source",
            value=f"{self._data_source}\n[WhatsMyName]({WMN_DATA_URL})",
            inline=False,
        )
        if self._loaded_at:
            loaded = datetime.fromtimestamp(self._loaded_at, tz=timezone.utc)
            embed.add_field(
                name="Loaded",
                value=f"{discord.utils.format_dt(loaded, 'R')} "
                f"({discord.utils.format_dt(loaded, 'f')})",
                inline=False,
            )
        authors = self._data.get("authors") or []
        if authors:
            embed.add_field(
                name="Authors", value=humanize_list(list(authors)), inline=False
            )
        engine_state = "running" if self._active_runs else "idle"
        embed.add_field(
            name="Engine",
            value=(
                f"concurrency `{global_settings['max_concurrency']}` · "
                f"http timeout `{global_settings['http_timeout']}s` · "
                f"run timeout `{global_settings['run_timeout']}s` · "
                f"impersonate `{global_settings['impersonate']}` · "
                f"{engine_state}"
            ),
            inline=False,
        )
        embed.add_field(
            name="This server",
            value=(
                f"mode `{guild_settings['mode']}` · "
                f"categories `{', '.join(guild_settings['categories']) or 'all'}` · "
                f"excluded `{', '.join(guild_settings['exclude_categories']) or 'none'}` · "
                f"show misses `{guild_settings['show_missing']}` · "
                f"roles `{len(guild_settings['allowed_roles'])}`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @naminter.command(name="refresh", aliases=["reload"])
    @commands.admin_or_permissions(manage_guild=True)
    async def naminter_refresh(self, ctx: commands.Context) -> None:
        """Re-download the WhatsMyName dataset and rebuild the engine."""
        async with ctx.typing():
            try:
                await self._load_dataset(force=True)
            except Exception as exc:
                await ctx.send(f"❌ Refresh failed: {exc}")
                return
        await self._close_engine(force=False)
        sites = len((self._data or {}).get("sites", []))
        await ctx.send(
            f"✅ Dataset refreshed — {sites} sites loaded from {self._data_source}."
        )

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    @commands.group(name="naminterset", aliases=["nimset"], invoke_without_command=True)
    async def naminterset(self, ctx: commands.Context) -> None:
        """Configure Naminter."""
        await ctx.send_help()

    @naminterset.command(name="show")
    @commands.bot_has_permissions(embed_links=True)
    async def naminterset_show(self, ctx: commands.Context) -> None:
        """Show the current settings."""
        guild_settings = await self.config.guild(ctx.guild).all()
        global_settings = await self.config.all()
        roles = [
            role.mention
            for role in ctx.guild.roles
            if role.id in guild_settings["allowed_roles"]
        ]
        embed = discord.Embed(title="Naminter settings", colour=EMBED_COLOUR)
        embed.add_field(
            name="This server",
            value=(
                f"mode: `{guild_settings['mode']}`\n"
                f"categories: `{', '.join(guild_settings['categories']) or 'all'}`\n"
                f"excluded categories: "
                f"`{', '.join(guild_settings['exclude_categories']) or 'none'}`\n"
                f"show misses by default: `{guild_settings['show_missing']}`\n"
                f"pages / entries per page: `{guild_settings['max_pages']}` / "
                f"`{guild_settings['per_page']}`\n"
                f"lookup access: "
                f"{humanize_list(roles) if roles else 'everyone (unrestricted)'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Bot wide",
            value=(
                f"concurrency: `{global_settings['max_concurrency']}`\n"
                f"http timeout: `{global_settings['http_timeout']}s`\n"
                f"run timeout: `{global_settings['run_timeout']}s`\n"
                f"impersonate: `{global_settings['impersonate']}`\n"
                f"default site limit: "
                f"`{global_settings['default_limit'] or 'unlimited'}`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    @naminterset.command(name="mode")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_mode(self, ctx: commands.Context, mode: str) -> None:
        """Set the default detection mode: `all` (strict) or `any` (loose)."""
        mode = mode.lower()
        if mode not in ("all", "any"):
            await ctx.send("❌ Mode must be `all` or `any`.")
            return
        await self.config.guild(ctx.guild).mode.set(mode)
        await ctx.send(f"✅ Default detection mode is now `{mode}`.")

    async def _edit_category_setting(
        self, ctx: commands.Context, key: str, action: str, categories: str
    ) -> None:
        """Shared `add`/`remove`/`clear`/`list` handling for both category settings."""
        action = action.lower()
        setting = getattr(self.config.guild(ctx.guild), key)
        current = await setting()
        if action == "list":
            await ctx.send(_category_line(key, current))
            return
        if action == "clear":
            await setting.set([])
            await ctx.send("✅ " + _category_line(key, []))
            return
        if action not in ("add", "remove"):
            await ctx.send("❌ Use `add`, `remove`, `clear` or `list`.")
            return
        requested = [
            item.strip().lower() for item in categories.split(",") if item.strip()
        ]
        if not requested:
            await ctx.send("❌ Name at least one category.")
            return
        known = {site.get("cat", "") for site in (self._data or {}).get("sites", [])}
        unknown = [item for item in requested if known and item not in known]
        if unknown:
            await ctx.send(
                f"❌ Unknown category: `{', '.join(unknown)}`. "
                "See `[p]naminter categories`."
            )
            return
        if action == "add":
            updated = sorted(set(current) | set(requested))
        else:
            updated = [item for item in current if item not in requested]
        await setting.set(updated)
        await ctx.send("✅ " + _category_line(key, updated))

    @naminterset.command(name="categories")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_categories(
        self, ctx: commands.Context, action: str = "list", *, categories: str = ""
    ) -> None:
        """Categories lookups are limited to: `add`, `remove`, `clear` or `list`."""
        await self._edit_category_setting(ctx, "categories", action, categories)

    @naminterset.command(name="exclude")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_exclude(
        self, ctx: commands.Context, action: str = "list", *, categories: str = ""
    ) -> None:
        """Categories lookups skip: `add`, `remove`, `clear` or `list`."""
        await self._edit_category_setting(ctx, "exclude_categories", action, categories)

    @naminterset.command(name="showmissing", aliases=["showall"])
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_showmissing(
        self, ctx: commands.Context, enabled: bool
    ) -> None:
        """Show misses, unknowns and errors by default without `--all`."""
        await self.config.guild(ctx.guild).show_missing.set(enabled)
        await ctx.send(
            f"✅ Misses and errors are now {'shown' if enabled else 'hidden'} by default."
        )

    @naminterset.command(name="maxpages")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_maxpages(self, ctx: commands.Context, pages: int) -> None:
        """Cap how many result pages a lookup may post (1-40)."""
        if not 1 <= pages <= 40:
            await ctx.send("❌ Choose between 1 and 40 pages.")
            return
        await self.config.guild(ctx.guild).max_pages.set(pages)
        await ctx.send(f"✅ At most {pages} result pages per lookup.")

    @naminterset.command(name="perpage")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_perpage(self, ctx: commands.Context, entries: int) -> None:
        """How many results go on one page (5-20)."""
        if not 5 <= entries <= 20:
            await ctx.send("❌ Choose between 5 and 20 entries per page.")
            return
        await self.config.guild(ctx.guild).per_page.set(entries)
        await ctx.send(f"✅ {entries} entries per page.")

    @naminterset.command(name="role")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_role(
        self,
        ctx: commands.Context,
        action: str = "list",
        role: Optional[discord.Role] = None,
    ) -> None:
        """Restrict lookups to roles — empty means everyone may look up."""
        action = action.lower()
        current = await self.config.guild(ctx.guild).allowed_roles()
        if action == "list":
            resolved = [r.mention for r in ctx.guild.roles if r.id in current]
            await ctx.send(
                "Lookups are restricted to: "
                + (
                    ", ".join(resolved) + " (and server managers)"
                    if resolved
                    else "*nobody is restricted* — every member may look up"
                )
            )
            return
        if action == "clear":
            await self.config.guild(ctx.guild).allowed_roles.set([])
            await ctx.send(
                "✅ Restriction removed — every member may run lookups again."
            )
            return
        if action not in ("add", "remove"):
            await ctx.send("❌ Use `add`, `remove`, `clear` or `list`.")
            return
        if role is None:
            await ctx.send("❌ Mention a role with that action.")
            return
        if action == "add":
            updated = sorted(set(current) | {role.id})
        else:
            updated = [item for item in current if item != role.id]
        await self.config.guild(ctx.guild).allowed_roles.set(updated)
        if not updated:
            await ctx.send(
                "✅ Restriction removed — every member may run lookups again."
            )
        else:
            await ctx.send(
                f"✅ Lookups are now limited to {role.mention} (and server managers)."
                if action == "add"
                else f"✅ {role.mention} can no longer run lookups; access is "
                "unchanged for everyone else."
            )

    @naminterset.command(name="reset")
    @commands.admin_or_permissions(manage_guild=True)
    async def naminterset_reset(self, ctx: commands.Context) -> None:
        """Reset this server's Naminter settings."""
        await self.config.guild(ctx.guild).clear()
        await ctx.send("✅ Server settings reset to defaults.")

    @naminterset.command(name="concurrency")
    @commands.is_owner()
    async def naminterset_concurrency(self, ctx: commands.Context, tasks: int) -> None:
        """Max parallel site checks (1-100, bot owner only)."""
        if not 1 <= tasks <= 100:
            await ctx.send("❌ Choose between 1 and 100.")
            return
        await self.config.max_concurrency.set(tasks)
        await self._close_engine(force=False)
        await ctx.send(f"✅ Concurrency set to {tasks}; the engine was rebuilt.")

    @naminterset.command(name="timeout")
    @commands.is_owner()
    async def naminterset_timeout(self, ctx: commands.Context, seconds: int) -> None:
        """HTTP timeout per site request in seconds (5-120, bot owner only)."""
        if not 5 <= seconds <= 120:
            await ctx.send("❌ Choose between 5 and 120 seconds.")
            return
        await self.config.http_timeout.set(seconds)
        await self._close_engine(force=False)
        await ctx.send(f"✅ HTTP timeout set to {seconds}s; the engine was rebuilt.")

    @naminterset.command(name="runtimeout")
    @commands.is_owner()
    async def naminterset_runtimeout(self, ctx: commands.Context, seconds: int) -> None:
        """Wall-clock limit for a single lookup in seconds (30-1800, owner only)."""
        if not 30 <= seconds <= 1800:
            await ctx.send("❌ Choose between 30 and 1800 seconds.")
            return
        await self.config.run_timeout.set(seconds)
        await ctx.send(f"✅ A single lookup may run for up to {seconds}s.")

    @naminterset.command(name="impersonate")
    @commands.is_owner()
    async def naminterset_impersonate(
        self, ctx: commands.Context, profile: str
    ) -> None:
        """Browser profile used for requests (bot owner only)."""
        profile = profile.lower()
        if profile not in IMPERSONATE_CHOICES:
            await ctx.send(
                "❌ Supported profiles: "
                + ", ".join(f"`{item}`" for item in IMPERSONATE_CHOICES)
            )
            return
        await self.config.impersonate.set(profile)
        await self._close_engine(force=False)
        await ctx.send(f"✅ Impersonating `{profile}`; the engine was rebuilt.")

    @naminterset.command(name="defaultlimit")
    @commands.is_owner()
    async def naminterset_defaultlimit(self, ctx: commands.Context, sites: int) -> None:
        """Default cap on sites per lookup, `0` for unlimited (bot owner only)."""
        if sites < 0 or sites > 2000:
            await ctx.send("❌ Choose between 0 (unlimited) and 2000.")
            return
        await self.config.default_limit.set(sites)
        await ctx.send(
            f"✅ Lookups check at most {sites} sites by default."
            if sites
            else "✅ Lookups check every matching site by default."
        )


async def setup(bot: Red) -> None:
    """Load the Naminter cog."""
    await bot.add_cog(Naminter(bot))
