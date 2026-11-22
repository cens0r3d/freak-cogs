# DiskInfo

Free space on the machine that runs the bot — as an embed, and as a PNG bar card
if you want a picture for the channel.

```
!df
```

**3 filesystems · 13.0 GiB free of 50.0 GiB (74% in use)**

- `/` — `█████████░░░` **74%** used, 35.0 GiB used · 13.0 GiB free · 48.0 GiB total, ext4
- `/boot` — `██░░░░░░░░░░` **13%** used, 124 MiB used · 812 MiB free · 936 MiB total, ext4

Both views read the same numbers, so a screenshot never disagrees with the
message.

## What is filtered out

Read from `/proc/self/mountinfo` (falling back to `/proc/mounts`):

- pseudo filesystems — `tmpfs`, `devtmpfs`, `proc`, `sysfs`, `cgroup*`, `overlay`,
  `squashfs`, `efivarfs`, `ramfs` and friends;
- loop devices (`/dev/loop*`) and session FUSE mounts (`gvfsd`, `portal`, `lxcfs`);
- bind mounts of a device that is already listed — the shortest mountpoint wins;
- mounts below the size cut-off (default **1 GiB**), which drops EFI/boot partitions;
- mounts you hid with `!df hide`.

Usage matches `df`: `used / (used + available)` — reserved blocks are not counted
as free, because they are not free for anyone but root. Sizes are binary
(GiB/MiB), same as `df -h`.

## Commands (`[p]df`, alias `diskfree`)

| Command | Effect | Who |
| --- | --- | --- |
| `df` | the report; adds the chart when `df image` is on | everyone |
| `df chart` | the same report with the PNG card attached | everyone |
| `df hide <mountpoint>` | drop a mount from the report, e.g. `df hide /boot` | mod |
| `df show <mountpoint>` | bring it back | mod |
| `df hidden` | list the hidden mounts of this guild | everyone |
| `df minsize <GiB>` | size cut-off, `0` shows everything (default 1) | admin |
| `df image [true\|false]` | attach the chart to every report | admin |

Settings are per guild; the numbers are always the host's, not a container's view
of the caller.

The chart needs **Pillow** in the bot's venv (`[p]pipinstall pillow`) and the
**Attach Files** permission. Without either, `df` still answers with the embed
and `df chart` names the missing piece.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| a mount is missing | under `minsize`, in `df hidden`, or a pseudo/bind filesystem — check `df hidden` and `df minsize 0` |
| `Chart unavailable` | Pillow missing in the bot venv: `[p]pipinstall pillow` |
| picture never arrives | bot lacks **Attach Files** in that channel |
| numbers differ from `df -h` by a few MB | reserved blocks and the `used + available` denominator |
| a network mount is slow | the report stats every mount synchronously; hide it with `df hide` |
