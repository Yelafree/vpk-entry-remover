# VPK format notes

Enough of the Valve Pak format to reason about an edit, plus the two facts that are
easy to get wrong. `scripts/remove_vpk_entry.py` implements all of this.

## Layout

A VPK file is a concatenation of five regions, in this order:

```
+---------------------------+
| header                    |  12 B (v1) or 28 B (v2)
+---------------------------+
| directory tree            |  variable, `treeSize` bytes
+---------------------------+
| file data section         |  `fileDataSectionSize` bytes, flat
+---------------------------+
| archive MD5 section       |  `archiveMD5SectionSize` bytes
+---------------------------+
| other MD5 section         |  `otherMD5SectionSize` bytes (48 in practice)
+---------------------------+
| signature section         |  `signatureSectionSize` bytes
+---------------------------+
```

### Header

```c
struct VPK_Header_t {
    uint32 signature;            // 0x55AA1234
    uint32 version;              // 1 or 2
    uint32 treeSize;
    uint32 fileDataSectionSize;  // v2 only
    uint32 archiveMD5SectionSize;// v2 only
    uint32 otherMD5SectionSize;  // v2 only
    uint32 signatureSectionSize; // v2 only
};
```

v1 is just the first three fields (12 bytes). v1 has no checksum sections at all.

### Directory tree

Recursive, null-terminated strings, terminated by empty strings:

```
extension \0
    directory \0
        filename \0
            uint32 crc32
            uint16 preloadBytes
            uint16 archiveIndex
            uint32 entryOffset
            uint32 entryLength
            uint16 0xFFFF          // terminator
            uint8  preloadBytes[...]  // only if preloadBytes > 0
        \0                          // end of this directory
    \0                              // end of this extension
\0                                  // end of tree
```

Root-level files use a **single space** `" "` as their directory. The full
in-archive path is `directory + "/" + filename + "." + extension` (or
`filename + "." + extension` at root).

### Addressing

`archiveIndex` selects where the payload lives:

* `0x7FFF` (32767) — **embedded**: the payload is in this file's data section, at
  `entryOffset` measured from the start of the data section. Mod paks are almost
  always this.
* `0..N` — **external**: the payload is in the sibling `name_NNN.vpk` shard, at
  `entryOffset` from that file's start. The base game's `pak01_dir.vpk` uses this
  exclusively, with `fileDataSectionSize == 0`.

Consequences that bite:

* Offsets are **relative**, so changing `treeSize` shifts the whole data region in
  the file without invalidating any offset. Removing tree entries does not require
  rewriting the payloads.
* Compacting the data section means renumbering the `entryOffset` of every surviving
  **embedded** entry, and only those. External entries' offsets belong to another
  file and must be left alone.
* A VPK can legitimately mix both kinds.

### CRC32

The stored `crc32` is the standard IEEE CRC-32 (poly `0xEDB88320`, init
`0xFFFFFFFF`, final XOR), i.e. exactly `zlib.crc32` and the ZIP CRC. It covers the
whole logical payload: `preloadBytes` first (if any), then the data-section bytes. An
entry with no payload has a stored CRC of `0`.

## The OtherMD5 section

48 bytes, three 16-byte MD5 digests. Let `S` be the offset of the section:

| Field | Offset | Value |
| :-- | :-- | :-- |
| tree checksum | `S + 0` | `MD5(tree bytes)` |
| archive checksum | `S + 16` | `MD5(archive MD5 section bytes)` — `MD5(b"")` when that section is absent |
| whole-file checksum | `S + 32` | `MD5(file[0 : S + 32])` |

**The third field is the one everyone gets wrong.** It is *not* the MD5 of the whole
file, and *not* the MD5 of the file with the section zeroed. It covers everything
before the field itself, so it **includes the two checksum fields at `S` and `S+16`**.
Hash the first `S + 32` bytes and nothing else.

Verified against three Valve-packed archives (`pak39_dir.vpk`, `pak40_dir.vpk` and
the base game's `pak01_dir.vpk`) by brute-forcing candidate formulas; only
`file[0 : S + 32]` reproduces the stored value in all of them.

Writing order therefore matters: emit header, tree, data section and archive MD5
section, then the tree checksum and archive checksum, and only then hash the bytes
written so far to produce the third field. An incremental hash over the output
stream gives this for free.

## Signature section (and its trailer quirk)

The signature section holds an RSA signature and is not recomputable without
Valve's private key. Editing any content invalidates it.

There is a trap here. In Deadlock's own `pak01_dir.vpk` the header declares
`signatureSectionSize == 20`, but **1082** bytes actually follow the OtherMD5
section: the signature is itself a complete nested VPK v1 (its own `0x55AA1234`
signature and header are plainly visible at that offset). So the declared size does
*not* reach EOF, and a strict `declared == fileSize` check wrongly rejects a
perfectly valid archive.

The safe handling: treat everything from `S + 48` to EOF as an opaque trailer,
copy it verbatim, and refuse only when the file is **shorter** than declared
(genuine truncation).

## Why not use a ZIP tool

A ZIP has a per-entry local header adjacent to its data, so a tool can rewrite
entries independently. A VPK keeps one flat data region addressed by a directory
tree, so deleting bytes in the middle invalidates every offset after them, and the
trailing checksum sections no longer describe the file. A ZIP-style edit can leave a
container where the tree parses but the payloads are garbage — which loads as silent
corruption rather than an error.

## Minimal correct removal

1. Parse header and tree.
2. Drop the tree entries to be removed.
3. Renumber embedded offsets over the survivors, in original data-region order, from
   0 (compaction) — or leave all offsets untouched (`--keep-holes`).
4. Rebuild the tree in the original extension/directory grouping order.
5. Rewrite the header with the new `treeSize` and `fileDataSectionSize`.
6. Copy embedded payloads, then the archive MD5 section, then recompute the OtherMD5
   section, then copy the trailer verbatim.
7. Re-read the output and check every CRC32 and all three MD5s.
