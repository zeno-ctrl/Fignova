"""User-adjustable caption settings with server-side validation."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FigureOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter: Optional[int] = Field(default=None, ge=0, le=99)  # manual chapter assignment
    include: Optional[bool] = None  # force include/exclude (decorative, unassigned, unsupported)
    page_confirmed_not_first: Optional[bool] = None  # user confirms an uncertain page assignment
    rewrite: Optional[bool] = None  # user accepted a correction of the existing caption text


class CaptionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = Field(default="Figure", max_length=24)
    separator: Literal[" ", ": ", ". ", " – ", " — "] = " "
    number_style: Literal["chapter", "chapter_dash", "document"] = "chapter"
    chapter_source: Literal["auto", "lo", "heading1", "document"] = "auto"
    lo_number_mode: Literal["visible", "ordinal"] = "visible"
    unassigned_policy: Literal["review", "first_chapter"] = "review"
    existing_mode: Literal["renumber", "skip", "replace"] = "renumber"
    include_decorative: bool = False
    position: Literal["below", "above"] = "below"
    align: Literal["center", "left", "right"] = "center"
    font_size: float = Field(default=10, ge=7, le=16)
    italic: bool = True
    color: Literal["neutral", "style"] = "neutral"  # neutral = dark grey, not bold; style = keep the document Caption style look
    field_mode: Literal["seq", "static"] = "seq"
    keep_with_image: bool = True
    update_fields_on_open: bool = False
    tone: Literal["academic", "professional", "technical", "plain"] = "academic"
    length: Literal["short", "standard", "detailed"] = "standard"
    language: Literal["auto", "en", "my"] = "auto"
    end_punctuation: bool = False
    figure_overrides: dict[str, FigureOverride] = Field(default_factory=dict)
    heading_overrides: dict[str, Literal["boundary", "ignore"]] = Field(default_factory=dict)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or any(ch in v for ch in "<>{}\\"):
            raise ValueError("invalid prefix")
        return v

    @field_validator("figure_overrides", "heading_overrides")
    @classmethod
    def _limit(cls, v):
        if len(v) > 5000:
            raise ValueError("too many overrides")
        return v
