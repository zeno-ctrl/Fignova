"""AutoFigure Vision: offline OCR + shape analysis produces grounded captions (no API key)."""
import io

import pytest
from PIL import Image

from autofigure.ai.autofigure_ai import AutoFigureAI
from autofigure.ai.captioner import CaptionContext
from autofigure.ai.local_vision import analyse, known_command, ocr_available
from tests import fixtures as fx
from tests.conftest import make_client, run_worker

pytestmark = pytest.mark.skipif(not ocr_available(), reason="rapidocr-onnxruntime not installed")


def jpg(b):
    o = io.BytesIO()
    Image.open(b).convert("RGB").save(o, "JPEG", quality=88)
    return o.getvalue()


@pytest.mark.parametrize("make,kind,expected", [
    (fx.network_image, "network_diagram", "Network topology with 7 switches and a router"),
    (fx.screenshot_image, "screenshot", "Screenshot of interface status with VLAN assignments"),
    (fx.cli_image, "cli", "Output of “show ip route” showing the routing table"),
    (fx.login_image, "screenshot", "Login page with username and password fields"),
    (fx.chart_image, "chart", "Bar chart comparing 8 values"),
])
def test_grounded_captions(make, kind, expected):
    r = analyse(jpg(make()))
    assert (r.kind, r.caption) == (kind, expected)


def test_photo_content_is_not_invented():
    r = analyse(jpg(fx.cat_image()))
    assert r.caption is None and r.confidence == "low"  # offline engine never guesses what a photo shows


def test_command_dictionary_handles_squashed_ocr():
    assert known_command("Rl#showipinterfacebrief") == "show ip interface brief"
    assert known_command("C:\\>ipconfig/all") == "ipconfig /all"


def test_offline_ai_combines_vision_and_author_text():
    ai = AutoFigureAI(external=[])
    ctx = CaptionContext("f", "LO1", "", None, "en", "academic", "standard", None, "", "")
    s = ai.suggest(jpg(fx.cli_image()), ctx)
    assert s.caption == "Output of “show ip route” showing the routing table" and not s.needs_review
    ctx.preceding_text = "The network topology used in the lab is shown next."
    s = ai.suggest(jpg(fx.network_image()), ctx)
    assert s.caption == "Network topology used in the lab" and s.confidence_band == "high"


def test_offline_detects_wrong_image_type_in_existing_captions(app):
    c = make_client(app)
    job = c.post("/api/uploads", files={"file": ("m.docx", fx.mislabeled_doc(),
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}, data={"consent": "yes"}).json()["job_id"]
    run_worker()
    a, b = c.get(f"/api/jobs/{job}/figures").json()["figures"]
    assert a["correction"] == "Network topology with 7 switches and a router"
    assert b["correction"] == "Output of “show ip route” showing the routing table"
    assert c.post(f"/api/jobs/{job}/correct", json={}).json()["corrected"] == 2
    run_worker()
    from docx import Document
    doc = Document(io.BytesIO(c.get(f"/api/jobs/{job}/download").content))
    assert [p.text for p in doc.paragraphs if p.style.name == "Caption"] == [
        "Figure 1.1 Network topology with 7 switches and a router",
        "Figure 1.2 Output of “show ip route” showing the routing table"]
