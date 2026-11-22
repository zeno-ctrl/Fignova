"""Figure discovery, Heading 1 / LO detection and existing-caption detection.

Walks the main document story in true reading order (directives 09, 26, 49, 50).
Package media files are never counted directly: every *displayed occurrence*
(w:drawing or VML picture) is a separate figure candidate, so one embedded
photo used twice yields two independent figures.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

from lxml import etree

from .model import DocumentAnalysis, ExistingCaption, FigureOccurrence, Heading
from .ooxml import NS, R, W, DocxPackage, paragraph_text
from .styles import StyleBook, paragraph_outline, paragraph_style

EMU_PER_CM = 360000
LO_RE = re.compile(
    r"^\s*(?:LO|L\.\s?O\.?|Learning\s+Outcomes?)\s*[-–—:.#]?\s*(\d{1,2})(?!\d)", re.IGNORECASE
)
CAPTION_TEXT_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Image|Picture)\s*(\d+(?:\s*[.\-–]\s*\d+)*)\s*[:.\-–—]?\s*(.*)$", re.IGNORECASE | re.S
)
AFG_BOOKMARK = "_AFG_"

DRAWING = W + "drawing"
PICT = W + "pict"


@dataclass
class FigureHandle:
    drawing: etree._Element
    run: etree._Element
    paragraph: etree._Element


def _int(v: Optional[str], default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def lo_number(text: str) -> Optional[int]:
    m = LO_RE.match(text or "")
    return int(m.group(1)) if m else None


def _in_fallback(el: etree._Element) -> bool:
    return any(a.tag == "{%s}Fallback" % NS["mc"] for a in el.iterancestors())


def _ancestor(el: etree._Element, tag: str, stop: Optional[etree._Element] = None) -> Optional[etree._Element]:
    for a in el.iterancestors():
        if a is stop:
            return None
        if a.tag == tag:
            return a
    return None


def _owning_paragraph(el: etree._Element) -> Optional[etree._Element]:
    return _ancestor(el, W + "p")


def _run_of(el: etree._Element) -> Optional[etree._Element]:
    return _ancestor(el, W + "r")


def _is_blank(p: etree._Element) -> bool:
    return not paragraph_text(p).strip() and p.find(".//w:drawing", NS) is None and p.find(".//w:pict", NS) is None


def _has_visual(p: etree._Element) -> bool:
    return p.find(".//w:drawing", NS) is not None or p.find(".//w:pict", NS) is not None


def detect_caption_paragraph(p: Optional[etree._Element], book: StyleBook, position: str) -> Optional[ExistingCaption]:
    if p is None or p.tag != W + "p" or _has_visual(p):
        return None
    text = paragraph_text(p).strip()
    signals: list[str] = []
    styled = book.is_caption(paragraph_style(p))
    if styled:
        signals.append("caption_style")
    instr = " ".join((t.text or "") for t in p.iter(W + "instrText"))
    instr += " " + " ".join(f.get(W + "instr", "") for f in p.iter(W + "fldSimple"))
    has_seq = bool(re.search(r"\bSEQ\s+(Figure|Fig|Image|Picture)\b", instr, re.I))
    if has_seq:
        signals.append("seq_field")
    ours = any((b.get(W + "name") or "").startswith(AFG_BOOKMARK) for b in p.iter(W + "bookmarkStart"))
    if ours:
        signals.append("autofigure_bookmark")
    m = CAPTION_TEXT_RE.match(text)
    if m and len(text) <= 300:
        signals.append("text_pattern")
    # Body sentences starting with "Figure" are only accepted with a structural
    # signal, or when short and directly adjacent below (directive 58).
    structural = styled or has_seq or ours
    if not structural:
        if position != "below" or not m or len(text) > 160 or text.endswith(("?",)):
            return None
        # "Figure 2 shows ..." is prose, not a caption
        if re.match(r"^\s*(Figure|Fig\.?)\s*[\d.\-]+\s+(shows|illustrates|depicts|presents|is|was|demonstrates)\b", text, re.I):
            return None
    if not text:
        return None
    number = re.sub(r"\s+", "", m.group(2)).replace("–", ".").replace("-", ".") if m else None
    desc = m.group(3).strip() if m else text
    return ExistingCaption(text=text, label_number=number, description=desc, position=position,
                           signals=signals, has_seq_field=has_seq, ours=ours)


def _next_para(p: etree._Element) -> Optional[etree._Element]:
    sib = p.getnext()
    skipped = 0
    while sib is not None:
        if sib.tag == W + "p":
            if _is_blank(sib) and skipped == 0:
                skipped += 1
                sib = sib.getnext()
                continue
            return sib
        if sib.tag in (W + "bookmarkStart", W + "bookmarkEnd", W + "proofErr"):
            sib = sib.getnext()
            continue
        return None
    return None


def _prev_para(p: etree._Element) -> Optional[etree._Element]:
    sib = p.getprevious()
    while sib is not None and sib.tag in (W + "bookmarkStart", W + "bookmarkEnd"):
        sib = sib.getprevious()
    return sib if sib is not None and sib.tag == W + "p" else None


def _nearby_text(p: etree._Element, limit: int = 600) -> str:
    texts = []
    prev = p.getprevious()
    n = 0
    while prev is not None and n < 2:
        if prev.tag == W + "p":
            t = paragraph_text(prev).strip()
            if t:
                texts.insert(0, t)
                n += 1
        prev = prev.getprevious()
    own = paragraph_text(p).strip()
    if own:
        texts.append(own)
    nxt = p.getnext()
    n = 0
    while nxt is not None and n < 1:
        if nxt.tag == W + "p":
            t = paragraph_text(nxt).strip()
            if t:
                texts.append(t)
                n += 1
        nxt = nxt.getnext()
    return " ".join(texts)[:limit]


def _preceding_text(p: etree._Element) -> str:
    prev = p.getprevious()
    hops = 0
    while prev is not None and hops < 3:
        if prev.tag == W + "p":
            if _has_visual(prev):
                return ""  # another figure sits in between: its sentence is not about this one
            t = paragraph_text(prev).strip()
            if t:
                return t[:400]
            hops += 1
        elif prev.tag == W + "tbl":
            return ""
        prev = prev.getprevious()
    return ""


def _guess_language(text: str) -> str:
    if not text:
        return "en"
    burmese = sum(1 for ch in text if "က" <= ch <= "႟")
    letters = sum(1 for ch in text if ch.isalpha())
    if letters and burmese / letters > 0.3:
        return "my"
    return "en"


def locate(pkg: DocxPackage, extra_heading1_ids: Optional[list[str]] = None,
           ) -> tuple[DocumentAnalysis, dict[str, FigureHandle], dict[str, etree._Element]]:
    part = pkg.main_part
    root = pkg.xml(part)
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("docx_no_body")
    book = StyleBook(pkg, extra_heading1_ids)
    rels = pkg.rels_for(part)

    headings: list[Heading] = []
    heading_els: dict[str, etree._Element] = {}
    figures: list[FigureOccurrence] = []
    handles: dict[str, FigureHandle] = {}
    unsupported: list[str] = []
    text_pattern_candidates: list[tuple[int, etree._Element, str]] = []
    list_counters: dict[str, int] = {}
    all_text: list[str] = []

    occurrence = 0
    for order, el in enumerate(body.iter()):
        tag = el.tag
        if tag == W + "p":
            if _ancestor(el, W + "txbxContent") is not None:
                continue
            txt = paragraph_text(el).strip()
            if txt and len(all_text) < 200:
                all_text.append(txt)
            sid = paragraph_style(el)
            level = book.heading_level(sid)
            ol = paragraph_outline(el)
            source = "style"
            if ol is not None:
                level, source = ol + 1, "outline"
            if level == 1 and txt:
                num_pr = el.find("w:pPr/w:numPr/w:numId", NS)
                list_number = None
                if num_pr is not None:
                    key = num_pr.get(W + "val") or ""
                    list_counters[key] = list_counters.get(key, 0) + 1
                    list_number = str(list_counters[key])
                hid = "h_" + hashlib.sha1(f"{order}:{txt}".encode()).hexdigest()[:10]
                headings.append(Heading(hid, order, txt[:200], 1, source, sid, lo_number(txt), list_number))
                heading_els[hid] = el
            elif level is None and txt and lo_number(txt) is not None and len(txt) <= 120:
                text_pattern_candidates.append((order, el, txt))
            continue
        if tag not in (DRAWING, PICT):
            continue
        if _in_fallback(el):
            continue  # mc:Fallback duplicates the mc:Choice rendering
        if tag == PICT and el.find(".//v:imagedata", NS) is None:
            continue  # VML shapes without a picture
        para = _owning_paragraph(el)
        run = _run_of(el)
        if para is None or run is None:
            unsupported.append("drawing_without_paragraph")
            continue
        in_textbox = _ancestor(el, W + "txbxContent") is not None
        # nested drawing inside a text box of another drawing: owning paragraph is inside the box
        kind, content_type = "vml", "picture"
        cx = cy = 0
        doc_pr_id = name = alt = wrap = None
        blip_rid = None
        if tag == DRAWING:
            inline = el.find("wp:inline", NS)
            anchor = el.find("wp:anchor", NS)
            container = inline if inline is not None else anchor
            kind = "inline" if inline is not None else "anchor"
            if container is None:
                unsupported.append("drawing_unknown_container")
                continue
            ext = container.find("wp:extent", NS)
            if ext is not None:
                cx, cy = _int(ext.get("cx")), _int(ext.get("cy"))
            dp = container.find("wp:docPr", NS)
            if dp is not None:
                doc_pr_id, name, alt = dp.get("id"), dp.get("name"), (dp.get("descr") or dp.get("title"))
            if anchor is not None:
                for wtag in ("wrapNone", "wrapSquare", "wrapTight", "wrapThrough", "wrapTopAndBottom"):
                    if anchor.find("wp:" + wtag, NS) is not None:
                        wrap = wtag
                        break
                if anchor.get("behindDoc") == "1" and wrap == "wrapNone":
                    wrap = "behindText"
            gd = container.find("a:graphic/a:graphicData", NS)
            uri = gd.get("uri", "") if gd is not None else ""
            blips = container.findall(".//a:blip", NS)
            if "chart" in uri:
                content_type = "chart"
            elif "diagram" in uri:
                content_type = "smartart"
            elif "wordprocessingGroup" in uri:
                content_type = "group"
            elif "wordprocessingShape" in uri and not blips:
                content_type = "shape"
            elif "wordprocessingCanvas" in uri:
                content_type = "canvas"
            if blips:
                blip_rid = blips[0].get(R + "embed") or blips[0].get(R + "link")
        else:
            imd = el.find(".//v:imagedata", NS)
            blip_rid = imd.get(R + "id") if imd is not None else None
            shape = el.find(".//v:shape", NS)
            style = shape.get("style", "") if shape is not None else ""
            mw = re.search(r"width:([\d.]+)pt", style)
            mh = re.search(r"height:([\d.]+)pt", style)
            cx = int(float(mw.group(1)) * 12700) if mw else 0
            cy = int(float(mh.group(1)) * 12700) if mh else 0
            if "position:absolute" in style:
                kind = "vml_floating"
        if content_type == "shape":
            continue  # plain shapes / text boxes are not figures (their inner pictures are visited separately)
        media_part = media_sha = media_ct = None
        if blip_rid and blip_rid in rels and not rels[blip_rid].external:
            media_part = rels[blip_rid].target
            if media_part in pkg.data:
                media_sha = hashlib.sha256(pkg.data[media_part]).hexdigest()
                media_ct = media_part.rsplit(".", 1)[-1].lower()
        elif blip_rid and blip_rid in rels and rels[blip_rid].external:
            unsupported.append("linked_external_image")
        imgs_in_para = len([d for d in para.iter(DRAWING, PICT) if not _in_fallback(d)])
        fid = "fig_" + hashlib.sha1(f"{occurrence}:{doc_pr_id}:{(media_sha or '')[:16]}:{order}".encode()).hexdigest()[:12]
        fig = FigureOccurrence(
            figure_id=fid, order=order, occurrence_index=occurrence, kind=kind, content_type=content_type,
            media_part=media_part, media_sha256=media_sha, media_content_type=media_ct,
            width_cm=round(cx / EMU_PER_CM, 2), height_cm=round(cy / EMU_PER_CM, 2),
            doc_pr_id=doc_pr_id, name=name, alt_text=(alt or None), in_table=_ancestor(el, W + "tc") is not None,
            in_textbox=in_textbox, paragraph_has_text=bool(paragraph_text(para).strip()),
            images_in_paragraph=imgs_in_para, wrap=wrap, nearby_text=_nearby_text(para),
            preceding_text=_preceding_text(para) if not in_textbox else "",
            own_text=paragraph_text(para).strip()[:300],
        )
        # existing caption detection (below, then above, then same paragraph)
        below = detect_caption_paragraph(_next_para(para), book, "below") if imgs_in_para == 1 or _is_last_visual(para, el) else None
        above = None
        prev_p = _prev_para(para)
        if below is None and prev_p is not None:
            before_prev = _prev_para(prev_p)
            # a caption directly under another picture belongs to that picture, not to this one
            if before_prev is None or not _has_visual(before_prev):
                above = detect_caption_paragraph(prev_p, book, "above")
        if above is not None and not ({"caption_style", "seq_field", "autofigure_bookmark"} & set(above.signals)):
            above = None
        fig.existing = below or above
        if fig.existing is None and fig.paragraph_has_text:
            same = paragraph_text(para).strip()
            m = CAPTION_TEXT_RE.match(same)
            if m and len(same) <= 200:
                fig.existing = ExistingCaption(same, re.sub(r"\s+", "", m.group(2)), m.group(3).strip(), "inline",
                                               ["same_paragraph_text"], False, False)
        figures.append(fig)
        handles[fid] = FigureHandle(el, run, para)
        occurrence += 1

    has_h1 = bool(headings)
    # No real Heading 1: fall back to LO-looking short paragraphs, flagged as text-pattern headings.
    if not has_h1 and text_pattern_candidates:
        for order, el, txt in text_pattern_candidates:
            hid = "h_" + hashlib.sha1(f"{order}:{txt}".encode()).hexdigest()[:10]
            headings.append(Heading(hid, order, txt[:200], 1, "text_pattern", paragraph_style(el), lo_number(txt)))
            heading_els[hid] = el
    has_lo = any(h.lo_number is not None for h in headings)
    if root.find(".//w:ins", NS) is not None or root.find(".//w:del", NS) is not None:
        unsupported.append("tracked_changes_present")
    analysis = DocumentAnalysis(headings=headings, figures=figures, page_count=None,
                                language_hint=_guess_language(" ".join(all_text)), unsupported=sorted(set(unsupported)),
                                has_heading1=has_h1, has_lo=has_lo)
    return analysis, handles, heading_els


def _is_last_visual(para: etree._Element, el: etree._Element) -> bool:
    visuals = [d for d in para.iter(DRAWING, PICT) if not _in_fallback(d)]
    return bool(visuals) and visuals[-1] is el
