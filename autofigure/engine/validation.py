"""Strict Word-only upload validation (directives 07, 29, 47).

Checks run before a file is stored or processed: extension, magic bytes,
ZIP/OPC structure, archive-bomb limits, path traversal, macros, encryption,
external relationships and hostile XML.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from .ooxml import MACRO_DOC_CT, MAIN_DOC_CT, NS, TEMPLATE_DOC_CT, parse_xml

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"


@dataclass
class Limits:
    max_bytes: int = 50 * 1024 * 1024
    max_uncompressed: int = 400 * 1024 * 1024
    max_entries: int = 5000
    max_ratio: int = 200
    max_media: int = 1000
    max_xml_part: int = 60 * 1024 * 1024


@dataclass
class ValidationResult:
    ok: bool
    code: str
    message: str
    warnings: list[str] = field(default_factory=list)
    media_count: int = 0


SNIFFED = [
    (b"%PDF", "pdf", "This is a PDF. Please upload a Microsoft Word (.docx) document."),
    (b"\x89PNG", "png", "This is a PNG image. Please upload a Microsoft Word (.docx) document."),
    (b"\xff\xd8\xff", "jpg", "This is a JPG image. Please upload a Microsoft Word (.docx) document."),
    (b"GIF8", "gif", "This is a GIF image. Please upload a Microsoft Word (.docx) document."),
    (b"{\\rtf", "rtf", "This is an RTF file. Save it as Word Document (.docx) and upload again."),
]


def fail(code: str, msg: str) -> ValidationResult:
    return ValidationResult(False, code, msg)


def validate_docx(filename: str, raw: bytes, limits: Limits | None = None) -> ValidationResult:
    limits = limits or Limits()
    name = (filename or "").strip()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if len(raw) == 0:
        return fail("empty_file", "The file is empty.")
    if len(raw) > limits.max_bytes:
        return fail("too_large", f"The file is larger than the {limits.max_bytes // (1024 * 1024)} MB limit.")
    for magic, kind, msg in SNIFFED:
        if raw.startswith(magic):
            return fail(f"wrong_type_{kind}", msg)
    if ext in ("docm", "dotm"):
        return fail("macro_enabled", "Macro-enabled Word files (.docm/.dotm) are not accepted. Save as .docx without macros.")
    if ext == "doc" or raw.startswith(OLE_MAGIC):
        if raw.startswith(OLE_MAGIC) and ext == "docx":
            return fail("encrypted_or_legacy", "This file is password-protected or a legacy .doc renamed to .docx. "
                                               "Remove the password / save as .docx and try again.")
        return fail("legacy_doc", "Legacy .doc files are not supported. In Word choose File → Save As → Word Document (.docx).")
    if ext not in ("docx",):
        return fail("wrong_extension", "Only Microsoft Word .docx files are accepted.")
    if not raw.startswith(ZIP_MAGIC):
        return fail("not_ooxml", "This file is not a valid Word document (.docx).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        infos = zf.infolist()
    except zipfile.BadZipFile:
        return fail("corrupt_package", "The Word file is damaged and cannot be opened. Re-save it in Word and try again.")
    if len(infos) > limits.max_entries:
        return fail("too_many_parts", "The document package contains too many parts.")
    total = 0
    media = 0
    names = set()
    for i in infos:
        n = i.filename
        if n.startswith("/") or "\\" in n or ".." in n.split("/") or "\x00" in n:
            return fail("unsafe_path", "The document package contains unsafe paths.")
        if posixpath.normpath(n) != n.rstrip("/") and not i.is_dir():
            return fail("unsafe_path", "The document package contains unsafe paths.")
        if i.flag_bits & 0x1:
            return fail("encrypted_zip", "The Word file is encrypted. Remove the password and try again.")
        total += i.file_size
        if i.compress_size and i.file_size / max(i.compress_size, 1) > limits.max_ratio and i.file_size > 1_000_000:
            return fail("zip_bomb", "The document package has a suspicious compression ratio.")
        if n.startswith("word/media/"):
            media += 1
        if n.lower().endswith("vbaproject.bin"):
            return fail("macro_enabled", "This document contains macros. Save it as a macro-free .docx and try again.")
        names.add(n)
    if total > limits.max_uncompressed:
        return fail("too_large_uncompressed", "The document expands to more data than the processing limit allows.")
    if media > limits.max_media:
        return fail("too_many_images", f"The document has more than {limits.max_media} embedded media files.")
    if "[Content_Types].xml" not in names:
        return fail("not_ooxml", "This ZIP is not a Word document (no [Content_Types].xml).")
    try:
        ct = parse_xml(zf.read("[Content_Types].xml"))
    except Exception:
        return fail("corrupt_package", "The Word package metadata is invalid.")
    main = None
    for ov in ct.findall("ct:Override", NS):
        ctype = ov.get("ContentType")
        if ctype == MACRO_DOC_CT:
            return fail("macro_enabled", "Macro-enabled Word documents are not accepted.")
        if ctype in (MAIN_DOC_CT, TEMPLATE_DOC_CT):
            main = (ov.get("PartName") or "").lstrip("/")
    if not main or main not in names:
        return fail("not_word", "This Open XML file is not a Word document (it may be Excel or PowerPoint).")
    warnings: list[str] = []
    for n in names:
        if not (n.endswith(".xml") or n.endswith(".rels")):
            continue
        info = zf.getinfo(n)
        if info.file_size > limits.max_xml_part:
            return fail("xml_too_large", "A part of this document is too large to process safely.")
        data = zf.read(n)
        if re.search(rb"<!DOCTYPE|<!ENTITY", data[:8192]):
            return fail("xml_doctype", "The document contains unsafe XML declarations.")
        if n.endswith(".rels") and b'TargetMode="External"' in data:
            if re.search(rb'Type="[^"]*/(attachedTemplate|oleObject|subDocument|frame)"[^>]*TargetMode="External"', data) or \
               re.search(rb'TargetMode="External"[^>]*Type="[^"]*/(attachedTemplate|oleObject|subDocument|frame)"', data):
                warnings.append("external_relationship_ignored")
            else:
                warnings.append("external_links_present")
    try:
        parse_xml(zf.read(main))
    except Exception:
        return fail("corrupt_xml", "The main document XML is damaged. Re-save the file in Word and try again.")
    return ValidationResult(True, "ok", "Valid Word document", warnings, media)


def safe_display_name(name: str) -> str:
    name = (name or "document.docx").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip() or "document.docx"
    return name[:180]


def output_filename(original: str) -> str:
    stem = safe_display_name(original)
    if stem.lower().endswith(".docx"):
        stem = stem[:-5]
    stem = re.sub(r"[^\w\-. ()က-႟]", "_", stem).strip(" .") or "document"
    return f"{stem[:120]}_AutoFigure.docx"
