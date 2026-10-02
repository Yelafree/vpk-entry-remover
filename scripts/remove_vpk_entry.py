#!/usr/bin/env python3
"""
remove_vpk_entry.py -- remove entries from Valve Pak (VPK) archives, safely.

Why a dedicated tool instead of a ZIP-style editor: a VPK is not a ZIP. It is a
header, a directory *tree*, one flat data region that every entry addresses by
(archive_index, offset, length), then up to three trailer sections (archive MD5,
"other" MD5, signature). Deleting an entry therefore means rebuilding the tree
AND repairing those trailers, or a strict reader rejects the whole archive.

What this does for every input:
  1. parse the header + directory tree (VPK v1 and v2),
  2. select entries to delete by extension or path substring,
  3. rebuild the tree without them,
  4. compact the embedded data region and renumber the surviving offsets,
  5. recompute the VPK v2 OtherMD5 section with the same formula the official
     Valve packer uses,
  6. write to a temp file, then atomically replace the target (with a backup),
  7. re-read the result and verify every CRC32 and every MD5.

Standard library only. No third-party packages, no build step.

The OtherMD5 formula, for the record (reverse-engineered from Valve-packed
archives and verified against them):

    field 0 (offset 0)  = MD5(tree bytes)
    field 1 (offset 16) = MD5(archive MD5 section bytes)   # MD5(b'') if absent
    field 2 (offset 32) = MD5(file[0 : other_md5_start + 32])

Field 2 is the interesting one: it is a hash of everything *before the field
itself*, i.e. it includes the two preceding checksum fields. Hashing the whole
file, or the file with the section zeroed, does NOT reproduce it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
import zlib
from pathlib import Path

VPK_SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE_INDEX = 0x7FFF
COPY_CHUNK = 8 << 20
DEFAULT_MATCH = "vnmskel_c"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_MATCH = 3
EXIT_VERIFY_FAILED = 4

# Windows consoles often default to a legacy code page; mod paths are frequently
# non-ASCII (e.g. "D:\\DeadLockModding\\待删除"). Never let printing crash the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


class VpkError(Exception):
    """Raised for anything that means 'do not touch this archive'."""


class Header:
    __slots__ = (
        "version",
        "tree_size",
        "file_data_size",
        "archive_md5_size",
        "other_md5_size",
        "signature_size",
        "header_size",
        "file_size",
    )

    def __init__(self, version, tree_size, file_data_size, archive_md5_size,
                 other_md5_size, signature_size, header_size, file_size):
        self.version = version
        self.tree_size = tree_size
        self.file_data_size = file_data_size
        self.archive_md5_size = archive_md5_size
        self.other_md5_size = other_md5_size
        self.signature_size = signature_size
        self.header_size = header_size
        self.file_size = file_size

    @property
    def data_start(self) -> int:
        return self.header_size + self.tree_size

    @property
    def archive_md5_start(self) -> int:
        return self.data_start + self.file_data_size

    @property
    def other_md5_start(self) -> int:
        return self.archive_md5_start + self.archive_md5_size

    @property
    def signature_start(self) -> int:
        return self.other_md5_start + self.other_md5_size

    @property
    def declared_size(self) -> int:
        return self.signature_start + self.signature_size

    @property
    def trailer_size(self) -> int:
        """Bytes from the signature section to EOF.

        Some Valve-packed archives (e.g. Deadlock's own pak01_dir.vpk) carry a
        nested signature VPK there that is larger than the header's declared
        signature size, so the trailer is treated as opaque and copied whole.
        """
        return self.file_size - self.signature_start

    def serialize(self, tree_size: int, file_data_size: int) -> bytes:
        if self.version >= 2:
            return struct.pack(
                "<IIIIIII",
                VPK_SIGNATURE,
                self.version,
                tree_size,
                file_data_size,
                self.archive_md5_size,
                self.other_md5_size,
                self.signature_size,
            )
        return struct.pack("<III", VPK_SIGNATURE, self.version, tree_size)


class Entry:
    __slots__ = (
        "ext", "dir", "name", "crc", "preload_bytes", "archive_index",
        "offset", "length", "preload_data", "out_offset",
    )

    def __init__(self, ext, dir_, name, crc, preload_bytes, archive_index,
                 offset, length, preload_data):
        self.ext = ext
        self.dir = dir_
        self.name = name
        self.crc = crc
        self.preload_bytes = preload_bytes
        self.archive_index = archive_index
        self.offset = offset
        self.length = length
        self.preload_data = preload_data
        self.out_offset = offset

    @property
    def path(self) -> str:
        # Root-level entries use a single space as their directory in VPK.
        return f"{self.name}.{self.ext}" if self.dir == " " else f"{self.dir}/{self.name}.{self.ext}"

    @property
    def embedded(self) -> bool:
        return self.archive_index == EMBEDDED_ARCHIVE_INDEX

    def __repr__(self) -> str:
        return f"<Entry {self.path!r} offset={self.offset} length={self.length}>"


def read_header(f) -> Header:
    f.seek(0, os.SEEK_END)
    file_size = f.tell()
    f.seek(0)

    raw = f.read(8)
    if len(raw) != 8:
        raise VpkError("file is too small to be a VPK")
    signature, version = struct.unpack("<II", raw)
    if signature != VPK_SIGNATURE:
        raise VpkError(f"not a VPK: signature 0x{signature:08X}, expected 0x{VPK_SIGNATURE:08X}")
    if version not in (1, 2):
        raise VpkError(f"unsupported VPK version {version} (only v1 and v2 are supported)")

    raw = f.read(4)
    if len(raw) != 4:
        raise VpkError("truncated header")
    (tree_size,) = struct.unpack("<I", raw)

    if version >= 2:
        raw = f.read(16)
        if len(raw) != 16:
            raise VpkError("truncated header")
        file_data_size, archive_md5_size, other_md5_size, signature_size = struct.unpack("<IIII", raw)
        header_size = 28
    else:
        # v1 has no trailer sections; the data region runs to EOF.
        archive_md5_size = other_md5_size = signature_size = 0
        header_size = 12
        file_data_size = file_size - header_size - tree_size

    if tree_size <= 0 or tree_size > file_size:
        raise VpkError(f"implausible tree size {tree_size}")

    header = Header(version, tree_size, file_data_size, archive_md5_size,
                    other_md5_size, signature_size, header_size, file_size)

    if version >= 2 and header.declared_size > file_size:
        raise VpkError(
            f"file is truncated: header declares {header.declared_size} bytes but the file is "
            f"{file_size} - refusing to touch a malformed archive"
        )
    if header.data_start > file_size:
        raise VpkError("data region starts past end of file")

    return header


def _read_cstr(buf: bytes, pos: int):
    end = buf.index(0, pos)
    return buf[pos:end].decode("utf-8", "surrogateescape"), end + 1


def parse_tree(tree: bytes) -> list:
    entries = []
    pos = 0
    size = len(tree)

    while pos < size:
        ext, pos = _read_cstr(tree, pos)
        if not ext:
            break
        while True:
            dir_, pos = _read_cstr(tree, pos)
            if not dir_:
                break
            while True:
                name, pos = _read_cstr(tree, pos)
                if not name:
                    break
                if pos + 18 > size:
                    raise VpkError("truncated directory entry")
                crc, preload, archive_index, offset, length, terminator = struct.unpack_from(
                    "<IHHIIH", tree, pos
                )
                pos += 18
                if terminator != 0xFFFF:
                    raise VpkError(f"bad entry terminator 0x{terminator:04X}")
                if preload:
                    if pos + preload > size:
                        raise VpkError("truncated preload data")
                    preload_data = tree[pos:pos + preload]
                    pos += preload
                else:
                    preload_data = b""
                entries.append(Entry(ext, dir_, name, crc, preload, archive_index,
                                     offset, length, preload_data))
    return entries


def build_tree(entries) -> bytes:
    """Rebuild the tree, preserving the original extension/directory grouping order."""
    by_ext = {}
    for e in entries:
        by_ext.setdefault(e.ext, {}).setdefault(e.dir, []).append(e)

    out = bytearray()

    def wcs(s: str) -> None:
        out.extend(s.encode("utf-8", "surrogateescape"))
        out.append(0)

    for ext, dirs in by_ext.items():
        wcs(ext)
        for dir_, group in dirs.items():
            wcs(dir_)
            for e in group:
                wcs(e.name)
                out.extend(struct.pack("<IHHIIH", e.crc, e.preload_bytes,
                                       e.archive_index, e.out_offset, e.length, 0xFFFF))
                if e.preload_bytes:
                    out.extend(e.preload_data)
            out.append(0)
        out.append(0)
    out.append(0)
    return bytes(out)


def select_removals(entries, matches, exts):
    """Return entries matching any path substring or exact extension."""
    needles = [m.lower() for m in matches]
    wanted_exts = {e.lower().lstrip(".") for e in exts}
    out = []
    for e in entries:
        path = e.path
        if needles and any(n in path.lower() for n in needles):
            out.append(e)
            continue
        if wanted_exts and e.ext.lower() in wanted_exts:
            out.append(e)
    return out


def unique_backup_path(path: Path) -> Path:
    candidate = path.with_name(path.name + ".bak")
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = path.with_name(f"{path.name}.bak{n}")
        if not candidate.exists():
            return candidate
        n += 1


def collect_inputs(paths, recursive: bool):
    files = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            pattern = "**/*.vpk" if recursive else "*.vpk"
            found = sorted(p.glob(pattern))
            if not found:
                print(f"  ! no .vpk files found in directory {p}")
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            print(f"  ! not found: {p}")
    # de-duplicate while keeping order
    seen = set()
    out = []
    for f in files:
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def process(path: Path, args) -> int:
    print(f"=== {path}")
    if not path.is_file():
        print(f"  ! not a file: {path}")
        return EXIT_ERROR

    # ---- phase 1: read the directory (handle closed before we touch anything) -----
    with open(path, "rb") as fin:
        header = read_header(fin)
        tree_bytes = fin.read(header.tree_size)
        if len(tree_bytes) != header.tree_size:
            raise VpkError("truncated directory tree")
        entries = parse_tree(tree_bytes)

    print(f"  VPK v{header.version} | {len(entries)} entries | tree {header.tree_size} B "
          f"| data {header.file_data_size} B | file {header.file_size} B")

    removals = select_removals(entries, args.match, args.ext)
    if not removals:
        print(f"  = no entry matches {args.match or args.ext} - nothing to do")
        return EXIT_NO_MATCH

    if args.dry_run:
        print(f"  would remove {len(removals)} entry(ies):")
        for e in removals:
            where = "embedded" if e.embedded else f"external archive {e.archive_index}"
            print(f"    - {e.path}  [{e.length} B, {where}, crc=0x{e.crc:08X}]")
        print("  (dry run - file untouched)")
        return EXIT_OK

    removal_ids = {id(e) for e in removals}
    keep = [e for e in entries if id(e) not in removal_ids]

    if not keep and not args.force:
        raise VpkError(
            f"this would delete ALL {len(entries)} entries; pass --force if that is really intended"
        )

    print(f"  removing {len(removals)} entry(ies):")
    for e in removals:
        where = "embedded" if e.embedded else f"external archive {e.archive_index}"
        print(f"    - {e.path}  [{e.length} B, {where}, crc=0x{e.crc:08X}]")

    # ---- phase 2: decide the new layout -------------------------------------------
    if args.keep_holes:
        # Do not move anything: surviving entries keep their original offsets and the
        # removed payload stays as unreferenced padding. Always valid, and the only
        # sane choice when the data region belongs to external archive files.
        new_data_size = header.file_data_size
        print("  mode: keep-holes (offsets unchanged, data region copied verbatim)")
    else:
        embedded = sorted((e for e in keep if e.embedded), key=lambda e: e.offset)
        cursor = 0
        for e in embedded:
            e.out_offset = cursor
            cursor += e.length
        new_data_size = cursor
        print(f"  mode: compact (embedded data {header.file_data_size} -> {new_data_size} B, "
              f"reclaims {header.file_data_size - new_data_size} B)")
        # Entries stored in external *_NNN.vpk files must keep their own offsets.
        external = [e for e in keep if not e.embedded]
        if external:
            print(f"  note: {len(external)} entr(ies) live in external archive files; "
                  "their offsets are untouched")

    new_tree = build_tree(keep)
    print(f"  tree {header.tree_size} -> {len(new_tree)} B | entries {len(entries)} -> {len(keep)}")

    new_header = header.serialize(len(new_tree), new_data_size)

    in_place = args.out is None
    target = path if in_place else Path(args.out)
    tmp = target.with_name(target.name + ".tmp")

    # ---- phase 3: stream the new archive ------------------------------------------
    try:
        with open(path, "rb") as fin, open(tmp, "wb") as fout:
            md5 = hashlib.md5() if header.version >= 2 else None

            def emit(data: bytes) -> None:
                fout.write(data)
                if md5 is not None:
                    md5.update(data)

            emit(new_header)
            emit(new_tree)

            written_data = 0
            if args.keep_holes:
                fin.seek(header.data_start)
                remaining = header.file_data_size
                while remaining > 0:
                    chunk = fin.read(min(COPY_CHUNK, remaining))
                    if not chunk:
                        raise VpkError("short read while copying the data region")
                    emit(chunk)
                    remaining -= len(chunk)
                written_data = header.file_data_size
            else:
                for e in sorted((x for x in keep if x.embedded), key=lambda x: x.out_offset):
                    if e.length == 0:
                        continue
                    fin.seek(header.data_start + e.offset)
                    remaining = e.length
                    while remaining > 0:
                        chunk = fin.read(min(COPY_CHUNK, remaining))
                        if not chunk:
                            raise VpkError(f"short read for {e.path}")
                        emit(chunk)
                        remaining -= len(chunk)
                        written_data += len(chunk)
                if written_data != new_data_size:
                    raise VpkError(f"wrote {written_data} data bytes, expected {new_data_size}")

            # Archive MD5 section: refers to external archives, none of which we
            # touched, so preserve it byte for byte.
            archive_md5_bytes = b""
            if header.archive_md5_size:
                fin.seek(header.archive_md5_start)
                archive_md5_bytes = fin.read(header.archive_md5_size)
                if len(archive_md5_bytes) != header.archive_md5_size:
                    raise VpkError("truncated archive MD5 section")
                emit(archive_md5_bytes)

            # Other MD5 section.
            if header.other_md5_size == 48:
                tree_hash = hashlib.md5(new_tree).digest()
                archive_hash = hashlib.md5(archive_md5_bytes).digest()
                emit(tree_hash)
                emit(archive_hash)
                whole_hash = md5.digest()  # covers everything emitted so far, this field excluded
                fout.write(whole_hash)
                print(f"  md5: tree={tree_hash.hex()} archive={archive_hash.hex()} "
                      f"whole={whole_hash.hex()}")
            elif header.other_md5_size:
                fin.seek(header.other_md5_start)
                blob = fin.read(header.other_md5_size)
                if len(blob) != header.other_md5_size:
                    raise VpkError("truncated OtherMD5 section")
                emit(blob)

            # Trailer (signature section + any extra bytes beyond the declared size):
            # opaque and unrelated to the data we removed, so copy it verbatim.
            if header.trailer_size > 0:
                fin.seek(header.signature_start)
                blob = fin.read(header.trailer_size)
                if len(blob) != header.trailer_size:
                    raise VpkError("truncated trailer")
                fout.write(blob)
                if header.signature_size:
                    print(f"  note: preserved a {header.trailer_size} B signature trailer verbatim; "
                          "the archive's signature no longer covers the modified content")
    except BaseException:
        # Never leave a half-written .tmp next to the user's archives.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # ---- phase 4: install (every handle on the target is closed by now) -----------
    if not args.no_backup and in_place:
        backup = unique_backup_path(path)
        # A hardlink is instant and costs no extra space; fall back to a copy when the
        # filesystem does not support it.
        try:
            os.link(path, backup)
            print(f"  backup: {backup} (hardlink)")
        except OSError:
            import shutil
            shutil.copy2(path, backup)
            print(f"  backup: {backup} (copy)")

    os.replace(tmp, target)
    print(f"  wrote: {target} ({target.stat().st_size} B)")

    if args.verify:
        problems = verify(target)
        if problems:
            print("  VERIFY FAILED:")
            for p in problems[:20]:
                print(f"    ! {p}")
            if len(problems) > 20:
                print(f"    ! ... and {len(problems) - 20} more")
            return EXIT_VERIFY_FAILED
        print("  verify: OK (all CRC32 + all MD5 checksums match)")

    return EXIT_OK


def verify(path: Path):
    """Re-read the archive and check every CRC32 and every MD5. Returns problems."""
    problems = []
    with open(path, "rb") as f:
        try:
            header = read_header(f)
        except VpkError as exc:
            return [f"header: {exc}"]

        tree_bytes = f.read(header.tree_size)
        try:
            entries = parse_tree(tree_bytes)
        except VpkError as exc:
            return [f"tree: {exc}"]

        # CRC32 of every entry's payload, compared against the value in the tree.
        for e in entries:
            crc = zlib.crc32(e.preload_data) if e.preload_data else 0
            if e.length == 0:
                if crc != e.crc:
                    problems.append(f"{e.path}: crc 0x{e.crc:08X} != computed 0x{crc:08X}")
                continue
            if not e.embedded:
                continue  # payload lives in a different file; cannot check here
            if e.offset + e.length > header.file_data_size:
                problems.append(f"{e.path}: offset+length runs past the data region")
                continue
            f.seek(header.data_start + e.offset)
            remaining = e.length
            while remaining > 0:
                chunk = f.read(min(COPY_CHUNK, remaining))
                if not chunk:
                    problems.append(f"{e.path}: short read")
                    break
                crc = zlib.crc32(chunk, crc)
                remaining -= len(chunk)
            else:
                if crc != e.crc:
                    problems.append(f"{e.path}: crc 0x{e.crc:08X} != computed 0x{crc:08X}")

        if header.version < 2 or header.other_md5_size != 48:
            return problems

        f.seek(header.other_md5_start)
        other = f.read(48)
        if len(other) != 48:
            problems.append("OtherMD5 section: truncated")
            return problems
        tree_hash, archive_hash, whole_hash = other[0:16], other[16:32], other[32:48]

        computed_tree = hashlib.md5(tree_bytes).digest()
        if computed_tree != tree_hash:
            problems.append(f"md5 tree: stored {tree_hash.hex()} != computed {computed_tree.hex()}")

        f.seek(header.archive_md5_start)
        archive_bytes = f.read(header.archive_md5_size)
        computed_archive = hashlib.md5(archive_bytes).digest()
        if computed_archive != archive_hash:
            problems.append(f"md5 archive: stored {archive_hash.hex()} != computed {computed_archive.hex()}")

        # Field 2 covers everything before and including the two checksums above.
        f.seek(0)
        digest = hashlib.md5()
        remaining = header.other_md5_start + 32
        while remaining > 0:
            chunk = f.read(min(COPY_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        computed_whole = digest.digest()
        if computed_whole != whole_hash:
            problems.append(f"md5 whole: stored {whole_hash.hex()} != computed {computed_whole.hex()}")

    return problems


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remove_vpk_entry.py",
        description="Remove entries from VPK archives and repack them safely.",
        epilog=(
            "examples:\n"
            "  python remove_vpk_entry.py pak14_dir.vpk\n"
            "  python remove_vpk_entry.py pak14_dir.vpk --dry-run\n"
            "  python remove_vpk_entry.py D:\\mods\\待删除 --recursive\n"
            "  python remove_vpk_entry.py mod.vpk --ext vnmskel_c --no-backup --out mod_clean.vpk\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("paths", nargs="+", help="VPK file(s), or directories containing *.vpk")
    p.add_argument("--match", "-m", action="append", default=None,
                   help=f"case-insensitive path substring to remove (repeatable). "
                        f"Default: {DEFAULT_MATCH}")
    p.add_argument("--ext", "-e", action="append", default=None,
                   help="remove entries whose extension equals this exactly (repeatable), "
                        "e.g. vnmskel_c. Combines with --match (either match wins).")
    p.add_argument("--dry-run", "-n", action="store_true",
                   help="report what would be removed without writing anything")
    p.add_argument("--out", "-o", default=None,
                   help="write to this path instead of editing in place (single input only)")
    p.add_argument("--keep-holes", action="store_true",
                   help="do not compact the data region; surviving offsets stay as they are")
    p.add_argument("--no-backup", action="store_true", help="skip the .bak hardlink/copy")
    p.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                   help="skip the post-write verification pass")
    p.add_argument("--recursive", "-r", action="store_true",
                   help="recurse into subdirectories when a path is a directory")
    p.add_argument("--force", action="store_true",
                   help="allow removing every entry in an archive")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # Default target: the VPK skeleton override that Deadlock mods tend to ship.
    if not args.match and not args.ext:
        args.match = [DEFAULT_MATCH]
    args.match = args.match or []
    args.ext = args.ext or []

    inputs = collect_inputs(args.paths, args.recursive)
    if not inputs:
        print("no input archives found", file=sys.stderr)
        return EXIT_USAGE
    if args.out and len(inputs) > 1:
        print("--out can only be used with a single input archive", file=sys.stderr)
        return EXIT_USAGE

    hard_failure = None
    any_matched = False
    any_no_match = False

    for path in inputs:
        try:
            code = process(path, args)
        except VpkError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            code = EXIT_ERROR
        except OSError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            code = EXIT_ERROR

        if code in (EXIT_ERROR, EXIT_VERIFY_FAILED):
            hard_failure = code
        elif code == EXIT_NO_MATCH:
            any_no_match = True
        else:
            any_matched = True

    # A hard failure anywhere dominates. Otherwise report "no match" only when no
    # input matched at all, so a batch run that cleaned some archives still exits 0.
    if hard_failure is not None:
        return hard_failure
    if any_matched:
        return EXIT_OK
    if any_no_match:
        return EXIT_NO_MATCH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
