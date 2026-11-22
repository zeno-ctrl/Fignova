"""Low-level OOXML package access.

The package is kept as an ordered list of zip members. Only the parts that the
engine deliberately changes are re-serialised; every other member is written
back byte-for-byte, so unrelated Word features survive untouched.
"""
from __future__ import annotations

import io
import posixpath
import zipfile
from dataclasses import dataclass, field

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "v": "urn:schemas-microsoft-com:vml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
W = "{%s}" % NS["w"]
R = "{%s}" % NS["r"]

MAIN_DOC_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
TEMPLATE_DOC_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
MACRO_DOC_CT = "application/vnd.ms-word.document.macroEnabled.main+xml"

# Parser hardened against XXE / entity expansion / network access.
SAFE_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_blank_text=False
)


def parse_xml(data: bytes) -> etree._Element:
    if b"<!DOCTYPE" in data[:2048] or b"<!ENTITY" in data[:4096]:
        raise ValueError("xml_doctype_forbidden")
    return etree.fromstring(data, SAFE_PARSER)


def serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


@dataclass
class Relationship:
    rid: str
    type: str
    target: str
    external: bool


@dataclass
class DocxPackage:
    infos: list[zipfile.ZipInfo]
    data: dict[str, bytes]
    _xml_cache: dict[str, etree._Element] = field(default_factory=dict)
    _dirty: set[str] = field(default_factory=set)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DocxPackage":
        zf = zipfile.ZipFile(io.BytesIO(raw))
        infos = zf.infolist()
        data = {i.filename: zf.read(i.filename) for i in infos if not i.is_dir()}
        return cls(infos=[i for i in infos if not i.is_dir()], data=data)

    # --- parts -------------------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self.data

    def xml(self, name: str) -> etree._Element:
        if name not in self._xml_cache:
            self._xml_cache[name] = parse_xml(self.data[name])
        return self._xml_cache[name]

    def mark_dirty(self, name: str) -> None:
        self._dirty.add(name)

    def set_bytes(self, name: str, raw: bytes) -> None:
        if name not in self.data:
            self.infos.append(zipfile.ZipInfo(name))
        self.data[name] = raw
        self._xml_cache.pop(name, None)
        self._dirty.discard(name)

    @property
    def main_part(self) -> str:
        root = self.xml("[Content_Types].xml")
        for ov in root.findall("ct:Override", NS):
            if ov.get("ContentType") in (MAIN_DOC_CT, TEMPLATE_DOC_CT, MACRO_DOC_CT):
                return ov.get("PartName").lstrip("/")
        return "word/document.xml"

    def rels_for(self, part: str) -> dict[str, Relationship]:
        d, b = posixpath.split(part)
        rels_name = posixpath.join(d, "_rels", b + ".rels")
        out: dict[str, Relationship] = {}
        if rels_name not in self.data:
            return out
        root = self.xml(rels_name)
        for rel in root.findall("rel:Relationship", NS):
            external = rel.get("TargetMode") == "External"
            target = rel.get("Target", "")
            if not external:
                target = posixpath.normpath(posixpath.join(d, target)).lstrip("/")
            out[rel.get("Id")] = Relationship(rel.get("Id"), rel.get("Type", ""), target, external)
        return out

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        for name in list(self._dirty):
            self.data[name] = serialize(self._xml_cache[name])
        self._dirty.clear()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            names = [i.filename for i in self.infos]
            # [Content_Types].xml must be first for some consumers.
            if "[Content_Types].xml" in names:
                names.remove("[Content_Types].xml")
                names.insert(0, "[Content_Types].xml")
            info_by_name = {i.filename: i for i in self.infos}
            for name in names:
                src = info_by_name[name]
                zi = zipfile.ZipInfo(name, date_time=src.date_time)
                zi.compress_type = zipfile.ZIP_STORED if name.startswith("word/media/") else zipfile.ZIP_DEFLATED
                zi.external_attr = src.external_attr
                zf.writestr(zi, self.data[name])
        return buf.getvalue()


def paragraph_text(p: etree._Element) -> str:
    parts = []
    for el in p.iter(W + "t", W + "tab", W + "br"):
        if el.tag == W + "t":
            # ignore text inside text boxes nested in this paragraph
            if any(a.tag == W + "txbxContent" for a in el.iterancestors()) and not _same_story(el, p):
                continue
            parts.append(el.text or "")
        elif el.tag == W + "tab":
            parts.append("\t")
    return "".join(parts)


def _same_story(el: etree._Element, p: etree._Element) -> bool:
    for anc in el.iterancestors():
        if anc is p:
            return True
        if anc.tag == W + "txbxContent":
            return False
    return False
