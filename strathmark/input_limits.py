"""Bound untrusted local containers before parsers allocate expanded data."""

import gzip
import hashlib
import io
import os
import stat
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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
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
                    if tag == "row" and int(element.get("r", "0")) > 100_000:
                        raise ValueError("Workbook exceeds row budget")
                    if tag == "c":
                        cells += 1
                        coordinate = element.get("r", "A1")
                        column = 0
                        for letter in coordinate:
                            if not letter.isalpha():
                                break
                            column = column * 26 + ord(letter.upper()) - 64
                        if column > 256 or cells > 1_000_000:
                            raise ValueError("Workbook exceeds cell budget")
                    if tag == "dimension":
                        # A forged dimension can make read-only iteration expand empty rows.
                        endpoint = element.get("ref", "A1").split(":")[-1]
                        row = "".join(c for c in endpoint if c.isdigit())
                        letters = "".join(c for c in endpoint if c.isalpha())
                        column = 0
                        for letter in letters:
                            column = column * 26 + ord(letter.upper()) - 64
                        if int(row or "0") > 100_000 or column > 256:
                            raise ValueError("Workbook exceeds dimension budget")
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
