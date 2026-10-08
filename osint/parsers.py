"""Pure-stdlib helpers for the OSINT cog.

Nothing here touches the network or Discord. Target classification, DNS/RDAP
record shaping, HTTP security-header grading and attachment metadata extraction
(EXIF, PNG text chunks, PDF info dictionary) live here so that they can be
tested on their own.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import struct
import zlib
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ----------------------------------------------------------------- targets

_RE_MAC = re.compile(r"^(?:[0-9a-f]{2}[:\-]){5}[0-9a-f]{2}$|^[0-9a-f]{12}$", re.I)
_RE_ASN = re.compile(r"^as(\d{1,10})$", re.I)
_RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[a-z]{2,63}$", re.I)
_RE_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$", re.I)
_RE_HEX = re.compile(r"^[0-9a-f]+$", re.I)


def classify_target(text: str) -> str:
    """Guess what the user typed.

    Returns one of ``ip``, ``ipv6``, ``domain``, ``url``, ``asn``, ``mac``,
    ``email``, ``hash`` or ``unknown``.
    """
    value = (text or "").strip().strip("<>").strip()
    if not value:
        return "unknown"

    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return "url"
    if "://" in value:
        return "unknown"
    if _RE_EMAIL.match(value):
        return "email"
    if _RE_MAC.match(value):
        return "mac"
    if _RE_ASN.match(value):
        return "asn"

    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return "ip" if addr.version == 4 else "ipv6"

    if "/" in value:
        host = value.split("/", 1)[0].split("@")[-1]
        if host and _is_domain(host):
            return "url"
    if _is_domain(value):
        return "domain"
    if len(value) in (32, 40, 64) and _RE_HEX.match(value):
        return "hash"
    return "unknown"


def _is_domain(value: str) -> bool:
    if len(value) > 253 or "." not in value:
        return False
    labels = value.rstrip(".").split(".")
    if any(not _RE_LABEL.match(label) for label in labels):
        return False
    # A TLD is never all digits (that pattern is an IP, handled earlier).
    return not labels[-1].isdigit()


def normalize_url(value: str) -> str:
    """Add a scheme to a bare host/path so it can actually be fetched."""
    value = (value or "").strip().strip("<>")
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value
    return value


def host_of(value: str) -> str:
    """Extract the hostname from a URL or bare host, without userinfo/port."""
    candidate = (value or "").strip()
    if "://" in candidate:
        without_scheme = candidate.split("://", 1)[1]
    else:
        without_scheme = candidate
    authority = without_scheme.split("/", 1)[0]
    authority = authority.split("@")[-1]
    if authority.startswith("["):  # [::1]:443
        return authority.split("]", 1)[0].strip("[")
    return authority.split(":", 1)[0]


#: Ranges that must never be contacted but that ``ipaddress.is_private`` does
#: not cover: carrier-grade NAT and the IETF protocol assignments.
_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
)


def is_public_address(value: str) -> bool:
    """True only for a globally routable address (blocks SSRF-ish targets)."""
    try:
        addr = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if any(
        addr in network for network in _EXTRA_BLOCKED if network.version == addr.version
    ):
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_public_hostname(host: str) -> bool:
    """Reject local-only names before any request is made to them."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"}:
        return False
    if host.endswith((".local", ".internal", ".localdomain", ".home.arpa", ".lan")):
        return False
    return "." in host


def mac_details(value: str) -> Dict[str, Any]:
    """Normalize a MAC address and read its OUI flag bits."""
    digits = re.sub(r"[^0-9a-f]", "", (value or "").lower())
    if len(digits) != 12:
        return {}
    octets = [digits[i : i + 2] for i in range(0, 12, 2)]
    first = int(octets[0], 16)
    return {
        "display": ":".join(octets),
        "oui": ":".join(octets[:3]).upper(),
        "nic": ":".join(octets[3:]).upper(),
        "multicast": bool(first & 0x01),
        "locally_administered": bool(first & 0x02),
    }


# -------------------------------------------------------------------- DNS

DNS_TYPES = {
    1: "A",
    2: "NS",
    5: "CNAME",
    6: "SOA",
    15: "MX",
    16: "TXT",
    28: "AAAA",
    257: "CAA",
}


