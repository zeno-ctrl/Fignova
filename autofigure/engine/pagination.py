"""Map every figure occurrence to the page it is displayed on in the ORIGINAL
document (directives 10 and 52).

Method: a probe copy of the original receives a tiny invisible marker run
(1 pt, white) immediately before each drawing run. The probe is rendered with
the same isolated renderer; the page on which each marker's text appears is
the page that holds the anchor point of that figure (Word and LibreOffice both
place floating objects on their anchor paragraph's page). The probe's page
count is compared with the untouched original render, and per-page image
counts from the PDF are used as a second signal. Anything that cannot be
verified is reported as uncertain and is *not* captioned automatically.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from lxml import etree

from .locator import locate
from .model import FigureOccurrence
from .ooxml import W, DocxPackage
from .render import RenderError, docx_to_pdf, pdf_images_per_page, pdf_layout

MARKER_RE = re.compile(r"QZX(\d{5})QZX")


def _marker_run(n: int) -> etree._Element:
    r = etree.Element(W + "r")
    rpr = etree.SubElement(r, W + "rPr")
    etree.SubElement(rpr, W + "color").set(W + "val", "FFFFFF")
    etree.SubElement(rpr, W + "sz").set(W + "val", "2")
    etree.SubElement(rpr, W + "szCs").set(W + "val", "2")
    t = etree.SubElement(r, W + "t")
    t.text = f"QZX{n:05d}QZX"
    return r


def build_probe(original: bytes) -> bytes:
    pkg = DocxPackage.from_bytes(original)
    analysis, handles, _ = locate(pkg)
    for fig in analysis.figures:
        h = handles[fig.figure_id]
        h.run.addprevious(_marker_run(fig.occurrence_index))
    pkg.mark_dirty(pkg.main_part)
    return pkg.to_bytes()


@dataclass
class PageMapResult:
    page_count: Optional[int]
    original_pdf: Optional[bytes]
    method: str
    warnings: list[str]


def map_pages(original: bytes, figures: list[FigureOccurrence], timeout: int = 180, render: bool = True) -> PageMapResult:
    warnings: list[str] = []
    if not render:  # serverless host: no renderer — use Word's saved layout, page breaks and a layout estimate
        _fallback_explicit_breaks(original, figures)
        return PageMapResult(None, None, "word_layout_estimate", [])
    try:
        original_pdf = docx_to_pdf(original, timeout)
        probe_pdf = docx_to_pdf(build_probe(original), timeout)
    except RenderError as e:
        _fallback_explicit_breaks(original, figures)
        return PageMapResult(None, None, "explicit_breaks_only", [f"render_unavailable:{e}"])
    orig_pages = pdf_layout(original_pdf)
    probe_pages = pdf_layout(probe_pdf)
    if len(orig_pages) != len(probe_pages):
        warnings.append("probe_page_count_differs")
    found: dict[int, int] = {}
    for page in probe_pages:
        for text, *_ in page.lines:
            for m in MARKER_RE.finditer(text.replace(" ", "")):
                found.setdefault(int(m.group(1)), page.page)
    img_counts = pdf_images_per_page(original_pdf)
    for fig in figures:
        pg = found.get(fig.occurrence_index)
        if pg is None:
            fig.page, fig.page_confident = None, False
            fig.page_evidence = "Marker not found in rendered probe"
            continue
        fig.page = pg
        fig.page_confident = "probe_page_count_differs" not in warnings
        fig.page_evidence = f"Anchor rendered on page {pg} of the original ({len(orig_pages)} pages)"
        if fig.kind in ("anchor", "vml_floating"):
            fig.warnings.append("floating_image")
    # Cross-check: a page cannot hold fewer raster images than body pictures we placed on it.
    per_page: dict[int, int] = {}
    for fig in figures:
        if fig.page and fig.content_type == "picture" and fig.media_part:
            per_page[fig.page] = per_page.get(fig.page, 0) + 1
    for page, n in per_page.items():
        if img_counts and img_counts.get(page, 0) < n:
            warnings.append(f"image_count_mismatch_page_{page}")
            for fig in figures:
                if fig.page == page and page <= 2:
                    fig.page_confident = False
                    fig.page_evidence += "; image count check disagreed"
    return PageMapResult(len(orig_pages), original_pdf, "rendered_probe", warnings)


def _fallback_explicit_breaks(original: bytes, figures: list[FigureOccurrence]) -> None:
    """Page assignment without a renderer (serverless hosts).

    Evidence, strongest first:
    1. ``w:lastRenderedPageBreak`` — where Microsoft Word itself broke pages when the file was last saved.
    2. Explicit breaks (page break, page-break-before, next-page section break): anything after one is
       certainly not on page 1.
    3. A layout estimate from paragraph lengths, image heights and the page size/margins.
    Only page 1 matters for the exclusion rule; borderline cases are left for the user to confirm.
    """
    from .locator import _ancestor
    from .ooxml import NS, paragraph_text
    pkg = DocxPackage.from_bytes(original)
    root = pkg.xml(pkg.main_part)
    body = root.find("w:body", NS)
    lrpb: list[int] = []
    explicit: list[int] = []
    heights: list[tuple[int, float]] = []  # (order, estimated height in cm) per block
    first_heading: Optional[int] = None
    for order, el in enumerate(body.iter()):
        tag = el.tag
        if tag == W + "lastRenderedPageBreak":
            lrpb.append(order)
        elif tag == W + "br" and el.get(W + "type") == "page":
            explicit.append(order)
        elif tag == W + "pageBreakBefore" and el.get(W + "val") not in ("0", "false"):
            explicit.append(order)
        elif tag == W + "sectPr" and el.getparent() is not None and el.getparent().tag == W + "pPr":
            t = el.find("w:type", NS)
            if t is None or t.get(W + "val") != "continuous":
                explicit.append(order + 1)  # the break follows this paragraph
        elif tag == W + "p" and _ancestor(el, W + "txbxContent") is None:
            txt = paragraph_text(el).strip()
            style = el.find("w:pPr/w:pStyle", NS)
            is_heading = style is not None and (style.get(W + "val") or "").lower().startswith(("heading", "title"))
            if is_heading and first_heading is None and txt:
                first_heading = order
            lines = max(1, -(-len(txt) // 90)) if txt else 1
            heights.append((order, lines * 0.55 + (0.9 if is_heading else 0.25)))
    # usable page height from the last section properties
    usable = 24.7
    sect = body.find("w:sectPr", NS)
    if sect is not None:
        pg, mar = sect.find("w:pgSz", NS), sect.find("w:pgMar", NS)
        try:
            usable = (int(pg.get(W + "h")) - int(mar.get(W + "top")) - int(mar.get(W + "bottom"))) / 567.0
        except (AttributeError, TypeError, ValueError):
            pass
    fig_h = {f.order: (f.height_cm or 5.0) + 0.4 for f in figures}

    def top_of(order: int) -> float:
        y = sum(h for o, h in heights if o < order)
        return y + sum(h for o, h in fig_h.items() if o < order)

    for fig in figures:
        if fig.kind in ("anchor", "vml_floating"):
            fig.warnings.append("floating_image")
        if lrpb:
            fig.page = 1 + sum(1 for b in lrpb if b < fig.order)
            fig.page_confident = True
            fig.page_evidence = f"Page {fig.page} according to Word's saved page layout"
            continue
        before = sum(1 for b in explicit if b < fig.order)
        if before:
            fig.page = 1 + before
            fig.page_confident = True
            fig.page_evidence = f"After {before} explicit page break(s) — not on page 1"
            continue
        top = top_of(fig.order)
        bottom = top + fig_h[fig.order]
        if explicit and (first_heading is None or first_heading > fig.order):
            fig.page, fig.page_confident = 1, True
            fig.page_evidence = "Before the first page break and before any heading — cover page"
        elif top > usable * 1.1:
            fig.page, fig.page_confident = 2 + int((top - usable) // usable), True
            fig.page_evidence = f"Estimated from layout (~{top:.0f} cm down the document, page height {usable:.0f} cm)"
        elif bottom < usable * 0.85 and (first_heading is None or first_heading > fig.order):
            fig.page, fig.page_confident = 1, True
            fig.page_evidence = "Estimated from layout: on the first page"
        else:
            fig.page, fig.page_confident = (1 if top < usable else 2), False
            fig.page_evidence = "Estimated from layout — near the first page; please confirm"
