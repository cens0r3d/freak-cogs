"""Username enumeration across the Sherlock site list.

Pure logic, no network and no framework: the site list and the rules that turn
one HTTP response into a verdict live here, so they can be tested on their own.
The engine is the one the `osint user` command runs — it reads Sherlock's
public ``data.json`` instead of depending on the sherlock package, which keeps
the cog free of requirements.

The interesting part is :func:`classify`. A site only says "this username
exists" through the shape its own error page has, and the three shapes in the
dataset are handled differently:

``status_code``   the site answers 404 for a missing profile, so any 2xx is a
                  hit — except that plenty of sites answer 200 for everything,
                  which is what the soft-404 guard is for.
``message``       the site answers 200 and puts an error sentence in the body,
                  so the body has to be read and searched. A body that could not
                  be read is *not* a hit.
``response_url``  the site redirects a missing profile elsewhere, so the final
                  URL decides.

Anything the rules cannot settle becomes ``unsure`` rather than a hit, and the
result embed says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Sherlock's released site list (~480 sites, JSON).
DATA_URL = "https://raw.githubusercontent.com/sherlock-project/sherlock/master/sherlock_project/resources/data.json"
#: Name of the on-disk copy inside the cog's data directory.
DATA_FILENAME = "sherlock-data.json"
#: How long the downloaded list may be reused.
DATA_MAX_AGE = 7 * 24 * 3600

#: Phrases that mean "no such profile" on sites that otherwise answer 200.
#: Only used to downgrade a status_code hit to *unsure*, never to decide a miss.
SOFT_404_MARKERS = (
    "user not found",
    "user doesn't exist",
    "user does not exist",
    "this user does not exist",
    "no user found",
    "page not found",
    "404 not found",
    "profil existiert nicht",
    "sorry, nobody on",
)

VERDICT_HIT = "hit"
VERDICT_UNSURE = "unsure"
VERDICT_MISS = "miss"
VERDICT_ERROR = "error"

#: URL fragments that turn a 200 into "we are looking at a challenge, not a
#: profile". Anything here means the response carries no information about the
#: username, so it belongs in *unsure* rather than in the hit list.
CHALLENGE_URL_MARKERS = (
    "verify-human",
    "verify_human",
    "verify.html",
    "captcha",
    "cf-chl",
    "challenge-platform",
    "bot-check",
    "are-you-a-human",
)

#: The same for the page text. A challenge page is short and says one of these.
CHALLENGE_BODY_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "enable javascript and cookies",
    "are you a robot",
    "unusual traffic",
    "verify you are human",
    "cf-error",
)

#: URL fragments that are the site's own "no such user" page, seen on sites that
#: answer 200 with a static error URL instead of a 404.
NOT_FOUND_URL_MARKERS = (
    "/notfound",
    "/not-found",
    "/not_found",
    "doesnotexist",
    "does-not-exist",
    "no-such-user",
    "nonexistent",
    "profile-not-found",
    "user-not-found",
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


@dataclass(frozen=True)
class Site:
    """One entry of the Sherlock dataset, normalised."""

    name: str
    url: str
    error_type: str
    error_msg: Tuple[str, ...]
    error_url: str
    regex: str
    is_nsfw: bool
    claimed: str


def valid_username(username: str) -> bool:
    """Whether the value is worth sending to a few hundred sites."""
    return bool(_USERNAME_RE.match((username or "").strip()))


def parse_sites(payload: Any) -> Dict[str, Site]:
    """Normalize the dataset, dropping the schema header and broken entries."""
    if not isinstance(payload, Mapping):
        return {}
    sites: Dict[str, Site] = {}
    for name, info in payload.items():
        if name.startswith("$") or not isinstance(info, Mapping):
            continue
        url = str(info.get("url") or "").strip()
        if not url or "{}" not in url:
            continue
        messages = info.get("errorMsg")
        if isinstance(messages, str):
            message_list = (messages,)
        elif isinstance(messages, Iterable):
            message_list = tuple(str(item) for item in messages if item)
        else:
            message_list = ()
        sites[str(name).strip()] = Site(
            name=str(name).strip(),
            url=url,
            error_type=str(info.get("errorType") or "").strip().lower(),
            error_msg=message_list,
            error_url=str(info.get("errorUrl") or "").strip(),
            regex=str(info.get("regexCheck") or "").strip(),
            is_nsfw=bool(info.get("isNSFW")),
            claimed=str(info.get("username_claimed") or ""),
        )
    return sites


def matches_site(site: Site, username: str) -> bool:
    """Apply the site's own username pattern before wasting a request on it."""
    if not site.regex:
        return True
    try:
        return re.search(site.regex, username) is not None
    except re.error:
        return True


def profile_url(site: Site, username: str) -> str:
    """The URL a found profile would live at."""
    return site.url.replace("{}", username)


def select_sites(
    sites: Mapping[str, Site],
    username: str,
    *,
    allow_nsfw: bool = False,
    names: Optional[Sequence[str]] = None,
    limit: int = 0,
) -> List[Site]:
    """Pick the sites to check, alphabetically, applying filters in one place."""
    chosen = []
    wanted = {name.lower() for name in names} if names else None
    for name in sorted(sites, key=str.lower):
        site = sites[name]
        if wanted is not None and name.lower() not in wanted:
            continue
        if site.is_nsfw and not allow_nsfw:
            continue
        if not matches_site(site, username):
            continue
        chosen.append(site)
        if limit and len(chosen) >= limit:
            break
    return chosen


