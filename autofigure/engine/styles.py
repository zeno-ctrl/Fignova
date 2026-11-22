"""Word style resolution: inheritance, outline levels and well-known styles."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lxml import etree

from .ooxml import NS, W, DocxPackage


@dataclass
class StyleInfo:
    style_id: str
    name: str
    based_on: Optional[str]
    outline_level: Optional[int]
    type: str


class StyleBook:
    def __init__(self, pkg: DocxPackage, extra_heading1_ids: Optional[list[str]] = None):
        self.styles: dict[str, StyleInfo] = {}
        self.default_paragraph: Optional[str] = None
        if pkg.has("word/styles.xml"):
            root = pkg.xml("word/styles.xml")
            for st in root.findall("w:style", NS):
                sid = st.get(W + "styleId")
                if not sid:
                    continue
                name_el = st.find("w:name", NS)
                based = st.find("w:basedOn", NS)
                ol = st.find("w:pPr/w:outlineLvl", NS)
                info = StyleInfo(
                    style_id=sid,
                    name=(name_el.get(W + "val") if name_el is not None else sid) or sid,
                    based_on=based.get(W + "val") if based is not None else None,
                    outline_level=int(ol.get(W + "val")) if ol is not None and (ol.get(W + "val") or "").isdigit() else None,
                    type=st.get(W + "type") or "paragraph",
                )
                self.styles[sid] = info
                if st.get(W + "default") in ("1", "true") and info.type == "paragraph":
                    self.default_paragraph = sid
        self.extra_heading1 = set(extra_heading1_ids or [])

    def chain(self, sid: Optional[str]) -> list[StyleInfo]:
        out: list[StyleInfo] = []
        seen = set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            out.append(self.styles[sid])
            sid = self.styles[sid].based_on
        return out

    def heading_level(self, sid: Optional[str]) -> Optional[int]:
        """Return 1-based heading level resolved through inheritance."""
        if sid in self.extra_heading1:
            return 1
        for st in self.chain(sid):
            nm = st.name.strip().lower()
            if nm.startswith("heading ") and nm[8:].strip().isdigit():
                return int(nm[8:].strip())
            if st.outline_level is not None and st.outline_level < 9:
                return st.outline_level + 1
        if sid and sid.lower().startswith("heading") and sid[7:].isdigit():
            return int(sid[7:])
        return None

    def is_caption(self, sid: Optional[str]) -> bool:
        return any(st.name.strip().lower() == "caption" or st.style_id == "Caption" for st in self.chain(sid))

    def caption_style_id(self) -> Optional[str]:
        for sid, st in self.styles.items():
            if st.name.strip().lower() == "caption" and st.type == "paragraph":
                return sid
        return "Caption" if "Caption" in self.styles else None


def paragraph_style(p: etree._Element) -> Optional[str]:
    ps = p.find("w:pPr/w:pStyle", NS)
    return ps.get(W + "val") if ps is not None else None


def paragraph_outline(p: etree._Element) -> Optional[int]:
    ol = p.find("w:pPr/w:outlineLvl", NS)
    if ol is not None and (ol.get(W + "val") or "").isdigit():
        return int(ol.get(W + "val"))
    return None


CAPTION_STYLE_XML = (
    '<w:style xmlns:w="%s" w:type="paragraph" w:styleId="Caption">'
    '<w:name w:val="caption"/>%s<w:next w:val="Normal"/><w:uiPriority w:val="35"/>'
    '<w:unhideWhenUsed/><w:qFormat/>'
    '<w:pPr><w:spacing w:before="60" w:after="200" w:line="240" w:lineRule="auto"/></w:pPr>'
    '<w:rPr><w:i/><w:iCs/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>'
)


def ensure_caption_style(pkg: DocxPackage, book: StyleBook) -> str:
    """Return the Caption style id, adding a standard definition when absent."""
    sid = book.caption_style_id()
    if sid:
        return sid
    if not pkg.has("word/styles.xml"):
        return "Caption"  # nothing to attach to; paragraph keeps direct formatting
    root = pkg.xml("word/styles.xml")
    based = '<w:basedOn w:val="Normal"/>' if "Normal" in book.styles else ""
    el = etree.fromstring(CAPTION_STYLE_XML % (NS["w"], based))
    root.append(el)
    pkg.mark_dirty("word/styles.xml")
    book.styles["Caption"] = StyleInfo("Caption", "caption", "Normal" if based else None, None, "paragraph")
    return "Caption"
