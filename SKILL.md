---
name: vpk-entry-remover
description: "Remove a file from inside a Valve VPK (pakXX_dir.vpk, 包体) and repack the archive so it stays valid. Use when asked to delete / remove / strip / 去掉 / 删除 a file from a pak or VPK — most often the vnmskel_c skeleton override that Deadlock hero mods ship. Matches by extension or path pattern, never a hardcoded path, because the path differs per mod. Handles one archive or a whole folder; backs up and re-verifies every CRC32 and MD5."
metadata:
  short-description: "Remove a file from inside a VPK and repack it safely"
---

# VPK Entry Remover

Delete entries from a Valve Pak (VPK) archive and leave a byte-valid container behind.

The usual job: a Deadlock hero mod ships its own `*.vnmskel_c` (the animation
skeleton). That file overrides the game's own skeleton for every other mod, so it
has to come out of the pak. The **path varies per mod** — `models/heroes_wip/<hero>/<hero>.vnmskel_c`,
`animation/skeletons/<name>.vnmskel_c`, `models/animgraph2/.../*.vnmskel_c` — but the
thing to delete is always the `vnmskel_c`. So never hardcode a path; match the pattern.

## Run it

```bash
# macOS / Linux
python3 "<skill-dir>/scripts/remove_vpk_entry.py" <path> [options]
```

```powershell
# Windows
python "<skill-dir>\scripts\remove_vpk_entry.py" <path> [options]
```

`<skill-dir>` is the directory this SKILL.md lives in. Only the Python standard
library is used — no `pip install`, no build step. Python 3.8+.

## The default is already the common case

```bash
python "<skill-dir>/scripts/remove_vpk_entry.py" pak14_dir.vpk
```

With no other arguments this removes every entry whose path contains `vnmskel_c`
(case-insensitive), wherever it sits in the tree. That single command is the whole
task for a typical mod pak.

**Always dry-run first when the target is not a single known archive:**

```bash
python "<skill-dir>/scripts/remove_vpk_entry.py" "D:\mods\待删除" --dry-run
```

`--dry-run` prints exactly which entries would go and touches nothing.

## Batch a whole folder

```bash
python "<skill-dir>/scripts/remove_vpk_entry.py" "D:\DeadLockModding" --dry-run
python "<skill-dir>/scripts/remove_vpk_entry.py" "D:\DeadLockModding"
python "<skill-dir>/scripts/remove_vpk_entry.py" "D:\DeadLockModding" --recursive   # include subfolders
```

A directory argument expands to its `*.vpk` files. Each archive is handled
independently: one failure does not abort the rest.

## Options that matter

| Option | Use it when |
| :-- | :-- |
| `--dry-run`, `-n` | Always, before a batch or an unfamiliar archive. |
| `--match SUBSTR`, `-m` | Target something other than `vnmskel_c`. Repeatable; any match wins. |
| `--ext EXT`, `-e` | Match by *exact* extension instead of substring, e.g. `--ext vnmskel_c`. Repeatable. |
| `--out PATH`, `-o` | Write the cleaned archive elsewhere and leave the original untouched. Single input only. |
| `--keep-holes` | Leave the data region in place instead of compacting (see below). |
| `--no-backup` | Skip the automatic `.bak`. Only when the user already has a copy. |
| `--no-verify` | Skip the post-write verification pass. Rarely correct. |
| `--force` | Permit deleting *every* entry in an archive. Guarded on purpose. |

### Compaction vs `--keep-holes`

By default the embedded data region is **compacted**: the deleted payload is gone
for good and the surviving offsets are renumbered, so the archive shrinks by the
removed size.

`--keep-holes` leaves offsets alone, so the deleted bytes survive as unreferenced
padding. Use it when the payload is not something the user wants destroyed, or when
you want a change that cannot possibly move a single surviving byte. Both modes
produce archives that pass full verification; only compaction actually removes the
data.

## What it guarantees

Every run without `--no-verify` re-reads its own output and checks:

