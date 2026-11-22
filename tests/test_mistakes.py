"""'Mistakes found in your file': wrong descriptions/numbers detected and corrected with one click."""
import io

from docx import Document

from autofigure.ai.autofigure_ai import AutoFigureAI
from autofigure.ai.captioner import Suggestion
from tests.conftest import make_client, run_worker
from tests.fixtures import wrong_caption_doc

DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class SeeingModel:
    """Stands in for Claude/ChatGPT in tests (no network)."""
    name = "anthropic"


def test_wrong_existing_captions_are_found_and_corrected(app, monkeypatch):
    seen = {"n": 0}

    class Model(SeeingModel):
        def suggest(self, img, ctx):
            seen["n"] += 1
            # first image in the document is the cat, second the network diagram
            text = "A cat staring" if ctx.existing_hint == "dog" else "Network topology overview"
            return Suggestion(ctx.figure_id, text, "high", "photograph", "en", False, [], "ai", "fake")

    import autofigure.pipeline as pl
    from autofigure.ai import captioner as cap
    orig_ctx = pl._context

    def ctx_with_hint(f, settings, lang, variant=None):
        c = orig_ctx(f, settings, lang, variant)
        c.existing_hint = f.existing.description if f.existing else ""
        return c

    monkeypatch.setattr(pl, "_context", ctx_with_hint)
    monkeypatch.setattr(pl, "get_captioner", lambda p, m: AutoFigureAI(external=[Model()]))

    c = make_client(app)
    r = c.post("/api/uploads", files={"file": ("pets.docx", wrong_caption_doc(), DOCX_CT)}, data={"consent": "yes", "settings": "{}"})
    job = r.json()["job_id"]
    run_worker()
    figs = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    cat, net = figs
    kinds = {m["kind"] for m in cat["mistakes"]}
    assert kinds == {"description"} and cat["correction"] == "A cat staring"
    assert {m["kind"] for m in net["mistakes"]} == {"number"}  # 4.9 → 1.2 (topology description matches)
    assert net["status"] == "renumber"
    # before correcting, the dog caption is still in the output
    before = Document(io.BytesIO(c.get(f"/api/jobs/{job}/download").content))
    assert "Figure 1.1 dog" in [p.text for p in before.paragraphs]
    d = c.post(f"/api/jobs/{job}/correct", json={}).json()
    assert d["corrected"] == 2 and d["regenerating"]
    run_worker()
    after = [p.text for p in Document(io.BytesIO(c.get(f"/api/jobs/{job}/download").content)).paragraphs if p.style.name == "Caption"]
    assert after == ["Figure 1.1 A cat staring", "Figure 1.2 Network topology"]
    figs = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    assert all(not f["mistakes"] for f in figs)
    man = c.get(f"/api/jobs/{job}/preview").json()
    assert any("Figure 1.1 A cat staring" in t for t in man["texts"])


def test_keep_mine_dismisses(app, monkeypatch):
    import autofigure.pipeline as pl

    class Model:
        name = "anthropic"

        def suggest(self, img, ctx):
            return Suggestion(ctx.figure_id, "Totally different thing", "high", "other", "en", False, [], "ai", "fake")

    monkeypatch.setattr(pl, "get_captioner", lambda p, m: AutoFigureAI(external=[Model()]))
    c = make_client(app)
    job = c.post("/api/uploads", files={"file": ("p.docx", wrong_caption_doc(), DOCX_CT)}, data={"consent": "yes"}).json()["job_id"]
    run_worker()
    fid = c.get(f"/api/jobs/{job}/figures").json()["figures"][0]["figure_id"]
    assert c.post(f"/api/jobs/{job}/correct", json={"figure_ids": [fid], "dismiss": True}).json()["dismissed"] == 1
    f0 = c.get(f"/api/jobs/{job}/figures").json()["figures"][0]
    assert not any(m["kind"] == "description" for m in f0["mistakes"])


def test_offline_detects_number_mistakes_only(app):
    c = make_client(app)
    job = c.post("/api/uploads", files={"file": ("p.docx", wrong_caption_doc(), DOCX_CT)}, data={"consent": "yes"}).json()["job_id"]
    run_worker()
    cat, net = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    assert cat["mistakes"] == []  # no vision model: description is not judged
    assert [m["kind"] for m in net["mistakes"]] == ["number"]