def doh_answers(payload: Mapping[str, Any], rtype: str) -> List[str]:
    """Pull the answers of one record type out of a DoH JSON response.

    ``data`` for TXT/MX/CAA comes back with the surrounding quotes, so those are
    unwrapped here.
    """
    records: List[str] = []
    for answer in payload.get("Answer") or []:
        if DNS_TYPES.get(answer.get("type")) != rtype:
            continue
        data = str(answer.get("data", "")).strip()
        if rtype == "TXT":
            # Chunks are already unquoted; join them so a split SPF or DKIM
            # record reads as one string again.
            data = " ".join(part.strip() for part in _split_quoted(data)).strip()
        elif rtype == "MX":
            data = _format_mx(data)
        elif rtype == "CAA":
            data = data.strip()
        if data:
            records.append(data)
    return records


def _format_mx(data: str) -> str:
    """``0 mail.example.com.`` becomes ``mail.example.com (pref 0)``.

    A null MX (``0 .``, RFC 7505) is spelled out, because "this domain accepts
    no mail at all" is a finding, not a formatting detail.
    """
    parts = data.split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return data.rstrip(".")
    host = parts[1].strip().rstrip(".")
    if not host:
        return f"null MX — Domain nimmt keine Mail an (pref {parts[0]})"
    return f"{host} (pref {parts[0]})"


def _split_quoted(data: str) -> List[str]:
    """Split a DoH character-string list into chunks with the quotes removed.

    DNS TXT records longer than 255 bytes arrive as several quoted strings that
    belong together; the quotes are framing, not content.
    """
    parts: List[str] = []
    current = ""
    in_quotes = False
    escaped = False
    for char in data:
        if escaped:
            current += char
            escaped = False
            continue
        if char == "\\":
            current += char
            escaped = True
            continue
        if char == '"':
            if in_quotes:
                parts.append(current)
                current = ""
            in_quotes = not in_quotes
            continue
        current += char
    if current.strip():
        parts.append(current)
    return parts or [data]


def doh_authority(payload: Mapping[str, Any], rtype: str = "SOA") -> List[str]:
    """Same as :func:`doh_answers` but for the authority section (NXDOMAIN SOA)."""
    records: List[str] = []
    for section in ("Authority", "Additional"):
        for answer in payload.get(section) or []:
            if DNS_TYPES.get(answer.get("type")) == rtype:
                data = str(answer.get("data", "")).strip()
                if data:
                    records.append(data)
    return records


DNS_STATUS = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}


def dns_status(payload: Mapping[str, Any]) -> str:
    return DNS_STATUS.get(
        int(payload.get("Status", -1)), f"status {payload.get('Status')}"
    )


# ------------------------------------------------------------------- RDAP


def _vcard_name(entity: Mapping[str, Any]) -> str:
    array = entity.get("vcardArray")
    if not isinstance(array, list) or len(array) < 2 or not isinstance(array[1], list):
        return ""
    for item in array[1]:
        if isinstance(item, list) and item and item[0] == "fn" and len(item) > 3:
            value = item[3]
            if isinstance(value, list):
                value = " ".join(str(part) for part in value)
            return str(value)
    return ""


