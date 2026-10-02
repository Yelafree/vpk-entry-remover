# VPK Entry Remover

**Remove a file from inside a Valve VPK archive — and leave a container that still loads.**

An [Agent Skill](https://developers.openai.com/plugins/concepts/skills) that deletes entries from
Valve Pak (`.vpk`) archives and repacks them so that every checksum still validates. Works as a
skill for AI coding agents (OpenAI Codex, DeepSeek Harness, Claude Code, …) and just as well as a
plain command-line tool, with no agent involved.

```
VPK v2 | 107 entries | tree 5934 B | data 245894259 B | file 245900269 B
  removing 1 entry(ies):
    - models/heroes_wip/unicorn/unicorn.vnmskel_c  [12927 B, embedded, crc=0x3765378A]
  mode: compact (embedded data 245894259 -> 245881332 B, reclaims 12927 B)
  tree 5934 -> 5870 B | entries 107 -> 106
  backup: pak14_dir.vpk.bak (hardlink)
  wrote: pak14_dir.vpk (245887278 B)
  verify: OK (all CRC32 + all MD5 checksums match)
```

- Python 3.8+ · standard library only · no `pip install` · no build step
- Windows, Linux, macOS
- VPK v1 and v2, embedded and externally-archived entries
- Backs up, repacks, then re-verifies every CRC32 and every MD5

---

## The problem this solves

Deadlock hero mods ship their own `*.vnmskel_c` — the animation skeleton. That file **overrides the
game's own skeleton for every other mod**, so it has to come out of the pak before shipping. Three
things make that harder than "delete a file".

### 1. A VPK is not a ZIP

A ZIP stores a local header next to each entry, so entries can be rewritten independently. A VPK is
a header, a **directory tree**, one flat data region that every entry addresses by
`(archiveIndex, offset, length)`, and then up to three trailer sections:

```
header | directory tree | file data section | archive MD5 | other MD5 | signature
```

Deleting bytes from the middle silently invalidates every later offset, and the trailer checksums no
longer describe the file. A ZIP-style edit leaves a container whose tree parses but whose payloads
are garbage — which surfaces as silent in-game corruption, not as an error. Removing an entry
therefore means rebuilding the tree **and** repairing the trailers.

### 2. The path is different in every mod

The thing to delete is always `vnmskel_c`, but its location is not. Real examples:

| Archive | Entry path |
| :-- | :-- |
| `pak14_dir.vpk` | `models/heroes_wip/unicorn/unicorn.vnmskel_c` |
| `haze_nude.vpk` | `models/heroes_staging/haze/haze.vnmskel_c` |
| `minafullnude.vpk` | `models/heroes_wip/vampirebat/vampirebat.vnmskel_c` |

So this tool matches by **extension or path pattern**, never by a hardcoded path.

### 3. The third checksum is not what it looks like

The VPK v2 `OtherMD5` section holds three 16-byte digests. The first two are obvious; the third is
not. It is **not** the MD5 of the whole file, and **not** the MD5 of the file with the section zeroed
out. It is the MD5 of everything *before the field itself*, which means it **includes the two
preceding checksum fields**:

```
field 0 (offset  0) = MD5(tree bytes)
field 1 (offset 16) = MD5(archive MD5 section bytes)
field 2 (offset 32) = MD5(file[0 : other_md5_start + 32])
```

This was reverse-engineered by brute-forcing candidate formulas against three Valve-packed archives
and confirmed byte-for-byte on all of them. A repacker that guesses wrong produces archives that
strict readers reject — see [`references/vpk-format.md`](references/vpk-format.md) for the full
format notes.

---

## Features

- **Pattern matching, not hardcoded paths** — finds the target wherever it lives in the tree.
- **Batch mode** — point it at a folder of mod paks; each is handled independently.
- **Dry run** — shows exactly what would be removed and touches nothing.
- **Automatic backup** — an instant hardlink `.bak` (falls back to a copy where unsupported).
- **Self-verifying** — re-reads its own output and checks every CRC32 and all three MD5s.
- **Atomic install** — writes to a temp file and swaps it in; cleans up after a failure.
- **Two repack modes** — compact the data region, or leave offsets untouched with `--keep-holes`.
- **Handles the awkward cases** — externally-archived entries, mixed archives, and the oversized
  signature trailer that Deadlock's own `pak01_dir.vpk` carries.

---

## Installation

The skill is a single folder. Installing it means copying that folder into your agent's skills root.

### OpenAI Codex

```powershell
git clone https://github.com/Yelafree/vpk-entry-remover "$env:USERPROFILE\.codex\skills\vpk-entry-remover"
```

Or copy an existing checkout:

```powershell
robocopy . "$env:USERPROFILE\.codex\skills\vpk-entry-remover" /MIR
```

Skills are picked up from `$CODEX_HOME/skills/<skill-name>/` (default `~/.codex/skills`). Invoke it
explicitly with `$vpk-entry-remover`, or just describe the task — implicit invocation is enabled via
[`agents/openai.yaml`](agents/openai.yaml). It becomes available on the next session.

### DeepSeek Harness

```powershell
git clone https://github.com/Yelafree/vpk-entry-remover "$env:USERPROFILE\.dsh\skills\vpk-entry-remover"
```

Discovered automatically from `~/.dsh/skills/<skill-name>/SKILL.md`.

### Claude Code

```bash
git clone https://github.com/Yelafree/vpk-entry-remover ~/.claude/skills/vpk-entry-remover
```

### Any other agent

Copy the folder into whatever skills directory that agent reads. The bundle uses the common
`SKILL.md` + `scripts/` + `references/` layout, so no changes are needed. Project-local installs work
too — drop it in `<project>/.codex/skills/` or `<project>/.dsh/skills/`.

### No agent at all

The script is an ordinary CLI:

```bash
git clone https://github.com/Yelafree/vpk-entry-remover
python vpk-entry-remover/scripts/remove_vpk_entry.py pak14_dir.vpk
```

---

## Usage

### Quick start

```bash
# The default target is the vnmskel_c skeleton override, wherever it lives.
python scripts/remove_vpk_entry.py pak14_dir.vpk
```

With no other arguments this removes every entry whose path contains `vnmskel_c`
(case-insensitive). That single command is the whole task for a typical mod pak.

### Always dry-run first

```bash
python scripts/remove_vpk_entry.py "D:\mods\待删除" --dry-run
```

```text
=== D:\mods\待删除\haze_nude.vpk
  VPK v2 | 66 entries | tree 3752 B | data 69790347 B | file 69794175 B
  would remove 1 entry(ies):
    - models/heroes_staging/haze/haze.vnmskel_c  [9839 B, embedded, crc=0xE26408A9]
  (dry run - file untouched)
```

### Batch a folder

```bash
python scripts/remove_vpk_entry.py "D:\DeadLockModding" --dry-run
python scripts/remove_vpk_entry.py "D:\DeadLockModding"
python scripts/remove_vpk_entry.py "D:\DeadLockModding" --recursive
```

A directory argument expands to its `*.vpk` files. One failure does not abort the rest.

### Options

| Option | Description |
| :-- | :-- |
| `--dry-run`, `-n` | Report what would be removed without writing anything. |
| `--match SUBSTR`, `-m` | Case-insensitive path substring to remove (default `vnmskel_c`). Repeatable; any match wins. |
| `--ext EXT`, `-e` | Match by *exact* extension instead, e.g. `--ext vnmskel_c`. Repeatable; combines with `--match`. |
| `--out PATH`, `-o` | Write the cleaned archive elsewhere; leave the original untouched. Single input only. |
| `--keep-holes` | Do not compact the data region (see below). |
| `--no-backup` | Skip the automatic `.bak`. |
| `--no-verify` | Skip the post-write verification pass. |
| `--recursive`, `-r` | Recurse into subdirectories for directory inputs. |
| `--force` | Permit deleting *every* entry in an archive. Guarded on purpose. |

### Compaction vs `--keep-holes`

By default the embedded data region is **compacted**: the deleted payload is gone for good, surviving
offsets are renumbered, and the archive shrinks by the removed size.

`--keep-holes` leaves all offsets alone, so the deleted bytes survive as unreferenced padding. Use it
to make a change that cannot possibly move a single surviving byte. Both modes pass full
verification; only compaction actually removes the data.

### Exit codes

| Code | Meaning |
| :-- | :-- |
| `0` | At least one archive was modified (or, under `--dry-run`, matched something). |
| `1` | Hard failure on an input (malformed, truncated, unreadable, I/O error). |
| `2` | Usage error (bad arguments, no inputs found). |
| `3` | **Nothing matched anywhere** — the archive is already clean, or `--match` is wrong. |
| `4` | The output failed its own verification. Restore the `.bak` and report. |

---

## Verification

Unless `--no-verify` is passed, every run re-reads its own output and checks:

- the **CRC32 of every entry** against the value stored in the directory tree,
- all three fields of the **VPK v2 `OtherMD5` section** against recomputed hashes.

A clean run ends with `verify: OK (all CRC32 + all MD5 checksums match)`. If you do not see that line,
treat the result as broken.

For an independent second opinion, [ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat)
via `Source2Viewer-CLI` is a completely separate implementation:

```bash
Source2Viewer-CLI.exe -i <archive> --vpk_verify   # must print "Success."
Source2Viewer-CLI.exe -i <archive> -l             # list entries; target must be absent
```

This project was validated against it, and the Python implementation was checked to produce output
**byte-identical** to an independently written C# implementation of the same operation.

---

## Compatibility and known quirks

| Case | Behaviour |
| :-- | :-- |
| VPK v1 | Fully supported; v1 has no checksum sections to rebuild. |
| VPK v2, embedded data (`archiveIndex == 0x7FFF`) | The normal case for mod paks. Compaction applies. |
| VPK v2, external archives (`archiveIndex == 0..N`) | Supported. External entries keep their own offsets; only embedded entries are renumbered. |
| Mixed embedded + external | Supported correctly. |
| Signed archives | The signature trailer is preserved verbatim, and the tool warns that the signature no longer covers the modified content. |

**A trap worth knowing about.** Deadlock's own `pak01_dir.vpk` declares
`signatureSectionSize == 20`, but **1082** bytes actually follow the `OtherMD5` section: the
signature is itself a complete nested VPK v1 (its `0x55AA1234` header is plainly visible at that
offset). A strict "declared size must equal file size" check therefore rejects a perfectly valid
archive. This tool treats everything after the `OtherMD5` section as an opaque trailer, copies it
whole, and refuses only genuine truncation.

---

## After removing a skeleton

Removing a mod's `vnmskel_c` does not remove the *reference* to it — the model's `vmdl_c` still names
`models/heroes_wip/<hero>/<hero>.vnmskel`. That is normally the point: the base game ships its own
copy of that skeleton, and with the mod's override gone the engine falls back to the game's version.

Confirm the intent, and **test in game**. If the mod's mesh was bound to its own skeleton, the model
can come out deformed once the game's skeleton is used instead.

To check whether the base game supplies a fallback:

```bash
# --dry-run never writes; this only reads the game's directory
python scripts/remove_vpk_entry.py "<game>/game/citadel/pak01_dir.vpk" --dry-run
```

---

## Requirements

- Python 3.8 or newer. Nothing else — no third-party packages, no compiler, no build step.
- Cross-checking with `Source2Viewer-CLI` is optional.

---

## Repository layout

```
vpk-entry-remover/
├── SKILL.md                     # agent-facing instructions
├── agents/
│   └── openai.yaml              # Codex UI metadata and invocation policy
├── references/
│   └── vpk-format.md            # VPK format notes + the checksum formula
└── scripts/
    └── remove_vpk_entry.py      # the tool (stdlib only)
```

The repository root *is* the skill bundle, so `git clone` straight into a skills directory installs
it.

---

## Safety

- Never edit a VPK with 7-Zip, WinRAR, `zip`, or a hex editor. See
  [A VPK is not a ZIP](#1-a-vpk-is-not-a-zip).
- Backups are automatic: an in-place run creates a hardlink `.bak` next to the archive. If
  `pak.vpk.bak` exists, the tool picks `.bak2`, `.bak3`, … so an earlier backup is never clobbered.
- Don't rewrite the base game's own `pak01_*.vpk` without an explicit reason — those are signed.

---

## License

[MIT](LICENSE)