* the **CRC32 of every entry** against the value stored in the directory tree,
* all three fields of the **VPK v2 `OtherMD5` section** against recomputed hashes.

A clean run ends with `verify: OK (all CRC32 + all MD5 checksums match)`. If you do
not see that line, treat the result as broken: restore from the `.bak` and report it.

The VPK format details, including the non-obvious third-checksum formula this tool
reproduces, are in `references/vpk-format.md`.

## Exit codes

| Code | Meaning |
| :-- | :-- |
| 0 | At least one archive was modified (or, under `--dry-run`, matched something). |
| 1 | Hard failure on an input (malformed, truncated, unreadable, I/O error). |
| 2 | Usage error (bad arguments, no inputs found). |
| 3 | **Nothing matched anywhere** — the file you were told to delete is not in these archives. |
| 4 | The output failed its own verification. Restore the `.bak` and report. |

Code 3 is information, not a bug: it means the archive is already clean, or the
`--match` value is wrong. Do not retry it unchanged.

## Safety rules

* **Never** try to do this with 7-Zip, WinRAR, `zip`, or a hex editor. A VPK is a
  header plus a flat data region addressed by a directory tree; deleting bytes from
  the middle silently invalidates every later offset. The archive will look fine to
  a ZIP tool and fail to load in the game.
* Backups are automatic and cheap: an in-place run creates a **hardlink** `.bak`
  next to the archive (instant, no extra disk). If the filesystem lacks hardlinks it
  falls back to a copy. Pass `--no-backup` only if the user already has a copy.
* A `.bak` is written per archive. If `pak.vpk.bak` already exists the tool picks
  `pak.vpk.bak2`, `.bak3`, … so an earlier backup is never clobbered.
* Don't rewrite the base game's own `pak01_*.vpk` without an explicit request. Those
  are signed; the tool preserves the signature trailer verbatim and says so, but the
  signature no longer covers the edited content.

## Check your work with a second opinion

If `Source2Viewer-CLI.exe` is available, it is an independent VRF-based reader and
makes a good cross-check:

```bash
Source2Viewer-CLI.exe -i <archive> --vpk_verify   # must print "Success."
Source2Viewer-CLI.exe -i <archive> -l             # list entries; target must be absent
```

## After removing a skeleton

Removing a mod's `vnmskel_c` does not remove the *reference* to it. A model's
`vmdl_c` still names `models/heroes_wip/<hero>/<hero>.vnmskel`. That is normally
fine and is usually the whole point: the base game ships its own copy of that
skeleton, and with the mod's override gone the engine falls back to the game's
version. Do confirm the intent with the user, and say plainly that the result needs
an in-game test — if the mod's mesh was bound to its own skeleton, the model can
come out deformed once the game's skeleton is used instead.

To check whether the base game supplies a fallback, search its own archive:

```powershell
# reuse the same parser; --dry-run never writes
python "<skill-dir>\scripts\remove_vpk_entry.py" "<game>\game\citadel\pak01_dir.vpk" --dry-run
```

## Troubleshooting

| Symptom | Cause and fix |
| :-- | :-- |
| `no entry matches [...] - nothing to do` | Genuinely not present, or wrong `--match`. Re-run with `--dry-run` and a shorter substring. |
| `not a VPK: signature ...` | Wrong file, or a `_NNN.vpk` data shard rather than the `_dir.vpk` directory. Point at the `_dir.vpk`. |
| `unsupported VPK version N` | Only v1 and v2 exist in practice. Report the version; do not guess. |
| `file is truncated` | The archive is damaged or partially copied. Do not proceed; restore from a good copy. |
| `would delete ALL N entries` | The `--match` pattern is too broad. Narrow it. `--force` exists but is almost never right. |
| Verification fails | Restore the `.bak` and report the exact failing entry path — do not ship the output. |
| `python: command not found` | Python 3.8+ is required. Tell the user; do not substitute a hand-rolled editor. |
