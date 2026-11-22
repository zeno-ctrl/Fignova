"""Surgical OOXML mutation: insert / renumber / replace caption paragraphs.

Always starts from the immutable original bytes, so regenerating after an
edit can never stack captions (idempotent by construction, directive 13).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Optional

from lxml import etree

from .locator import AFG_BOOKMARK, _next_para, _prev_para, locate
from .model import FigureOccurrence
from .ooxml import NS, W, DocxPackage
from .settings import CaptionSettings
from .styles import StyleBook, ensure_caption_style

CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
MYANMAR_RE = re.compile(r"[က-႟]")


def clean_text(s: str, limit: int = 240) -> str:
    s = CONTROL_RE.sub("", s or "")
    s = " ".join(s.split())
    return s[:limit]


def compose_caption(settings: CaptionSettings, label: str, description: str) -> str:
    description = clean_text(description)
    text = f"{settings.prefix} {label}"
    if description:
        text += settings.separator + description
        if settings.end_punctuation and not description.endswith((".", "!", "?", "။")):
            text += "."
    return text


@dataclass
class PlannedCaption:
    figure_id: str
    action: str  # insert | renumber | replace
    label: str
    text: str
    chapter_number: Optional[int]
    index: int
    use_seq: bool
    seq_restart: Optional[int]


def _w(tag: str) -> str:
    return W + tag


def _rpr(settings: CaptionSettings, text: str) -> etree._Element:
    rpr = etree.Element(_w("rPr"))
    if MYANMAR_RE.search(text):
        f = etree.SubElement(rpr, _w("rFonts"))
        f.set(_w("ascii"), "Myanmar Text")
        f.set(_w("hAnsi"), "Myanmar Text")
        f.set(_w("cs"), "Myanmar Text")
    if settings.color == "neutral":
        etree.SubElement(rpr, _w("b")).set(_w("val"), "0")
        etree.SubElement(rpr, _w("bCs")).set(_w("val"), "0")
    if settings.italic:
        etree.SubElement(rpr, _w("i"))
        etree.SubElement(rpr, _w("iCs"))
    else:
        etree.SubElement(rpr, _w("i")).set(_w("val"), "0")
    if settings.color == "neutral":
        etree.SubElement(rpr, _w("color")).set(_w("val"), "262626")
    hp = str(int(round(settings.font_size * 2)))
    etree.SubElement(rpr, _w("sz")).set(_w("val"), hp)
    etree.SubElement(rpr, _w("szCs")).set(_w("val"), hp)
    return rpr


def _text_run(settings: CaptionSettings, text: str) -> etree._Element:
    r = etree.Element(_w("r"))
    r.append(_rpr(settings, text))
    t = etree.SubElement(r, _w("t"))
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return r


def _field_runs(settings: CaptionSettings, instr: str, cached: str) -> list[etree._Element]:
    runs = []
    for kind in ("begin", None, "separate", "cached", "end"):
        r = etree.Element(_w("r"))
        r.append(_rpr(settings, cached))
        if kind in ("begin", "separate", "end"):
            etree.SubElement(r, _w("fldChar")).set(_w("fldCharType"), kind)
        elif kind is None:
            it = etree.SubElement(r, _w("instrText"))
            it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            it.text = f" {instr} "
        else:
            t = etree.SubElement(r, _w("t"))
            t.text = cached
        runs.append(r)
    return runs


def _caption_children(settings: CaptionSettings, plan: PlannedCaption, description: str) -> list[etree._Element]:
    prefix = settings.prefix + " "
    out: list[etree._Element] = []
    tail = ""
    if description:
        tail = settings.separator + clean_text(description)
        if settings.end_punctuation and not tail.endswith((".", "!", "?", "။")):
            tail += "."
    if settings.field_mode == "seq" and plan.use_seq:
        lead = prefix
        if plan.chapter_number is not None and settings.number_style != "document":
            lead += f"{plan.chapter_number}{'-' if settings.number_style == 'chapter_dash' else '.'}"
        out.append(_text_run(settings, lead))
        label_id = re.sub(r"[^A-Za-z]", "", settings.prefix) or "Figure"
        instr = f"SEQ {label_id} \\* ARABIC"
        if plan.seq_restart is not None:
            instr += f" \\r {plan.seq_restart}"
        out.extend(_field_runs(settings, instr, str(plan.index)))
        if tail:
            out.append(_text_run(settings, tail))
    else:
        out.append(_text_run(settings, plan.text))
    return out


def _ppr(settings: CaptionSettings, style_id: str, base: Optional[etree._Element] = None) -> etree._Element:
    ppr = copy.deepcopy(base) if base is not None else etree.Element(_w("pPr"))
    for child in list(ppr):
        if child.tag in (_w("pStyle"), _w("jc"), _w("keepNext"), _w("numPr")):
            ppr.remove(child)
    ps = etree.Element(_w("pStyle"))
    ps.set(_w("val"), style_id)
    ppr.insert(0, ps)
    if settings.position == "above":
        ppr.insert(1, etree.Element(_w("keepNext")))
    if base is None:
        sp = etree.SubElement(ppr, _w("spacing"))
        sp.set(_w("before"), "60")
        sp.set(_w("after"), "200")
    jc = etree.SubElement(ppr, _w("jc"))
    jc.set(_w("val"), {"center": "center", "left": "left", "right": "right"}[settings.align])
    # Schema order inside pPr matters for strict consumers: reorder known children.
    order = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr", "suppressLineNumbers",
             "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct",
             "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind", "contextualSpacing",
             "mirrorIndents", "suppressOverlap", "jc", "textDirection", "textAlignment", "textboxTightWrap",
             "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange"]
    rank = {W + n: i for i, n in enumerate(order)}
    kids = sorted(list(ppr), key=lambda e: rank.get(e.tag, 99))
    for k in list(ppr):
        ppr.remove(k)
    for k in kids:
        ppr.append(k)
    return ppr


def _set_keep_next(p: etree._Element) -> None:
    ppr = p.find("w:pPr", NS)
    if ppr is None:
        ppr = etree.Element(_w("pPr"))
        p.insert(0, ppr)
    if ppr.find("w:keepNext", NS) is None:
        kn = etree.Element(_w("keepNext"))
        ps = ppr.find("w:pStyle", NS)
        if ps is not None:
            ps.addnext(kn)
        else:
            ppr.insert(0, kn)


class _BookmarkIds:
    def __init__(self, root: etree._Element):
        ids = [int(b.get(_w("id"))) for b in root.iter(_w("bookmarkStart")) if (b.get(_w("id")) or "").isdigit()]
        self.next = max(ids, default=0) + 1000

    def take(self) -> str:
        self.next += 1
        return str(self.next)


def _fill_paragraph(p: etree._Element, settings: CaptionSettings, style_id: str, plan: PlannedCaption,
                    description: str, bm: _BookmarkIds, keep_ppr: bool) -> None:
    old_ppr = p.find("w:pPr", NS)
    for child in list(p):
        p.remove(child)
    p.append(_ppr(settings, style_id, old_ppr if keep_ppr else None))
    bid = bm.take()
    bs = etree.SubElement(p, _w("bookmarkStart"))
    bs.set(_w("id"), bid)
    bs.set(_w("name"), (AFG_BOOKMARK + plan.figure_id)[:40])
    for el in _caption_children(settings, plan, description):
        p.append(el)
    be = etree.SubElement(p, _w("bookmarkEnd"))
    be.set(_w("id"), bid)


def build_plan(figures: list[FigureOccurrence], settings: CaptionSettings) -> list[PlannedCaption]:
    plans: list[PlannedCaption] = []
    prev_seq_by_chapter: dict[str, bool] = {}
    for f in sorted(figures, key=lambda x: x.order):
        if not f.eligible:
            continue
        key = f.chapter_key or ""
        acts = f.status in ("insert", "renumber", "replace")
        existing_seq = bool(f.existing and f.existing.has_seq_field)
        if acts:
            restart = None
            if not prev_seq_by_chapter.get(key):
                restart = f.figure_index  # first SEQ-driven caption of this chapter (or after static ones)
            plans.append(PlannedCaption(f.figure_id, f.status, f.label or "", compose_caption(settings, f.label or "", f.caption_text),
                                        None if settings.number_style == "document" else f.chapter_number,
                                        f.figure_index or 0, True, restart))
            prev_seq_by_chapter[key] = True
        else:
            prev_seq_by_chapter[key] = existing_seq and f.status == "preserve"
    return plans


def write_captions(original: bytes, figures: list[FigureOccurrence], settings: CaptionSettings,
                   extra_heading1_ids: Optional[list[str]] = None) -> tuple[bytes, list[dict]]:
    pkg = DocxPackage.from_bytes(original)
    analysis, handles, _ = locate(pkg, extra_heading1_ids)
    known = {f.figure_id for f in analysis.figures}
    by_id = {f.figure_id: f for f in figures}
    missing = [fid for fid, f in by_id.items() if f.eligible and fid not in known]
    if missing:
        raise RuntimeError("figure_identity_mismatch")
    book = StyleBook(pkg, extra_heading1_ids)
    style_id = ensure_caption_style(pkg, book)
    part = pkg.main_part
    root = pkg.xml(part)
    bm = _BookmarkIds(root)
    plans = build_plan(figures, settings)
    manifest: list[dict] = []
    for plan in sorted(plans, key=lambda p: by_id[p.figure_id].order, reverse=True):
        fig = by_id[plan.figure_id]
        h = handles[plan.figure_id]
        para = h.paragraph
        if plan.action == "insert":
            newp = etree.Element(_w("p"))
            _fill_paragraph(newp, settings, style_id, plan, fig.caption_text, bm, keep_ppr=False)
            if settings.position == "below":
                para.addnext(newp)
                if settings.keep_with_image:
                    _set_keep_next(para)
            else:
                para.addprevious(newp)
        else:
            target = _next_para(para) if fig.existing and fig.existing.position == "below" else _prev_para(para)
            if target is None:
                raise RuntimeError("existing_caption_not_found")
            desc = fig.caption_text if plan.action == "replace" else (fig.existing.description if fig.existing else "")
            if plan.action == "renumber" and fig.caption_source == "manual":
                desc = fig.caption_text
            _fill_paragraph(target, settings, style_id, plan, desc, bm, keep_ppr=True)
            plan.text = compose_caption(settings, plan.label, desc)
        manifest.append({"figure_id": plan.figure_id, "action": plan.action, "label": plan.label, "text": plan.text})
    pkg.mark_dirty(part)
    if settings.update_fields_on_open and pkg.has("word/settings.xml"):
        sroot = pkg.xml("word/settings.xml")
        if sroot.find("w:updateFields", NS) is None:
            uf = etree.Element(_w("updateFields"))
            uf.set(_w("val"), "true")
            later = ["hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars", "rsids", "mathPr",
                     "attachedSchema", "themeFontLang", "clrSchemeMapping", "doNotIncludeSubdocsInStats",
                     "doNotAutoCompressPictures", "forceUpgrade", "captions", "readModeInkLockDown", "smartTagType",
                     "schemaLibrary", "shapeDefaults", "doNotEmbedSmartTags", "decimalSymbol", "listSeparator"]
            anchor = next((c for c in sroot if etree.QName(c).localname in later), None)
            if anchor is not None:
                anchor.addprevious(uf)
            else:
                sroot.append(uf)
            pkg.mark_dirty("word/settings.xml")
    manifest.reverse()
    return pkg.to_bytes(), manifest
