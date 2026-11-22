"""Output integrity checks before a revision may be marked ready (directive 18)."""
from __future__ import annotations

import io
import zipfile

from .locator import AFG_BOOKMARK, locate
from .ooxml import NS, W, DocxPackage, paragraph_text, parse_xml

ALLOWED_CHANGED = {"word/document.xml", "word/styles.xml", "word/settings.xml"}


class OutputInvalid(RuntimeError):
    pass


def verify_output(original: bytes, output: bytes, manifest: list[dict]) -> dict:
    problems: list[str] = []
    try:
        zo = zipfile.ZipFile(io.BytesIO(output))
        zi = zipfile.ZipFile(io.BytesIO(original))
    except zipfile.BadZipFile as e:
        raise OutputInvalid("output_not_zip") from e
    if zo.testzip() is not None:
        problems.append("crc_error")
    on, inn = set(zo.namelist()), set(zi.namelist())
    if on != inn:
        problems.append("package_members_changed")
    changed = []
    for n in inn & on:
        if zi.read(n) != zo.read(n):
            changed.append(n)
            if n not in ALLOWED_CHANGED:
                problems.append(f"unexpected_change:{n}")
    for n in on:
        if n.endswith(".xml") or n.endswith(".rels"):
            try:
                parse_xml(zo.read(n))
            except Exception:
                problems.append(f"bad_xml:{n}")
    pkg_o = DocxPackage.from_bytes(output)
    pkg_i = DocxPackage.from_bytes(original)
    a_o, _, _ = locate(pkg_o)
    a_i, _, _ = locate(pkg_i)
    if len(a_o.figures) != len(a_i.figures):
        problems.append("figure_count_changed")
    if [f.media_sha256 for f in a_o.figures] != [f.media_sha256 for f in a_i.figures]:
        problems.append("figure_order_or_media_changed")
    # each written caption: exactly one bookmark, expected text, adjacent to a picture paragraph
    root = pkg_o.xml(pkg_o.main_part)
    marks: dict[str, list] = {}
    for b in root.iter(W + "bookmarkStart"):
        nm = b.get(W + "name") or ""
        if nm.startswith(AFG_BOOKMARK):
            marks.setdefault(nm[len(AFG_BOOKMARK):], []).append(b.getparent())
    verified = 0
    for item in manifest:
        ps = marks.get(item["figure_id"], [])
        if len(ps) != 1:
            problems.append(f"caption_count:{item['figure_id']}:{len(ps)}")
            continue
        p = ps[0]
        txt = paragraph_text(p).strip()
        if txt != item["text"]:
            problems.append(f"caption_text_mismatch:{item['figure_id']}")
            continue
        neighbours = [p.getprevious(), p.getnext()]
        prev2 = p.getprevious().getprevious() if p.getprevious() is not None else None
        neighbours.append(prev2)
        if not any(n is not None and (n.find(".//w:drawing", NS) is not None or n.find(".//w:pict", NS) is not None)
                   for n in neighbours):
            problems.append(f"caption_not_adjacent:{item['figure_id']}")
            continue
        verified += 1
    if problems:
        raise OutputInvalid(";".join(problems[:20]))
    return {"changed_parts": sorted(changed), "verified_captions": verified, "figures": len(a_o.figures)}
