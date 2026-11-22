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


def map_pages(original: bytes, figures: list[FigureOccurrence], timeout: int = 180) -> PageMapResult:
    warnings: list[str] = []
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
    """Without a renderer only explicit breaks are known; nothing is marked confident."""
    pkg = DocxPackage.from_bytes(original)
    root = pkg.xml(pkg.main_part)
    page = 1
    breaks_at: list[int] = []
    for order, el in enumerate(root.iter()):
        if (el.tag == W + "br" and el.get(W + "type") == "page") or el.tag == W + "lastRenderedPageBreak" \
                or el.tag == W + "pageBreakBefore" or (el.tag == W + "sectPr" and el.getparent().tag == W + "pPr"):
            breaks_at.append(order)
    for fig in figures:
        before = sum(1 for b in breaks_at if b < fig.order)
        fig.page = page + before
        fig.page_confident = False
        fig.page_evidence = "Estimated from explicit page breaks only (renderer unavailable)"
