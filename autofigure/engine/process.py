"""Storage-agnostic engine entry points used by the worker and the tests."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from .locator import locate
from .model import DocumentAnalysis, ExistingCaption, FigureOccurrence
from .numbering import check_invariants, plan_numbering
from .ooxml import DocxPackage
from .pagination import map_pages
from .render import docx_to_pdf, pdf_layout, rasterize
from .settings import CaptionSettings
from .verify import verify_output
from .writer import compose_caption, write_captions


@dataclass
class AnalysisResult:
    analysis: DocumentAnalysis
    original_pdf: Optional[bytes]
    page_method: str
    warnings: list[str]


def analyze(original: bytes, timeout: int = 180, render: bool = True) -> AnalysisResult:
    pkg = DocxPackage.from_bytes(original)
    analysis, _, _ = locate(pkg)
    pm = map_pages(original, analysis.figures, timeout, render)
    analysis.page_count = pm.page_count
    return AnalysisResult(analysis, pm.original_pdf, pm.method, pm.warnings + analysis.unsupported)


def figure_from_dict(d: dict) -> FigureOccurrence:
    d = dict(d)
    ex = d.pop("existing", None)
    f = FigureOccurrence(**d)
    f.existing = ExistingCaption(**ex) if ex else None
    return f


def plan(figures: list[FigureOccurrence], headings, settings: CaptionSettings) -> dict:
    _, summary = plan_numbering(figures, headings, settings)
    summary["invariant_problems"] = check_invariants(figures)
    return summary


def media_thumbnail(original: bytes, media_part: Optional[str], max_px: int = 360) -> Optional[bytes]:
    if not media_part:
        return None
    pkg = DocxPackage.from_bytes(original)
    raw = pkg.data.get(media_part)
    if raw is None:
        return None
    try:
        Image.MAX_IMAGE_PIXELS = 80_000_000
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        b = io.BytesIO()
        im.save(b, "PNG", optimize=True)
        return b.getvalue()
    except Exception:
        return None


def media_for_ai(original: bytes, media_part: Optional[str], max_px: int = 1568) -> Optional[bytes]:
    if not media_part:
        return None
    raw = DocxPackage.from_bytes(original).data.get(media_part)
    if raw is None:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=88)
        return b.getvalue()
    except Exception:
        return None


@dataclass
class Revision:
    docx: bytes
    manifest: list[dict]
    verification: dict


def build_revision(original: bytes, figures: list[FigureOccurrence], settings: CaptionSettings) -> Revision:
    out, manifest = write_captions(original, figures, settings)
    ver = verify_output(original, out, manifest)
    return Revision(out, manifest, ver)


@dataclass
class Preview:
    pdf: bytes
    pages: list[bytes]
    thumbs: list[bytes]
    manifest: dict


def render_preview(docx: bytes, labels: dict[str, str], prefix: str, timeout: int = 180) -> Preview:
    """Render an exact revision and locate each caption on the rendered pages."""
    pdf = docx_to_pdf(docx, timeout)
    layout = pdf_layout(pdf)
    pages, thumbs = rasterize(pdf)
    highlights: dict[str, dict] = {}
    want = {f"{prefix} {lab}": fid for fid, lab in labels.items()}
    for pg in layout:
        for text, x0, y0, x1, y1 in pg.lines:
            t = " ".join(text.split())
            for key, fid in want.items():
                if fid in highlights:
                    continue
                if t == key or t.startswith(key + " ") or t.startswith(key + ":") or t.startswith(key + ".") \
                        or t.startswith(key + " –") or t.startswith(key + " —"):
                    highlights[fid] = {"page": pg.page, "x": x0 / pg.width, "y": y0 / pg.height,
                                       "w": (x1 - x0) / pg.width, "h": (y1 - y0) / pg.height}
    manifest = {
        "page_count": len(layout),
        "pages": [{"n": p.page, "w": p.width, "h": p.height} for p in layout],
        "highlights": highlights,
        "texts": [" ".join(" ".join(t[0].split()) for t in p.lines)[:20000] for p in layout],
    }
    return Preview(pdf, pages, thumbs, manifest)


__all__ = ["analyze", "plan", "build_revision", "render_preview", "compose_caption", "figure_from_dict",
           "media_thumbnail", "media_for_ai"]
