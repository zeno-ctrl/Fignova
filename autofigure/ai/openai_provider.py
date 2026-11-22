"""Optional OpenAI (ChatGPT) vision captioner.

Selected with AUTOFIGURE_AI_PROVIDER=openai and OPENAI_API_KEY. Uses the same
grounding rules, JSON schema and sanitisation as the Claude provider, so the
rest of the pipeline cannot tell which model produced a suggestion except via
the recorded model version. Set AUTOFIGURE_OPENAI_MODEL to a current
vision-capable model available to your account.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Optional

from .captioner import (CATEGORIES, LENGTH_HINT, PLACEHOLDER, SCHEMA, SYSTEM_PROMPT, TONE_HINT, VARIANT_HINT,
                        CaptionContext, Suggestion, _xml_escape, sanitize_caption)


class OpenAICaptioner:
    name = "openai"

    def __init__(self, model: Optional[str] = None, timeout: float = 60.0):
        from openai import OpenAI  # lazy import: only needed when this provider is selected

        self.client = OpenAI(timeout=timeout, max_retries=2)
        self.model = model or os.environ.get("AUTOFIGURE_OPENAI_MODEL", "gpt-4o")

    def suggest(self, image_jpeg: Optional[bytes], ctx: CaptionContext) -> Suggestion:
        if not image_jpeg:
            raise ValueError("no_image_bytes")
        lang_name = "Burmese (Myanmar script)" if ctx.language == "my" else "English"
        instructions = (
            f"Caption language: {lang_name}. Length: {LENGTH_HINT.get(ctx.length, '4–12 words')}. "
            f"Style: {TONE_HINT.get(ctx.tone, TONE_HINT['academic'])}. " + VARIANT_HINT.get(ctx.variant or "", "")
        )
        reference = (
            "<document_reference>\n"
            f"<section_heading>{_xml_escape(ctx.heading)[:200]}</section_heading>\n"
            f"<nearby_text>{_xml_escape(ctx.nearby_text)[:600]}</nearby_text>\n"
            f"<author_alt_text>{_xml_escape(ctx.alt_text or '')[:300]}</author_alt_text>\n"
            "</document_reference>"
        )
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_jpeg).decode()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": reference},
                    {"type": "text", "text": instructions},
                ]},
            ],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "figure_caption", "strict": True, "schema": SCHEMA}},
        )
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise RuntimeError("model_refused")
        data = json.loads(choice.message.content or "{}")
        caption, flags = sanitize_caption(data.get("suggestedCaption", ""))
        conf = data.get("confidenceBand") if data.get("confidenceBand") in ("high", "medium", "low") else "low"
        cat = data.get("detectedCategory") if data.get("detectedCategory") in CATEGORIES else "other"
        needs = bool(data.get("needsHumanReview")) or conf == "low" or bool(flags) or not caption
        return Suggestion(ctx.figure_id, caption or PLACEHOLDER["en"][cat], conf, cat,
                          data.get("language", ctx.language), needs,
                          [str(x)[:120] for x in (data.get("uncertainDetails") or [])][:5] + flags,
                          "ai", getattr(resp, "model", self.model))