def rdap_entities(rdap: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """Flatten RDAP entities to ``(role, name)`` pairs, nested ones included."""
    found: List[Tuple[str, str]] = []

    def walk(entities: Any, depth: int = 0) -> None:
        if depth > 3 or not isinstance(entities, list):
            return
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            name = _vcard_name(entity) or str(entity.get("handle", ""))
            roles = entity.get("roles") or ["entity"]
            for role in roles:
                if name:
                    found.append((str(role), name))
            walk(entity.get("entities"), depth + 1)

    walk(rdap.get("entities"))
    return found


def rdap_summary(rdap: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize a domain / ip / autnum RDAP record into flat fields."""
    summary: Dict[str, Any] = {
        "handle": str(rdap.get("handle", "") or ""),
        "name": str(
            rdap.get("ldhName") or rdap.get("name") or rdap.get("startAddress") or ""
        ),
        "status": [str(s) for s in (rdap.get("status") or [])],
        "events": {},
        "nameservers": [],
        "entities": rdap_entities(rdap),
        "cidrs": [],
        "range": "",
        "country": "",
        "remarks": "",
    }

    for event in rdap.get("events") or []:
        if isinstance(event, dict) and event.get("eventAction"):
            summary["events"][str(event["eventAction"])] = str(
                event.get("eventDate", "")
            )[:10]

    for server in rdap.get("nameservers") or []:
        if isinstance(server, dict):
            name = str(server.get("ldhName") or server.get("unicodeName") or "").lower()
            if name:
                summary["nameservers"].append(name)

    for cidr in rdap.get("cidr0_cidrs") or []:
        if isinstance(cidr, dict):
            prefix = cidr.get("v4prefix") or cidr.get("v6prefix")
            if prefix:
                summary["cidrs"].append(f"{prefix}/{cidr.get('length')}")

    start, end = rdap.get("startAddress"), rdap.get("endAddress")
    if start and end:
        summary["range"] = f"{start} - {end}"

    for entity in rdap.get("entities") or []:
        if (
            isinstance(entity, dict)
            and entity.get("country")
            and not summary["country"]
        ):
            summary["country"] = str(entity["country"])

    for remark in rdap.get("remarks") or []:
        if isinstance(remark, dict) and remark.get("description"):
            text = " ".join(str(line) for line in remark["description"])
            if text:
                summary["remarks"] = text
                break

    return summary


def pick_entity(entities: Sequence[Tuple[str, str]], *roles: str) -> Optional[str]:
    """First entity name whose role matches, in the order the roles are given."""
    for role in roles:
        for entity_role, name in entities:
            if entity_role == role:
                return name
    return None


# ------------------------------------------------------ mail authentication


def parse_spf(records: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Summarize an SPF record, or ``None`` when the domain has none."""
    record = next((r for r in records if r.strip().lower().startswith("v=spf1")), None)
    if record is None:
        return None

    parts = record.split()[1:]
    includes = [p.split(":", 1)[1] for p in parts if p.lower().startswith("include:")]
    mechanisms = {
        "include": len(includes),
        "a": sum(
            1
            for p in parts
            if p.lower().startswith(("a", "a:")) and not p.lower().startswith("all")
        ),
        "mx": sum(1 for p in parts if p.lower().startswith("mx")),
        "ptr": sum(1 for p in parts if p.lower().startswith("ptr")),
        "exists": sum(1 for p in parts if p.lower().startswith("exists:")),
    }
    # Every include/a/mx/exists costs one of the 10 allowed DNS lookups.
    lookups = (
        mechanisms["include"]
        + mechanisms["a"]
        + mechanisms["mx"]
        + mechanisms["ptr"]
        + mechanisms["exists"]
    )

    qualifier = next(
        (p for p in parts if p.lower().endswith(("-all", "~all", "+all", "?all"))), ""
    )
    qualifier = qualifier.lower().lstrip("+-~?") and qualifier.lower()[-4:]
    strength = {
        "-all": "strict (fail)",
        "~all": "soft (mark only)",
        "+all": "open - anyone may send as this domain",
        "?all": "neutral (no policy)",
    }.get(qualifier, "no all-mechanism")

    notes: List[str] = []
    if lookups > 10:
        notes.append(
            f"{lookups} DNS lookups - over the RFC limit of 10, receivers may permfail"
        )
    if qualifier == "+all":
        notes.append("the record authorises every sender (spoofing-friendly)")

    return {
        "record": record,
        "all": qualifier or "(none)",
        "strength": strength,
        "includes": includes,
        "lookups": lookups,
        "notes": notes,
    }


def parse_dmarc(records: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Summarize a DMARC record, or ``None`` when the domain has none."""
    record = next(
        (r for r in records if r.strip().lower().startswith("v=dmarc1")), None
    )
    if record is None:
        return None

    tags: Dict[str, str] = {}
    for part in record.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            tags[key.strip().lower()] = value.strip()

    policy = tags.get("p", "").lower()
    strength = {
        "reject": "strong (reject)",
        "quarantine": "medium (quarantine)",
        "none": "monitoring only (no enforcement)",
    }.get(policy, f"unset ({policy or 'no p tag'})")

    notes: List[str] = []
    percent = tags.get("pct", "100")
    if percent not in ("", "100"):
        notes.append(f"pct={percent} - only part of the mail is checked")
    if tags.get("sp") and tags["sp"].lower() != policy:
        notes.append(f"subdomains use sp={tags['sp']}")
    if not tags.get("rua"):
        notes.append("no rua= address, so no aggregate reports are requested")

    return {
        "record": record,
        "policy": policy or "(none)",
        "strength": strength,
        "pct": percent or "100",
        "rua": tags.get("rua", ""),
        "subdomains": tags.get("sp", ""),
        "notes": notes,
    }


# ------------------------------------------------------- HTTP security grade

_SECURITY_HEADERS: Tuple[Tuple[str, Tuple[str, ...], Optional[str]], ...] = (
    ("HSTS", ("strict-transport-security",), None),
    ("CSP", ("content-security-policy",), None),
    ("X-Frame-Options", ("x-frame-options",), "frame-ancestors"),
    ("X-Content-Type-Options", ("x-content-type-options",), None),
    ("Referrer-Policy", ("referrer-policy",), None),
    ("Permissions-Policy", ("permissions-policy", "feature-policy"), None),
)

_GRADES = {6: "A", 5: "B", 4: "C", 3: "D", 2: "E", 1: "F", 0: "F"}


def lower_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def grade_security_headers(
    headers: Mapping[str, Any],
) -> Tuple[str, List[Tuple[str, bool]]]:
    """Grade the response headers and say which ones are missing.

    ``X-Frame-Options`` also counts as present when a CSP carries
    ``frame-ancestors``, which is the modern equivalent.
    """
    lowered = lower_headers(headers)
    csp = lowered.get("content-security-policy", "")
    results: List[Tuple[str, bool]] = []
    for label, names, csp_alternative in _SECURITY_HEADERS:
        present = any(name in lowered for name in names)
        if not present and csp_alternative and csp_alternative in csp.lower():
            present = True
        results.append((label, present))
    score = sum(1 for _, present in results if present)
    return _GRADES.get(score, "F"), results


def exposed_fingerprint(headers: Mapping[str, Any]) -> List[str]:
    """Headers that name the software stack, with version numbers kept."""
    lowered = lower_headers(headers)
    found: List[str] = []
    for name in ("server", "x-powered-by", "x-aspnet-version", "x-generator", "via"):
        value = lowered.get(name)
        if value:
            found.append(f"{name}: {value}")
    if lowered.get("x-robots-tag"):
        found.append(f"x-robots-tag: {lowered['x-robots-tag']}")
    return found


# -------------------------------------------------------- file metadata


def hashes_of(data: bytes) -> Dict[str, str]:
    return {
        "md5": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def humanize_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sniff_image(data: bytes) -> Dict[str, Any]:
    """Identify an image container and its pixel dimensions."""
    info: Dict[str, Any] = {"kind": "", "mime": "", "width": 0, "height": 0}
    if data.startswith(b"\xff\xd8"):
        info.update(kind="JPEG", mime="image/jpeg")
        info.update(_jpeg_dimensions(data))
    elif data.startswith(_PNG_SIGNATURE):
        info.update(kind="PNG", mime="image/png")
        if len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            info.update(width=width, height=height)
        if b"acTL" in data[:4096]:
            info["animated"] = True
    elif data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        info.update(kind="GIF", mime="image/gif")
        if len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            info.update(width=width, height=height)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        info.update(kind="WebP", mime="image/webp")
        info.update(_webp_dimensions(data))
    elif data[:4] in (b"II*\x00", b"MM\x00*"):
        info.update(kind="TIFF", mime="image/tiff")
    elif data.startswith(b"%PDF"):
        info.update(kind="PDF", mime="application/pdf")
    return info


def _jpeg_dimensions(data: bytes) -> Dict[str, int]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:
            break
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return {"width": width, "height": height}
        index += 2 + length
    return {}


def _webp_dimensions(data: bytes) -> Dict[str, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return {"width": width, "height": height}
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return {"width": width, "height": height}
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return {"width": (bits & 0x3FFF) + 1, "height": ((bits >> 14) & 0x3FFF) + 1}
    return {}


# ------------------------------------------------------------- EXIF / TIFF

_TYPE_SIZES = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}

_IFD0_TAGS = {
    0x010E: "ImageDescription",
    0x010F: "Make",
    0x0110: "Model",
    0x0112: "Orientation",
    0x011A: "XResolution",
    0x011B: "YResolution",
    0x0128: "ResolutionUnit",
    0x0131: "Software",
    0x0132: "DateTime",
    0x013B: "Artist",
    0x013E: "WhitePoint",
    0x8298: "Copyright",
    0x8769: "ExifIFD",
    0x8825: "GPSIFD",
}

_EXIF_TAGS = {
    0x829A: "ExposureTime",
    0x829D: "FNumber",
    0x8822: "ExposureProgram",
    0x8827: "ISO",
    0x9000: "ExifVersion",
    0x9003: "DateTimeOriginal",
    0x9004: "DateTimeDigitized",
    0x9101: "ComponentsConfiguration",
    0x9201: "ShutterSpeedValue",
    0x9202: "ApertureValue",
    0x9204: "ExposureBiasValue",
    0x9207: "MeteringMode",
    0x9209: "Flash",
    0x920A: "FocalLength",
    0x927C: "MakerNote",
    0x9286: "UserComment",
    0xA002: "PixelXDimension",
    0xA003: "PixelYDimension",
    0xA430: "OwnerName",
    0xA431: "BodySerialNumber",
    0xA433: "LensMake",
    0xA434: "LensModel",
    0xA435: "LensSerialNumber",
}

_GPS_TAGS = {
    0x0000: "GPSVersionID",
    0x0001: "GPSLatitudeRef",
    0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef",
    0x0004: "GPSLongitude",
    0x0005: "GPSAltitudeRef",
    0x0006: "GPSAltitude",
    0x0007: "GPSTimeStamp",
    0x0008: "GPSSatellites",
    0x000D: "GPSSpeedRef",
    0x0010: "GPSImgDirectionRef",
    0x0011: "GPSImgDirection",
    0x001D: "GPSDateStamp",
}

#: Tags that make a photo attributable to a person or a device.
_PERSONAL_TAGS = (
    "Make",
    "Model",
    "Software",
    "Artist",
    "Copyright",
    "OwnerName",
    "BodySerialNumber",
    "LensModel",
    "LensSerialNumber",
    "DateTimeOriginal",
    "UserComment",
    "GPSLatitude",
)


def _decode_ascii(raw: bytes) -> str:
    text = raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
    return re.sub(r"[\r\n\t]+", " ", text)


def _format_rational(raw: bytes, little: bool) -> str:
    numerator, denominator = struct.unpack(("<" if little else ">") + "II", raw[:8])
    if denominator == 0:
        return f"{numerator}/0"
    if numerator % denominator == 0:
        return str(numerator // denominator)
    if numerator < denominator:
        return f"{numerator}/{denominator}"
    return f"{numerator / denominator:.2f}"


class _TiffReader:
    """Minimal TIFF/EXIF IFD reader (both byte orders, nested IFDs)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.little = data[:2] == b"II"
        self.tags: Dict[str, str] = {}
        self.unknown: Dict[str, int] = {}
        self.gps: Optional[Tuple[float, float]] = None
        self.found = False

    # -- primitives ----------------------------------------------------
    def _u16(self, offset: int) -> int:
        return struct.unpack_from(
            ("<" if self.little else ">") + "H", self.data, offset
        )[0]

    def _u32(self, offset: int) -> int:
        return struct.unpack_from(
            ("<" if self.little else ">") + "I", self.data, offset
        )[0]

    def _value(self, entry: int) -> Tuple[int, bytes]:
        type_id = self._u16(entry + 2)
        count = self._u32(entry + 4)
        size = _TYPE_SIZES.get(type_id, 1) * count
        if size > 4:
            offset = self._u32(entry + 8)
            raw = self.data[offset : offset + size]
        else:
            raw = self.data[entry + 8 : entry + 8 + size]
        return type_id, raw

    def _render(self, type_id: int, raw: bytes, count: int) -> str:
        if type_id in (2, 7):
            text = _decode_ascii(raw)
            if type_id == 7 and not text:
                return f"<{len(raw)} bytes>"
            return text
        if type_id in (5, 10):
            values = [
                _format_rational(raw[i : i + 8], self.little)
                for i in range(0, min(len(raw), 8 * 8), 8)
            ]
            return ", ".join(values) if values else "<empty>"
        if type_id in (3, 8):
            step = 2
            fmt = ("<" if self.little else ">") + "H" * (len(raw) // step)
            return ", ".join(
                str(v) for v in struct.unpack(fmt, raw[: (len(raw) // step) * step])
            )
        if type_id in (1, 6):
            return ", ".join(str(b) for b in raw[:16])
        if type_id in (4, 9):
            step = 4
            fmt = ("<" if self.little else ">") + "I" * (len(raw) // step)
            return ", ".join(
                str(v) for v in struct.unpack(fmt, raw[: (len(raw) // step) * step])
            )
        return f"<type {type_id}, {count} values>"

    # -- IFDs ----------------------------------------------------------
    def parse(self) -> None:
        if len(self.data) < 8 or self._u16(2) != 0x2A:
            return
        self.found = True
        self._ifd(self._u32(4), _IFD0_TAGS, depth=0)
        self.gps = self._gps_coordinates()

    def _ifd(self, offset: int, names: Mapping[int, str], depth: int) -> None:
        if depth > 3 or offset <= 0 or offset + 2 > len(self.data):
            return
        count = self._u16(offset)
        if count == 0 or count > 512:
            return
        for index in range(count):
            entry = offset + 2 + index * 12
            if entry + 12 > len(self.data):
                return
            tag = self._u16(entry)
            type_id, raw = self._value(entry)
            if not raw:
                continue
            values = self._value_count(entry)
            label = names.get(tag) or f"tag 0x{tag:04X}"
            if tag in (0x8769, 0x8825):
                child = self._u32(entry + 8)
                self._ifd(child, _EXIF_TAGS if tag == 0x8769 else _GPS_TAGS, depth + 1)
                continue
            if tag == 0x927C:  # MakerNote: present but not worth dumping
                self.tags["MakerNote"] = f"present ({len(raw)} bytes)"
                continue
            if label.startswith("tag 0x"):
                self.unknown[label] = values
                continue
            self.tags[label] = self._render(type_id, raw, values)

    def _value_count(self, entry: int) -> int:
        return self._u32(entry + 4)

    def _gps_coordinates(self) -> Optional[Tuple[float, float]]:
        lat, lon = self._gps_degrees("GPSLatitude"), self._gps_degrees("GPSLongitude")
        if lat is None or lon is None:
            return None
        if "S" in self.tags.get("GPSLatitudeRef", ""):
            lat = -lat
        if "W" in self.tags.get("GPSLongitudeRef", ""):
            lon = -lon
        if lat == 0.0 and lon == 0.0:
            return None
        return round(lat, 6), round(lon, 6)

    def _gps_degrees(self, tag: str) -> Optional[float]:
        raw_value = self.tags.get(tag)
        if not raw_value:
            return None
        parts: List[float] = []
        for piece in raw_value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if "/" in piece:
                numerator, _, denominator = piece.partition("/")
                try:
                    denominator_value = float(denominator)
                    parts.append(
                        float(numerator) / denominator_value
                        if denominator_value
                        else 0.0
                    )
                except ValueError:
                    return None
            else:
                try:
                    parts.append(float(piece))
                except ValueError:
                    return None
        if len(parts) < 3:
            return None
        degrees, minutes, seconds = parts[0], parts[1], parts[2]
        return degrees + minutes / 60 + seconds / 3600


def _tiff_from_jpeg(data: bytes) -> Optional[bytes]:
    """Locate the EXIF TIFF block inside a JPEG APP1 segment."""
    index = 2
    while index + 4 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:  # start of scan, metadata is done
            return None
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker == 0xE1:
            payload = data[index + 4 : index + 2 + length]
            if payload.startswith(b"Exif\x00\x00"):
                return payload[6:]
        index += 2 + length
    return None


def image_exif(data: bytes) -> Dict[str, Any]:
    """Read EXIF out of a JPEG, PNG (``eXIf``) or raw TIFF."""
    tiff: Optional[bytes] = None
    if data.startswith(b"\xff\xd8"):
        tiff = _tiff_from_jpeg(data)
    elif data.startswith(_PNG_SIGNATURE):
        tiff = _png_chunk(data, b"eXIf")
    elif data[:4] in (b"II*\x00", b"MM\x00*"):
        tiff = data

    if not tiff:
        return {"present": False, "tags": {}, "unknown": {}, "gps": None}

    reader = _TiffReader(tiff)
    reader.parse()
    if not reader.found:
        return {"present": False, "tags": {}, "unknown": {}, "gps": None}
    return {
        "present": bool(reader.tags or reader.unknown),
        "tags": reader.tags,
        "unknown": reader.unknown,
        "gps": reader.gps,
    }


def _png_chunk(data: bytes, chunk_type: bytes) -> Optional[bytes]:
    index = 8
    while index + 8 <= len(data):
        length = struct.unpack(">I", data[index : index + 4])[0]
        name = data[index + 4 : index + 8]
        payload = data[index + 8 : index + 8 + length]
        if name == chunk_type:
            return payload
        if name == b"IEND":
            return None
        index += 12 + length
    return None


def png_text_chunks(data: bytes) -> Dict[str, str]:
    """Read ``tEXt`` / ``zTXt`` / ``iTXt`` keywords out of a PNG."""
    if not data.startswith(_PNG_SIGNATURE):
        return {}
    found: Dict[str, str] = {}
    index = 8
    while index + 8 <= len(data) and len(found) < 24:
        length = struct.unpack(">I", data[index : index + 4])[0]
        name = data[index + 4 : index + 8]
        payload = data[index + 8 : index + 8 + length]
        if name == b"IEND":
            break
        if name == b"tEXt" and b"\x00" in payload:
            keyword, _, text = payload.partition(b"\x00")
            found[_decode_ascii(keyword)] = _decode_ascii(text)
        elif name == b"zTXt" and b"\x00" in payload:
            keyword, _, rest = payload.partition(b"\x00")
            try:
                text = zlib.decompress(rest[1:]).decode("utf-8", "replace")
            except zlib.error:
                text = "<compressed, could not be read>"
            found[_decode_ascii(keyword)] = _decode_ascii(text.encode())
        elif name == b"iTXt":
            parts = payload.split(b"\x00", 5)
            if len(parts) >= 6:
                found[_decode_ascii(parts[0])] = _decode_ascii(parts[5])
        index += 12 + length
    return found


_PDF_KEYS = (
    ("Author", "/Author"),
    ("Creator", "/Creator"),
    ("Producer", "/Producer"),
    ("Title", "/Title"),
    ("Subject", "/Subject"),
    ("Keywords", "/Keywords"),
    ("CreationDate", "/CreationDate"),
    ("ModDate", "/ModDate"),
)


def _pdf_string(data: bytes, marker: bytes) -> str:
    start = data.find(marker)
    if start < 0:
        return ""
    rest = data[start + len(marker) : start + len(marker) + 512].lstrip()
    if rest.startswith(b"<"):  # hex string
        end = rest.find(b">")
        if end < 0:
            return ""
        try:
            return (
                bytes.fromhex(rest[1:end].replace(b"\n", b"").decode())
                .decode("latin-1", "replace")
                .strip()
            )
        except ValueError:
            return ""
    if not rest.startswith(b"("):
        return ""
    depth = 0
    out = bytearray()
    for index, byte in enumerate(rest[1:], start=1):
        if byte == 0x5C and index + 1 < len(rest):  # backslash escape
            out.append(rest[index + 1])
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            if depth == 0:
                break
            depth -= 1
        elif byte == 0x5C:
            continue
        out.append(byte)
    return out.decode("latin-1", "replace").strip()


def pdf_metadata(data: bytes) -> Dict[str, str]:
    """Read the classic PDF info dictionary (Author/Producer/…) if it is there."""
    if not data.startswith(b"%PDF"):
        return {}
    head = data[: 512 * 1024]
    found: Dict[str, str] = {}
    for label, marker in _PDF_KEYS:
        value = _pdf_string(head, marker.encode())
        if value:
            if label.endswith("Date") and value.startswith("D:"):
                value = _format_pdf_date(value[2:])
            found[label] = value
    return found


def _format_pdf_date(raw: str) -> str:
    digits = re.sub(r"[^0-9]", "", raw)[:14].ljust(14, "0")
    try:
        return (
            f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]} "
            f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        )
    except IndexError:
        return raw


def file_metadata(data: bytes, filename: str = "") -> Dict[str, Any]:
    """One report for any attachment: type, size, hashes, embedded metadata."""
    report: Dict[str, Any] = {
        "filename": filename or "attachment",
        "size": len(data),
        "size_human": humanize_bytes(len(data)),
        "hashes": hashes_of(data),
        "image": sniff_image(data),
        "exif": {},
        "png_text": {},
        "pdf": {},
    }
    report["exif"] = image_exif(data)
    report["png_text"] = png_text_chunks(data)
    report["pdf"] = pdf_metadata(data)
    return report


def gps_links(latitude: float, longitude: float) -> Dict[str, str]:
    """Coordinate links that do not require an API key."""
    return {
        "osm": f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=17/{latitude}/{longitude}",
        "geo": f"geo:{latitude},{longitude}",
    }


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def plural(count: int, singular: str, plural_form: str = "") -> str:
    return singular if count == 1 else (plural_form or singular + "s")