def classify(
    site: Site,
    *,
    status: Optional[int],
    body: str,
    final_url: str,
    username: str,
) -> Tuple[str, str]:
    """Turn one response into ``(verdict, reason)``.

    ``verdict`` is one of ``hit``, ``unsure`` or ``miss``; transport failures are
    the caller's business and never reach here.
    """
    username_lower = (username or "").lower()
    url_lower = (final_url or "").lower()
    body_lower = (body or "").lower()
    error_url = site.error_url.lower()

    if status is None:
        return VERDICT_UNSURE, "keine Antwort"

    if error_url and error_url in url_lower and status < 400:
        return VERDICT_MISS, "auf die Fehler-URL umgeleitet"

    challenge = _challenge_marker(url_lower, body_lower)
    if challenge:
        return VERDICT_UNSURE, f"Bot-Schutz statt Profil ({challenge})"

    verdict, reason = _verdict_for_type(
        site, status, body_lower, url_lower, username_lower
    )

    if verdict == VERDICT_HIT:
        # A 200 whose URL is the site's own "no such user" page is not a hit —
        # `aniworld.to/profil/notFound` answered exactly that way.
        missing = _missing_url_marker(url_lower)
        if missing:
            return VERDICT_UNSURE, f"HTTP {status}, aber die Ziel-URL ist „{missing}“"

    return verdict, reason


def _verdict_for_type(
    site: Site,
    status: int,
    body_lower: str,
    url_lower: str,
    username_lower: str,
) -> Tuple[str, str]:
    """The per-``errorType`` rules, with the normalising work already done."""
    if site.error_type == "message":
        if status >= 400:
            return VERDICT_MISS, f"HTTP {status}"
        if not body_lower:
            return VERDICT_UNSURE, "Antwort konnte nicht gelesen werden"
        for marker in site.error_msg:
            if marker.lower() in body_lower:
                return VERDICT_MISS, "Fehlertext der Seite gefunden"
        if 200 <= status < 300:
            return VERDICT_HIT, f"HTTP {status}, kein Fehlertext"
        return VERDICT_UNSURE, f"HTTP {status}"

    if site.error_type == "response_url":
        if status >= 400:
            return VERDICT_MISS, f"HTTP {status}"
        if username_lower and username_lower in url_lower:
            return VERDICT_HIT, "Profil-URL bleibt bestehen"
        return VERDICT_UNSURE, "Ziel-URL nennt den Namen nicht"

    # status_code sites (and anything without an errorType)
    if status in (404, 410):
        return VERDICT_MISS, f"HTTP {status}"
    if 200 <= status < 300:
        for marker in SOFT_404_MARKERS:
            if marker in body_lower:
                return (
                    VERDICT_UNSURE,
                    "HTTP 200, aber die Seite meldet 'nicht gefunden'",
                )
        return VERDICT_HIT, f"HTTP {status}"
    if 300 <= status < 400:
        return VERDICT_UNSURE, f"HTTP {status}"
    return VERDICT_MISS, f"HTTP {status}"


def _challenge_marker(url_lower: str, body_lower: str) -> str:
    """The bot-wall a site put in front of us, or an empty string.

    Sites answer a challenge page with a 200 and no not-found text, so without
    this the engine reports a profile for every blocked request — measured on a
    60-site run, one of 14 "hits" was Apple's `verify-human/verify.html`.
    """
    for marker in CHALLENGE_URL_MARKERS:
        if marker in url_lower:
            return marker
    for marker in CHALLENGE_BODY_MARKERS:
        if marker in body_lower:
            return marker
    return ""


def _missing_url_marker(url_lower: str) -> str:
    """The "no such user" fragment in a final URL, or an empty string."""
    for marker in NOT_FOUND_URL_MARKERS:
        if marker in url_lower:
            return marker
    return ""


@dataclass
class Check:
    """One finished site check."""

    site: Site
    verdict: str
    reason: str
    status: Optional[int] = None
    url: str = ""

    @property
    def found(self) -> bool:
        return self.verdict == VERDICT_HIT

    @property
    def unsure(self) -> bool:
        return self.verdict == VERDICT_UNSURE


def summarize(checks: Sequence[Check]) -> Dict[str, Any]:
    """Counts and sorted lists for the report."""
    hits = sorted(
        (c for c in checks if c.verdict == VERDICT_HIT),
        key=lambda c: c.site.name.lower(),
    )
    unsure = sorted(
        (c for c in checks if c.verdict == VERDICT_UNSURE),
        key=lambda c: c.site.name.lower(),
    )
    misses = sorted(
        (c for c in checks if c.verdict == VERDICT_MISS),
        key=lambda c: c.site.name.lower(),
    )
    errors = sorted(
        (c for c in checks if c.verdict == VERDICT_ERROR),
        key=lambda c: c.site.name.lower(),
    )
    return {
        "checked": len(checks),
        "hits": hits,
        "unsure": unsure,
        "misses": misses,
        "errors": errors,
    }


def snapshot(checks: Sequence[Check], verdicts: Sequence[str]) -> List[Dict[str, Any]]:
    """JSON-friendly rows for the export button."""
    wanted = set(verdicts)
    return [
        {
            "site": check.site.name,
            "verdict": check.verdict,
            "reason": check.reason,
            "status": check.status,
            "url": check.url,
            "error_type": check.site.error_type,
        }
        for check in checks
        if check.verdict in wanted
    ]
