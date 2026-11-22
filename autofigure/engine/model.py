"""Canonical document model shared by every engine stage (directive 49)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Heading:
    heading_id: str
    order: int  # document-order key shared with figures
    text: str
    level: int
    source: str  # "style" | "outline" | "text_pattern"
    style_id: Optional[str]
    lo_number: Optional[int]
    list_number: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExistingCaption:
    text: str
    label_number: Optional[str]  # "1.2" etc as written
    description: str
    position: str  # "below" | "above" | "inline"
    signals: list[str]
    has_seq_field: bool
    ours: bool  # carries an AutoFigure bookmark


@dataclass
class FigureOccurrence:
    figure_id: str
    order: int
    occurrence_index: int  # 0-based index among all visual objects in reading order
    kind: str  # inline | anchor | vml
    content_type: str  # picture | chart | group | smartart | shape
    media_part: Optional[str]
    media_sha256: Optional[str]
    media_content_type: Optional[str]
    width_cm: float
    height_cm: float
    doc_pr_id: Optional[str]
    name: Optional[str]
    alt_text: Optional[str]
    in_table: bool
    in_textbox: bool
    paragraph_has_text: bool
    images_in_paragraph: int
    wrap: Optional[str]
    nearby_text: str
    preceding_text: str = ""  # the paragraph that introduces the figure (author's own words)
    own_text: str = ""  # text sharing the image paragraph
    # filled by later stages
    page: Optional[int] = None
    page_evidence: str = ""
    page_confident: bool = False
    chapter_key: Optional[str] = None
    chapter_number: Optional[int] = None
    chapter_title: Optional[str] = None
    figure_index: Optional[int] = None
    label: Optional[str] = None
    eligible: bool = False
    status: str = "pending"  # see numbering.STATUSES
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    existing: Optional[ExistingCaption] = None
    caption_text: str = ""
    caption_source: str = ""  # ai | fallback | existing | manual
    confidence_band: str = ""
    needs_review: bool = False
    category: str = ""
    alternatives: list[str] = field(default_factory=list)  # second-opinion captions from other engines
    engine: str = ""  # which AutoFigure AI engine produced the suggestion
    mistakes: list[dict] = field(default_factory=list)  # problems found in the author's existing caption
    correction: str = ""  # proposed corrected description for an existing caption

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class DocumentAnalysis:
    headings: list[Heading]
    figures: list[FigureOccurrence]
    page_count: Optional[int]
    language_hint: str
    unsupported: list[str]
    has_heading1: bool
    has_lo: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "headings": [h.to_dict() for h in self.headings],
            "figures": [f.to_dict() for f in self.figures],
            "page_count": self.page_count,
            "language_hint": self.language_hint,
            "unsupported": self.unsupported,
            "has_heading1": self.has_heading1,
            "has_lo": self.has_lo,
        }
