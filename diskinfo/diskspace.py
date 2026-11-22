"""Filesystem facts for the diskinfo cog.

Standard library only and no discord/redbot imports, so this module can be
read and tested with a bare interpreter.
"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass

GIB = 1024**3

# Filesystems that never hold user data and would only pad the list.
PSEUDO_FILESYSTEMS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
    }
)

# fuse.<name> mounts that are session plumbing, not storage.
PSEUDO_FUSE = frozenset({"fuse.gvfsd-fuse", "fuse.portal", "fuse.lxcfs", "fuse.dumpfd"})

_OCTAL = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class Filesystem:
    mountpoint: str
    fstype: str
    source: str
    total: int
    used: int
    free: int
    inodes_total: int = 0
    inodes_used: int = 0

    @property
    def percent(self) -> float:
        """Used share of the space that is actually claimable (matches df)."""
        denominator = self.used + self.free
        if denominator <= 0:
            return 0.0
        return self.used / denominator * 100

    @property
    def inode_percent(self) -> float | None:
        if self.inodes_total <= 0:
            return None
        return self.inodes_used / self.inodes_total * 100


def _unescape(value: str) -> str:
    return _OCTAL.sub(lambda match: chr(int(match.group(1), 8)), value)


def _read_mount_table() -> list[tuple[str, str, str]]:
    """(mountpoint, fstype, source) triples, most specific table first."""
    entries: list[tuple[str, str, str]] = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                left, separator, right = line.partition(" - ")
                if not separator:
                    continue
                left_fields = left.split()
                right_fields = right.split()
                if len(left_fields) < 5 or len(right_fields) < 2:
                    continue
                entries.append(
                    (
                        _unescape(left_fields[4]),
                        _unescape(right_fields[0]),
                        _unescape(right_fields[1]),
                    )
                )
    except OSError:
        pass

    if entries:
        return entries

    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                entries.append(
                    (_unescape(fields[1]), _unescape(fields[2]), _unescape(fields[0]))
                )
    except OSError:
        pass

    if not entries:
        entries.append(("/", "unknown", "unknown"))
    return entries


def _is_pseudo(fstype: str, source: str) -> bool:
    if fstype in PSEUDO_FILESYSTEMS:
        return True
    if fstype in PSEUDO_FUSE:
        return True
    if fstype.startswith("cgroup"):
        return True
    if source.startswith("/dev/loop"):
        return True
    return False


def human_size(value: int) -> str:
    """Binary sizes with one decimal - 48.0 GiB, 124 MiB."""
    step = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if step < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(step)} B"
            return f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} PiB"


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown host"


def collect(min_total: int = 0, hidden: tuple[str, ...] = ()) -> list[Filesystem]:
    """Every real filesystem at or above ``min_total`` bytes, minus ``hidden``.

    Bind mounts of the same device are collapsed to their shortest mountpoint,
    read-only image mounts and pseudo filesystems are dropped.
    """
    hidden_paths = {path.rstrip("/") or "/" for path in hidden}
    seen_devices: set[int] = set()
    filesystems: list[Filesystem] = []

    for mountpoint, fstype, source in sorted(
        _read_mount_table(), key=lambda item: (item[0].count("/"), len(item[0]))
    ):
        if _is_pseudo(fstype, source):
            continue
        normalised = mountpoint.rstrip("/") or "/"
        if normalised in hidden_paths:
            continue
        try:
            stat = os.stat(mountpoint)
            stats = os.statvfs(mountpoint)
        except OSError:
            # Stale handle, ejected media or no permission to traverse it.
            continue
        if stat.st_dev in seen_devices:
            continue
        total = stats.f_blocks * stats.f_frsize
        if total < max(min_total, 1):
            continue

        used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
        free = stats.f_bavail * stats.f_frsize
        inodes_total = int(stats.f_files)
        inodes_used = int(stats.f_files - stats.f_ffree)

        seen_devices.add(stat.st_dev)
        filesystems.append(
            Filesystem(
                mountpoint=normalised,
                fstype=fstype,
                source=source,
                total=total,
                used=used,
                free=free,
                inodes_total=inodes_total,
                inodes_used=inodes_used,
            )
        )

    filesystems.sort(key=lambda fs: (fs.mountpoint != "/", fs.mountpoint))
    return filesystems
