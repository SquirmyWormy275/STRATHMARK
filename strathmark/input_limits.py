"""Bound untrusted local containers before parsers allocate expanded data."""

import gzip
import hashlib
import io
import os
import re
import stat
import struct
import tarfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_INPUT_BYTES = 67_108_864
MAX_EXPANDED_BYTES = 134_217_728
MAX_ARCHIVE_MEMBERS = 1024


def read_bounded_file(path, maximum=MAX_INPUT_BYTES):
    path = Path(path)
    named = path.lstat()
    if not stat.S_ISREG(named.st_mode) or named.st_size > maximum:
        raise ValueError("Input file exceeds its regular-file byte budget")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum
            or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("Input file exceeds its regular-file byte budget")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > maximum or (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("Input file changed or exceeded its byte budget")
        return raw


def preflight_zip(raw):
    """Bound directory parsing before ZipFile constructs any ZipInfo objects."""
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Archive exceeds input-byte budget")
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise ValueError("Archive has no bounded end directory")
    _, disk, start_disk, disk_count, count, size, offset, comment = struct.unpack_from(
        "<4s4H2IH", raw, end
    )
    if (
        disk
        or start_disk
        or disk_count != count
        or count > MAX_ARCHIVE_MEMBERS
        or size > 1_048_576
        or offset + size != end
        or end + 22 + comment != len(raw)
    ):
        raise ValueError("Archive exceeds member or directory budget")
    # Do not trust the declared count: Python walks the actual directory bytes.
    cursor = offset
    observed = 0
    while cursor < end:
        observed += 1
        if (
            observed > MAX_ARCHIVE_MEMBERS
            or cursor + 46 > end
            or raw[cursor : cursor + 4] != b"PK\x01\x02"
        ):
            raise ValueError("Archive exceeds member or directory budget")
        name, extra, note, member_disk = struct.unpack_from("<4H", raw, cursor + 28)
        if member_disk:
            raise ValueError("Archive cannot span disks")
        cursor += 46 + name + extra + note
    if cursor != end or observed != count:
        raise ValueError("Archive directory count or length differs")


def _bounded_coordinate(value, label):
    # Bound text before integer conversion or base-26 arithmetic. XML attributes
    # are untrusted, and a deadline check after huge bigint work is too late.
    if len(value) > 9 or re.fullmatch(r"[A-Za-z]{1,3}[1-9][0-9]{0,5}", value) is None:
        raise ValueError(f"Workbook exceeds {label} coordinate budget")
    column = 0
    split = 0
    for split, letter in enumerate(value):
        if "0" <= letter <= "9":
            break
        column = column * 26 + ord(letter.upper()) - 64
    if column > 256 or int(value[split:]) > 100_000:
        raise ValueError(f"Workbook exceeds {label} budget")


def check_zip(archive, *, stored_only=False, maximum=MAX_EXPANDED_BYTES):
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("Archive exceeds member budget")
    total = 0
    for entry in entries:
        total += entry.file_size
        if (
            entry.file_size > MAX_INPUT_BYTES
            or total > maximum
            or entry.file_size > max(1, entry.compress_size) * 200
            or (stored_only and entry.compress_type != zipfile.ZIP_STORED)
        ):
            raise ValueError("Archive exceeds expanded-byte or compression budget")


def bounded_workbook(path):
    raw = read_bounded_file(path)
    preflight_zip(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        check_zip(archive)
        sheets = [
            item
            for item in archive.infolist()
            if item.filename.startswith("xl/worksheets/") and item.filename.endswith(".xml")
        ]
        if len(sheets) > 32:
            raise ValueError("Workbook exceeds sheet budget")
        cells = 0
        deadline = time.monotonic() + 10
        for member in sheets:
            with archive.open(member) as stream:
                for _, element in ElementTree.iterparse(stream, events=("end",)):
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag == "row":
                        row = element.get("r", "0")
                        if (
                            len(row) > 6
                            or not row.isascii()
                            or not row.isdecimal()
                            or int(row) > 100_000
                        ):
                            raise ValueError("Workbook exceeds row budget")
                    if tag == "c":
                        cells += 1
                        _bounded_coordinate(element.get("r", "A1"), "cell")
                        if cells > 1_000_000:
                            raise ValueError("Workbook exceeds cell budget")
                    if tag == "dimension":
                        # A forged dimension can make read-only iteration expand empty rows.
                        dimension = element.get("ref", "A1")
                        if len(dimension) > 19 or dimension.count(":") > 1:
                            raise ValueError("Workbook exceeds dimension budget")
                        for endpoint in dimension.split(":"):
                            _bounded_coordinate(endpoint, "dimension")
                    if time.monotonic() > deadline:
                        raise ValueError("Workbook preflight deadline expired")
                    element.clear()
    return io.BytesIO(raw)


def tar_stream_digests(compressed):
    deadline = time.monotonic() + 30
    expanded = 0

    class BudgetReader:
        def read(self, size=-1):
            nonlocal expanded
            if time.monotonic() > deadline:
                raise ValueError("Recovery archive deadline expired")
            chunk = decompressed.read(
                min(size if size >= 0 else 65536, MAX_EXPANDED_BYTES - expanded + 1)
            )
            expanded += len(chunk)
            if expanded > MAX_EXPANDED_BYTES:
                raise ValueError("Recovery archive exceeds expanded-byte budget")
            return chunk

    result = {}
    with gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed:
        with tarfile.open(fileobj=BudgetReader(), mode="r|") as archive:
            for count, member in enumerate(archive, 1):
                if count > MAX_ARCHIVE_MEMBERS or member.size > MAX_INPUT_BYTES:
                    raise ValueError("Recovery archive exceeds member budget")
                if (
                    member.issym()
                    or member.islnk()
                    or Path(member.name).is_absolute()
                    or ".." in Path(member.name).parts
                    or member.name in result
                ):
                    raise ValueError("Recovery archive contains unsafe members or repeated names")
                if not member.isfile():
                    continue
                digest = hashlib.sha256()
                stream = archive.extractfile(member)
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
                result[member.name] = digest.hexdigest()
    return result
