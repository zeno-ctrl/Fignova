"""AutoFigure AI — the site's own captioning engine.

It is an orchestration layer, not a new foundation model:

1. **Document-context analyzer (built in, offline, no API key).** Authors
   usually introduce a figure in their own words ("The network topology used
   in the lab is shown next.", "A second chart compares latency.",
   "Performance results:"). The analyzer extracts that grounded phrase and turns
   it into a caption noun phrase. Author alt text is the second grounded
   source. Nothing is invented: if no grounded phrase exists, it falls back to a
   neutral type word and flags the figure for review.
2. **Vision models (optional).** When the operator configures Claude
   (ANTHROPIC_API_KEY) and/or ChatGPT (OPENAI_API_KEY), each image is also
   described by those models under the same grounding rules.
3. **Cross-checking.** With two vision models, their captions are compared;
   agreement raises confidence, disagreement flags the figure for review and
   keeps the other wording as a one-click "second opinion". The author's own
   intro sentence is used as an additional corroborating signal.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from .captioner import (AUTO_ALT_RE, PLACEHOLDER, CaptionContext, FallbackCaptioner, Suggestion, classify_image,
                        sanitize_caption)

log = logging.getLogger("autofigure.ai")

TYPE = r"(?:figure|image|screenshot|screen\s?shot|diagram|chart|graph|photo(?:graph)?|picture|table|map|plot|snapshot|illustration|output|topology|flowchart|layout|interface)"
VERB_ING = {
    "shows": "showing", "show": "showing", "illustrates": "illustrating", "depicts": "depicting",
    "presents": "presenting", "displays": "displaying", "compares": "comparing", "summarises": "summarising",
    "summarizes": "summarizing", "outlines": "outlining", "highlights": "highlighting", "demonstrates": "demonstrating",
    "describes": "describing", "lists": "listing", "represents": "representing", "captures": "capturing",
    "contains": "containing", "confirms": "confirming", "indicates": "indicating", "explains": "explaining",
}
VERBS = "|".join(VERB_ING)
STOP_LEAD = re.compile(r"^(?:for example|as follows|the following|following|below|see below|e\.g\.|i\.e\.|note|steps?)\b", re.I)
PRONOUN_LEAD = re.compile(r"^(?:i|we|you|they|he|she|it|this|that|these|those|my|our)\b", re.I)
ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)
COLON_REJECT = re.compile(r"\b(?:is|are|was|were|as follows|follows|following|again|same|another|below|above|here|next|"
                          r"see|shown|e\.g|example|note|step)\b", re.I)
TRAILING = re.compile(r"\s+(?:below|above|here|next|underneath|beneath)$", re.I)

PATTERNS = [
    # "A second chart compares latency." / "The following screenshot shows the router login page."
    ("type_verb", re.compile(
        rf"^(?:the\s+|this\s+|a\s+|an\s+)?(?:(?:following|below|next|above|first|second|third|fourth|final|last|new)\s+)*"
        rf"(?P<type>{TYPE})(?:\s+\d+(?:[.\-]\d+)*)?\s+(?:below\s+|above\s+|here\s+)?(?P<verb>{VERBS})\s+(?P<x>.+)$", re.I)),
    # "The network topology used in the lab is shown next."
    ("passive", re.compile(
        rf"^(?P<x>.{{4,120}}?)\s+(?:is|are|was|were|can be|has been|have been)\s+"
        rf"(?:shown|illustrated|depicted|presented|displayed|given|provided|captured|seen|visualised|visualized)\s+"
        rf"(?:below|next|here|underneath|in the (?:following|next) {TYPE})\.?$", re.I)),
    # "Below is a screenshot of the DHCP pool."
    ("below_is", re.compile(r"^(?:below|here|next)\s+(?:is|are)\s+(?P<x>.+)$", re.I)),
    # "Screenshot of the DHCP pool:" (lead-in phrase ending in a colon)
    ("colon", re.compile(r"^(?P<x>[^:]{3,90}):$")),
]


def _last_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return parts[-1].strip()


def _tidy(x: str, max_words: int) -> Optional[str]:
    x = x.strip().strip("\"'“”‘’").rstrip(" .;,")
    x = TRAILING.sub("", x)
    x = re.sub(r"\s+(?:as|which is|that is)\s+(?:shown|illustrated|seen).*$", "", x, flags=re.I)
    x = ARTICLE.sub("", x)
    if not x or PRONOUN_LEAD.match(x) or STOP_LEAD.match(x):
        return None
    words = x.split()
    if len(words) < 2 and not re.search(r"[A-Za-z]{4,}", x):
        return None
    if len(words) > max_words:
        return None  # a long clause is prose, not a caption; do not truncate meaning away
    return x[0].upper() + x[1:]


def context_caption(ctx: CaptionContext) -> tuple[Optional[str], str]:
    """Extract a caption phrase from the author's own introductory sentence."""
    if ctx.language not in ("en", "auto"):
        return None, ""
    max_words = {"short": 7, "standard": 14, "detailed": 20}.get(ctx.length, 14)
    sentence = _last_sentence(ctx.preceding_text)
    if not sentence or len(sentence) > 260:
        return None, ""
    for name, rx in PATTERNS:
        m = rx.match(sentence)
        if not m:
            continue
        if name == "type_verb":
            typ = re.sub(r"\s+", "", m.group("type")).lower()
            obj = _tidy(m.group("x"), max_words - 2)
            if obj and typ in ("figure", "image", "picture"):
                return obj, name  # "Figure 3 illustrates X" → "X" (no "Image showing…" filler)
            typ = typ.capitalize()
            if obj:
                return f"{typ} {VERB_ING[m.group('verb').lower()]} {obj[0].lower() + obj[1:] if not obj[:2].isupper() else obj}", name
        else:
            if name == "colon" and (len(m.group("x").split()) > 7 or COLON_REJECT.search(m.group("x"))):
                continue
            obj = _tidy(m.group("x"), max_words)
            if obj:
                return obj, name
    return None, ""


