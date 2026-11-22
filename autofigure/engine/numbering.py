"""Caption numbering as a pure, deterministic function (directive 51).

Input: ordered figure occurrences (with page + existing caption info), the
detected headings and the settings. Output: each figure's chapter, index,
label and planned action. No I/O happens here so the rules are unit-testable
with simple lists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .model import FigureOccurrence, Heading
from .settings import CaptionSettings

DECORATIVE_MIN_CM = 1.5  # either side below this => decorative icon/logo by default

ACTION_STATUSES = {"insert", "renumber", "replace", "preserve"}
STATUS_LABELS = {
    "insert": "Caption inserted",
    "renumber": "Existing caption renumbered",
    "replace": "Existing caption replaced",
    "preserve": "Existing caption kept",
    "skip_existing": "Already captioned (skipped)",
    "excluded_first_page": "On original page 1 (protected)",
    "excluded_page_uncertain": "Page uncertain — confirm",
    "excluded_decorative": "Decorative (small image)",
    "excluded_unassigned": "Before first section — assign",
    "excluded_unsupported": "Unsupported placement",
    "excluded_manual": "Excluded by you",
}


@dataclass
class Chapter:
    key: str
    number: Optional[int]
    title: str
    heading_id: Optional[str]
    order: int


def resolve_chapters(headings: list[Heading], settings: CaptionSettings) -> tuple[list[Chapter], str, list[str]]:
    """Return chapter boundaries, the mode used, and warnings."""
    warnings: list[str] = []
    hs = [h for h in headings if settings.heading_overrides.get(h.heading_id) != "ignore"]
    forced = {hid for hid, v in settings.heading_overrides.items() if v == "boundary"}
    lo_heads = [h for h in hs if h.lo_number is not None or h.heading_id in forced]
    mode = settings.chapter_source
    if mode == "auto":
        mode = "lo" if any(h.lo_number is not None for h in hs) else ("heading1" if hs else "document")
    if mode == "lo" and not lo_heads:
        warnings.append("no_lo_headings_found")
        mode = "heading1" if hs else "document"
    chapters: list[Chapter] = []
    if mode == "lo":
        seen_numbers: dict[int, Chapter] = {}
        for h in lo_heads:
            if settings.lo_number_mode == "visible" and h.lo_number is not None:
                num = h.lo_number
            elif settings.lo_number_mode == "visible":
                num = max(seen_numbers, default=0) + 1  # manually forced boundary without an LO number
            else:
                num = len(seen_numbers) + 1
            if num in seen_numbers:
                # "LO1 part A" + "LO1 part B": continue the same chapter's counter
                chapters.append(Chapter(seen_numbers[num].key, num, h.text, h.heading_id, h.order))
                continue
            ch = Chapter(f"lo{num}", num, h.text, h.heading_id, h.order)
            seen_numbers[num] = ch
            chapters.append(ch)
        if any(h.source == "text_pattern" for h in lo_heads):
            warnings.append("headings_from_text_pattern")
        nums = [c.number for c in chapters]
        if nums != sorted(nums):
            warnings.append("lo_numbers_out_of_order")
    elif mode == "heading1":
        for i, h in enumerate(hs, start=1):
            chapters.append(Chapter(f"h{i}", i, h.text, h.heading_id, h.order))
        if any(h.source == "text_pattern" for h in hs):
            warnings.append("headings_from_text_pattern")
    else:
        warnings.append("document_wide_numbering")
    return chapters, mode, warnings


def _chapter_for(order: int, chapters: list[Chapter]) -> Optional[Chapter]:
    cur = None
    for ch in chapters:
        if ch.order < order:
            cur = ch
        else:
            break
    return cur


def format_label(settings: CaptionSettings, chapter_number: Optional[int], index: int) -> str:
    if settings.number_style == "document" or chapter_number is None:
        return str(index)
    sep = "-" if settings.number_style == "chapter_dash" else "."
    return f"{chapter_number}{sep}{index}"


def plan_numbering(figures: list[FigureOccurrence], headings: list[Heading], settings: CaptionSettings,
                   ) -> tuple[list[FigureOccurrence], dict]:
    chapters, mode, warnings = resolve_chapters(headings, settings)
    document_wide = mode == "document" or settings.number_style == "document"
    counters: dict[str, int] = {}
    first_chapter = chapters[0] if chapters else None
    for fig in sorted(figures, key=lambda f: f.order):
        ov = settings.figure_overrides.get(fig.figure_id)
        fig.warnings = [w for w in fig.warnings if not w.startswith("plan:")]
        fig.label = fig.figure_index = fig.chapter_number = fig.chapter_key = fig.chapter_title = None
        fig.eligible = False
        # 1. page-one protection (decided on the ORIGINAL render, never re-evaluated after mutation)
        if fig.page == 1:
            fig.status, fig.reason = "excluded_first_page", "Shown on page 1 of the original document"
            continue
        if fig.page is None or not fig.page_confident:
            if not (ov and ov.page_confirmed_not_first):
                fig.status = "excluded_page_uncertain"
                fig.reason = "Page could not be verified from the rendered original; confirm it is not on page 1"
                continue
        # 2. unsupported placements
        if fig.in_textbox or fig.content_type in ("smartart", "canvas"):
            if not (ov and ov.include):
                fig.status = "excluded_unsupported"
                fig.reason = "Inside a text box / SmartArt / drawing canvas — not edited automatically"
                continue
        # 3. manual exclusion
        if ov and ov.include is False:
            fig.status, fig.reason = "excluded_manual", "Excluded by reviewer"
            continue
        # 4. decorative
        small = min(fig.width_cm or 99, fig.height_cm or 99) < DECORATIVE_MIN_CM
        if small and not settings.include_decorative and not (ov and ov.include):
            fig.status, fig.reason = "excluded_decorative", "Small image treated as decorative (icon/logo)"
            continue
        # 5. chapter assignment
        if ov and ov.chapter is not None:
            num = ov.chapter
            ch = next((c for c in chapters if c.number == num), None)
            key, title = (ch.key, ch.title) if ch else (f"manual{num}", f"Section {num}")
        elif document_wide:
            num, key, title = None, "doc", "Whole document"
        else:
            ch = _chapter_for(fig.order, chapters)
            if ch is None:
                if settings.unassigned_policy == "first_chapter" and first_chapter:
                    ch = first_chapter
                    fig.warnings.append("plan:assigned_to_first_chapter")
                else:
                    fig.status = "excluded_unassigned"
                    fig.reason = "Appears before the first section heading — assign a section to caption it"
                    continue
            num, key, title = ch.number, ch.key, ch.title
        counters[key] = counters.get(key, 0) + 1
        idx = counters[key]
        fig.eligible = True
        fig.chapter_key, fig.chapter_number, fig.chapter_title, fig.figure_index = key, num, title, idx
        fig.label = format_label(settings, None if document_wide else num, idx)
        # 6. existing caption policy
        ex = fig.existing
        if ex is None:
            fig.status, fig.reason = "insert", "New caption"
        elif ov and ov.rewrite and ex.position != "inline":
            fig.status, fig.reason = "replace", "Existing caption corrected"
        elif ex.position == "inline":
            fig.status, fig.reason = "skip_existing", "Caption text shares the image paragraph — kept unchanged"
            if (ex.label_number or "") != fig.label:
                fig.warnings.append("plan:inline_caption_number_mismatch")
        elif settings.existing_mode == "skip":
            fig.status, fig.reason = "skip_existing", "Already captioned"
            if (ex.label_number or "") != fig.label:
                fig.warnings.append("plan:existing_number_mismatch")
        elif settings.existing_mode == "replace":
            fig.status, fig.reason = "replace", "Existing caption replaced with a new description"
        else:
            if (ex.label_number or "") == fig.label and _prefix_matches(ex.text, settings.prefix):
                fig.status, fig.reason = "preserve", "Existing caption already correct"
            else:
                fig.status, fig.reason = "renumber", f"Renumbered from {ex.label_number or 'unnumbered'}"
    summary = summarize(figures, chapters, mode, warnings)
    return figures, summary


def _prefix_matches(text: str, prefix: str) -> bool:
    return text.strip().lower().startswith(prefix.strip().lower())


def summarize(figures: list[FigureOccurrence], chapters: list[Chapter], mode: str, warnings: list[str]) -> dict:
    counts: dict[str, int] = {}
    for f in figures:
        counts[f.status] = counts.get(f.status, 0) + 1
    return {
        "mode": mode,
        "warnings": warnings,
        "chapters": [{"key": c.key, "number": c.number, "title": c.title, "heading_id": c.heading_id} for c in chapters],
        "counts": counts,
        "total_images": len(figures),
        "captioned": sum(counts.get(s, 0) for s in ACTION_STATUSES) + counts.get("skip_existing", 0),
    }


def check_invariants(figures: list[FigureOccurrence]) -> list[str]:
    """Every chapter sequence must be 1..n with no gaps or duplicates."""
    problems = []
    by_key: dict[str, list[int]] = {}
    for f in figures:
        if f.eligible and f.figure_index is not None:
            by_key.setdefault(f.chapter_key or "", []).append(f.figure_index)
    for key, idx in by_key.items():
        if sorted(idx) != list(range(1, len(idx) + 1)):
            problems.append(f"sequence_broken:{key}")
    labels = [f.label for f in figures if f.eligible]
    if len(labels) != len(set(labels)):
        # same LO number on two chapters with continue semantics never duplicates; real dupes are bugs
        problems.append("duplicate_labels")
    return problems