_STOP = set("a an the of and or to in on for with by from at as is are was were be this that these those using used "
            "showing shows image figure picture photo screenshot diagram chart graph".split())


def _content_tokens(s: str) -> set[str]:
    out = set()
    for w in re.findall(r"[A-Za-zက-႟]+", (s or "").lower()):
        if w in _STOP or len(w) < 3:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def agreement(a: str, b: str) -> float:
    ta, tb = _content_tokens(a), _content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


class AutoFigureAI:
    """Default captioner. Always available; stronger when vision models are configured."""

    name = "autofigure"

    def __init__(self, external: Optional[list] = None):
        self.external = external if external is not None else self._configured_models()

    @staticmethod
    def _configured_models() -> list:
        models = []
        disabled = {p.strip() for p in os.environ.get("AUTOFIGURE_AI_DISABLE", "").split(",") if p.strip()}
        if os.environ.get("ANTHROPIC_API_KEY") and "anthropic" not in disabled:
            try:
                from .captioner import AnthropicCaptioner
                models.append(AnthropicCaptioner(os.environ.get("AUTOFIGURE_AI_MODEL", "claude-opus-5-5")))
            except Exception as e:
                log.warning("claude_unavailable code=%s", type(e).__name__)
        if os.environ.get("OPENAI_API_KEY") and "openai" not in disabled:
            try:
                from .openai_provider import OpenAICaptioner
                models.append(OpenAICaptioner())
            except Exception as e:
                log.warning("openai_unavailable code=%s", type(e).__name__)
        return models

    @property
    def uses_external(self) -> bool:
        return bool(self.external)

    def describe_engines(self) -> str:
        names = [{"anthropic": "Claude", "openai": "ChatGPT"}.get(m.name, m.name) for m in self.external]
        if not names:
            return "AutoFigure AI · AutoFigure Vision + document context"
        return "AutoFigure AI · " + " + ".join(names) + (" consensus" if len(names) > 1 else "") + " + document context"

    def suggest(self, image_jpeg: Optional[bytes], ctx: CaptionContext) -> Suggestion:
        local, how = context_caption(ctx)
        results: list[tuple[str, Suggestion]] = []
        for m in self.external:
            try:
                results.append((m.name, m.suggest(image_jpeg, ctx)))
            except Exception as e:
                log.warning("vision_model_failed engine=%s figure=%s code=%s", m.name, ctx.figure_id, type(e).__name__)
        if not results:
            return self._local_only(image_jpeg, ctx, local, how, failed=bool(self.external))
        results.sort(key=lambda r: -RANK.get(r[1].confidence_band, 0))  # stable: Claude first on ties
        best_name, best = results[0]
        best.alternatives = []
        details = list(best.uncertain_details)
        if len(results) >= 2:
            other_name, other = results[1]
            score = agreement(best.caption, other.caption)
            if score >= 0.25 and RANK[best.confidence_band] >= 2 and RANK[other.confidence_band] >= 2:
                best.confidence_band = "high"
                best.needs_review = best.needs_review and bool([d for d in details if d in ("contained_link", "suspicious_text")])
                details.append(f"models_agree:{score:.2f}")
            else:
                best.needs_review = True
                if best.confidence_band == "high":
                    best.confidence_band = "medium"
                details.append(f"models_disagree:{score:.2f}")
                if other.caption and other.caption != best.caption:
                    best.alternatives.append(other.caption)
        if local:
            corroboration = agreement(best.caption, local)
            if corroboration >= 0.3:
                details.append("author_context_agrees")
                if best.confidence_band == "medium" and len(results) == 1:
                    best.confidence_band = "high"
            elif local not in best.alternatives:
                best.alternatives.append(local)
        best.uncertain_details = details[:8]
        best.engine = self.describe_engines()
        best.source = "ai"
        best.model_version = ",".join(r[1].model_version for r in results if r[1].model_version)
        return best

    def suggest_with_text(self, image_jpeg: Optional[bytes], ctx: CaptionContext, lines) -> Suggestion:
        """Offline suggestion using text lines read elsewhere (the visitor's browser)."""
        local, how = context_caption(ctx)
        return self._local_only(image_jpeg, ctx, local, how, False, lines)

    def _local_only(self, image_jpeg: Optional[bytes], ctx: CaptionContext, local: Optional[str], how: str,
                    failed: bool, lines=None) -> Suggestion:
        """Offline path: AutoFigure Vision (OCR + shape analysis) cross-checked with the author's own words."""
        from .local_vision import analyse
        lang = ctx.language if ctx.language in PLACEHOLDER else "en"
        extra = ["vision_models_unavailable"] if failed else []
        vis = analyse(image_jpeg, lines=lines)
        vcap = vis.caption if lang == "en" else None  # vision templates are English; Burmese keeps author/placeholder text
        engine = "AutoFigure AI · AutoFigure Vision + document context"
        alts: list[str] = []
        if local:
            text, flags = sanitize_caption(local)
            conf, details = "medium", [f"from_author_text:{how}"] + flags + extra
            if vcap and agreement(vcap, text) >= 0.2:
                conf = "high"
                details.append("vision_agrees_with_author")
            elif vcap and vis.confidence == "medium":
                alts.append(vcap)
            cat = vis.kind
        elif vcap:
            text, flags = sanitize_caption(vcap)
            conf, details, cat = vis.confidence, ["from_image_text_and_shapes"] + vis.evidence[:3] + flags + extra, vis.kind
        else:
            s = FallbackCaptioner().suggest(image_jpeg, ctx)
            s.uncertain_details += extra + (["photo_content_needs_vision_model"] if vis.kind == "photograph" else [])
            s.engine, s.alternatives = engine, []
            return s
        # review unless the image itself backs the caption (vision evidence, alone or agreeing with the author)
        needs = conf == "low" or (bool(local) and "vision_agrees_with_author" not in details)
        s = Suggestion(ctx.figure_id, text, conf, cat, lang, needs, details, "context")
        s.engine, s.alternatives = engine, alts
        return s
