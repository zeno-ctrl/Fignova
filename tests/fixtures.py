"""Synthetic, licence-free Word fixtures (directive 90). No real user documents."""
from __future__ import annotations

import io
import math
import random

from docx import Document
from docx.enum.text import WD_BREAK
from docx.shared import Cm
from lxml import etree
from PIL import Image, ImageDraw


def _png(img: Image.Image) -> io.BytesIO:
    b = io.BytesIO()
    img.save(b, "PNG")
    b.seek(0)
    return b


def cat_image(seed: int = 1, size=(800, 600)) -> io.BytesIO:
    """A simple illustrated cat (vector-drawn, authorised sample art)."""
    w, h = size
    img = Image.new("RGB", size, (214, 226, 236))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    for y in range(h):
        c = int(200 + 40 * y / h)
        d.line([(0, y), (w, y)], fill=(c - 30, c - 15, c))
    fur = (120 + rnd.randint(0, 40),) * 3
    cx, cy = w // 2, h // 2 + 40
    d.ellipse([cx - 170, cy - 60, cx + 170, cy + 220], fill=fur)  # body
    d.ellipse([cx - 120, cy - 230, cx + 120, cy + 10], fill=fur)  # head
    d.polygon([(cx - 115, cy - 150), (cx - 95, cy - 290), (cx - 25, cy - 215)], fill=fur)
    d.polygon([(cx + 115, cy - 150), (cx + 95, cy - 290), (cx + 25, cy - 215)], fill=fur)
    for ex in (-50, 50):
        d.ellipse([cx + ex - 28, cy - 140, cx + ex + 28, cy - 95], fill=(236, 214, 90))
        d.ellipse([cx + ex - 8, cy - 140, cx + ex + 8, cy - 95], fill=(20, 20, 20))
    d.polygon([(cx - 12, cy - 75), (cx + 12, cy - 75), (cx, cy - 60)], fill=(230, 140, 150))
    for s in (-1, 1):
        for k in range(3):
            d.line([(cx + s * 20, cy - 62 + k * 8), (cx + s * 140, cy - 80 + k * 18)], fill=(240, 240, 240), width=2)
    return _png(img)


def network_image(size=(900, 520)) -> io.BytesIO:
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    nodes = [(450, 70), (200, 250), (450, 250), (700, 250), (120, 430), (300, 430), (600, 430), (780, 430)]
    edges = [(0, 1), (0, 2), (0, 3), (1, 4), (1, 5), (3, 6), (3, 7), (2, 5), (2, 6)]
    for a, b in edges:
        d.line([nodes[a], nodes[b]], fill=(40, 90, 160), width=4)
    for i, (x, y) in enumerate(nodes):
        d.rounded_rectangle([x - 60, y - 28, x + 60, y + 28], 10, fill=(25, 187, 210) if i else (11, 20, 38))
        d.text((x - 30, y - 6), "Router" if i == 0 else f"Switch {i}", fill=(255, 255, 255))
    return _png(img)


def screenshot_image(size=(1000, 620)) -> io.BytesIO:
    img = Image.new("RGB", size, (243, 244, 246))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size[0], 44], fill=(30, 41, 59))
    for i, x in enumerate((16, 40, 64)):
        d.ellipse([x, 14, x + 16, 30], fill=[(239, 68, 68), (234, 179, 8), (34, 197, 94)][i])
    d.rectangle([0, 44, 220, size[1]], fill=(226, 232, 240))
    for i in range(8):
        d.rectangle([20, 70 + i * 44, 200, 96 + i * 44], fill=(203, 213, 225))
    for r in range(6):
        d.rectangle([250, 80 + r * 80, 960, 140 + r * 80], outline=(148, 163, 184), fill=(255, 255, 255))
        d.text((270, 100 + r * 80), f"Interface Gi0/{r}   status: up   vlan {10 + r}", fill=(15, 23, 42))
    return _png(img)


def chart_image(size=(900, 540), seed: int = 3) -> io.BytesIO:
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    d.line([(80, 470), (860, 470)], fill=(0, 0, 0), width=2)
    d.line([(80, 40), (80, 470)], fill=(0, 0, 0), width=2)
    for i in range(8):
        hgt = rnd.randint(60, 400)
        d.rectangle([110 + i * 92, 470 - hgt, 170 + i * 92, 470], fill=(25, 187, 210) if i % 2 else (124, 92, 255))
    pts = [(110 + i * 40, 300 + 80 * math.sin(i / 2)) for i in range(19)]
    d.line(pts, fill=(239, 68, 68), width=3)
    return _png(img)


def icon_image() -> io.BytesIO:
    img = Image.new("RGB", (64, 64), (25, 187, 210))
    ImageDraw.Draw(img).ellipse([12, 12, 52, 52], fill=(255, 255, 255))
    return _png(img)


def _save(doc: Document) -> bytes:
    b = io.BytesIO()
    doc.save(b)
    return b.getvalue()


def _page_break(doc: Document) -> None:
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def _pic(doc_or_cell, img: io.BytesIO, width_cm: float = 12) -> None:
    p = doc_or_cell.add_paragraph()
    p.alignment = 1
    p.add_run().add_picture(img, width=Cm(width_cm))


def cover_lo_doc() -> bytes:
    """Acceptance tests A/B/C: cover cat on page 1, LO1 two figures, LO2 three figures."""
    doc = Document()
    doc.add_paragraph("Network Engineering Assignment").style = "Title"
    doc.add_paragraph("Student: Sample Student · Unit 5")
    _pic(doc, cat_image(1), 10)
    _page_break(doc)
    doc.add_heading("LO1: Background", level=1)
    doc.add_paragraph("This section introduces the scenario. The cat below is our office mascot.")
    _pic(doc, cat_image(2))
    doc.add_paragraph("The network topology used in the lab is shown next.")
    _pic(doc, network_image())
    _page_break(doc)
    doc.add_heading("LO2: Build and Test", level=1)
    doc.add_paragraph("Router configuration was applied through the web interface.")
    _pic(doc, screenshot_image())
    doc.add_paragraph("Performance results:")
    _pic(doc, chart_image())
    doc.add_paragraph("A second chart compares latency.")
    _pic(doc, chart_image(seed=9))
    return _save(doc)


def intro_then_lo_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Report title").style = "Title"
    _page_break(doc)
    doc.add_heading("Introduction", level=1)
    doc.add_paragraph("Intro text with an image before LO1.")
    _pic(doc, chart_image(seed=2))
    doc.add_heading("LO1 Research", level=1)
    _pic(doc, network_image())
    doc.add_heading("LO 2 - Implementation", level=1)
    _pic(doc, screenshot_image())
    doc.add_heading("Learning Outcome 3: Evaluation", level=1)
    _pic(doc, chart_image(seed=5))
    doc.add_heading("Conclusion", level=1)
    _pic(doc, cat_image(4))
    return _save(doc)


def plain_heading_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("Methods", level=1)
    _pic(doc, network_image())
    _pic(doc, screenshot_image())
    doc.add_heading("Results", level=1)
    _pic(doc, chart_image())
    return _save(doc)


def no_heading_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Cover page with no headings")
    _page_break(doc)
    _pic(doc, chart_image())
    _pic(doc, network_image())
    return _save(doc)


def table_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1 Screenshots", level=1)
    t = doc.add_table(rows=1, cols=2)
    t.style = "Table Grid"
    _pic(t.cell(0, 0), screenshot_image(), 7)
    t.cell(0, 1).add_paragraph("Explanation of the screenshot in the neighbouring cell.")
    _pic(t.cell(0, 1), network_image(), 7)
    return _save(doc)


def existing_captions_doc(correct: bool = True) -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1 Existing", level=1)
    _pic(doc, network_image())
    doc.add_paragraph("Figure 1.1 Network topology overview" if correct else "Fig. 7 Network topology overview",
                      style="Caption")
    _pic(doc, chart_image())
    doc.add_paragraph("Figure 2 shows the measured throughput over time.")
    return _save(doc)


def same_image_twice_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1 Reuse", level=1)
    data = chart_image().getvalue()
    _pic(doc, io.BytesIO(data))
    doc.add_paragraph("Same picture again:")
    _pic(doc, io.BytesIO(data))
    return _save(doc)


def many_images_doc(n: int = 120) -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    data = [chart_image(seed=i).getvalue() for i in range(4)]
    for lo in range(1, 5):
        doc.add_heading(f"LO{lo}", level=1)
        for k in range(n // 4):
            _pic(doc, io.BytesIO(data[k % 4]), 5)
    return _save(doc)


def no_images_doc() -> bytes:
    doc = Document()
    doc.add_heading("LO1", level=1)
    doc.add_paragraph("Only text.")
    return _save(doc)


def auto_pagination_doc() -> bytes:
    """No explicit page break: a long cover page pushes the second image to page 2."""
    doc = Document()
    doc.add_paragraph("Cover with an inline logo-sized photo")
    _pic(doc, cat_image(5), 6)
    for i in range(45):
        doc.add_paragraph(f"Filler line {i} that occupies vertical space on the first page of the document.")
    doc.add_heading("LO1 Flow", level=1)
    _pic(doc, network_image())
    return _save(doc)


def floating_doc() -> bytes:
    """Cover image converted to a floating (anchored) drawing on page 1, plus one on page 2."""
    raw = cover_lo_doc()
    from docx import Document as D
    doc = D(io.BytesIO(raw))
    body = doc.element.body
    ns = {"wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
    inlines = body.findall(".//wp:inline", ns)
    for inline in (inlines[0], inlines[2]):
        _inline_to_anchor(inline)
    b = io.BytesIO()
    doc.save(b)
    return b.getvalue()


def _inline_to_anchor(inline) -> None:
    WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    anchor = etree.Element(WP + "anchor", distT="0", distB="0", distL="114300", distR="114300", simplePos="0",
                           relativeHeight="251659264", behindDoc="0", locked="0", layoutInCell="1", allowOverlap="1")
    etree.SubElement(anchor, WP + "simplePos", x="0", y="0")
    ph = etree.SubElement(anchor, WP + "positionH", relativeFrom="column")
    etree.SubElement(ph, WP + "align").text = "center"
    pv = etree.SubElement(anchor, WP + "positionV", relativeFrom="paragraph")
    etree.SubElement(pv, WP + "posOffset").text = "0"
    for child in list(inline):
        tag = etree.QName(child).localname
        if tag == "extent":
            anchor.append(child)
            etree.SubElement(anchor, WP + "effectExtent", l="0", t="0", r="0", b="0")
            etree.SubElement(anchor, WP + "wrapTopAndBottom")
        elif tag in ("docPr", "cNvGraphicFramePr", "graphic"):
            anchor.append(child)
    inline.getparent().replace(inline, anchor)


def burmese_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("မျက်နှာဖုံး စာမျက်နှာ")
    _page_break(doc)
    doc.add_heading("LO1 နောက်ခံ", level=1)
    doc.add_paragraph("ကွန်ရက် ပုံကြမ်းကို အောက်တွင် ပြထားသည်။")
    _pic(doc, network_image())
    return _save(doc)


def text_lo_doc() -> bytes:
    """LO markers typed as bold text (no Heading 1 style)."""
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    p = doc.add_paragraph()
    p.add_run("LO1 Analysis").bold = True
    _pic(doc, chart_image())
    p = doc.add_paragraph()
    p.add_run("LO2 Design").bold = True
    _pic(doc, network_image())
    return _save(doc)


def icon_doc() -> bytes:
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1", level=1)
    _pic(doc, icon_image(), 0.8)
    _pic(doc, chart_image())
    return _save(doc)


ALL = {
    "01_cover_lo1_lo2.docx": cover_lo_doc,
    "02_intro_before_lo.docx": intro_then_lo_doc,
    "03_heading1_no_lo.docx": plain_heading_doc,
    "04_no_heading.docx": no_heading_doc,
    "05_images_in_table.docx": table_doc,
    "06_existing_captions_correct.docx": lambda: existing_captions_doc(True),
    "07_existing_captions_incorrect.docx": lambda: existing_captions_doc(False),
    "08_same_image_twice.docx": same_image_twice_doc,
    "09_many_images.docx": many_images_doc,
    "10_no_images.docx": no_images_doc,
    "11_auto_pagination.docx": auto_pagination_doc,
    "12_floating_images.docx": floating_doc,
    "13_burmese.docx": burmese_doc,
    "14_text_lo_markers.docx": text_lo_doc,
    "15_decorative_icon.docx": icon_doc,
}


def wrong_caption_doc() -> bytes:
    """A cat photo captioned 'dog', a misnumbered caption, and a correct one."""
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1 Pets and networks", level=1)
    _pic(doc, cat_image(3))
    doc.add_paragraph("Figure 1.1 dog", style="Caption")
    _pic(doc, network_image())
    doc.add_paragraph("Figure 4.9 Network topology", style="Caption")
    return _save(doc)


ALL["16_wrong_existing_captions.docx"] = wrong_caption_doc


def cli_image(size=(900, 420)) -> io.BytesIO:
    """Terminal window with router CLI output (rendered with a real font so OCR can read it)."""
    from PIL import ImageFont
    img = Image.new("RGB", size, (18, 18, 18))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSansMono.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    lines = ["R1#show ip route", "Codes: C - connected, S - static, O - OSPF", "Gateway of last resort is not set",
             "C    192.168.1.0/24 is directly connected, Gi0/0", "O    10.0.0.0/8 [110/2] via 192.168.1.2, Gi0/1",
             "R1#"]
    for i, t in enumerate(lines):
        d.text((20, 20 + i * 34), t, fill=(220, 220, 220), font=font)
    return _png(img)


def login_image(size=(900, 560)) -> io.BytesIO:
    from PIL import ImageFont
    img = Image.new("RGB", size, (245, 247, 250))
    d = ImageDraw.Draw(img)
    try:
        f1, f2 = ImageFont.truetype("DejaVuSans.ttf", 34), ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        f1 = f2 = ImageFont.load_default()
    d.rectangle([250, 80, 650, 480], fill=(255, 255, 255), outline=(200, 205, 215))
    d.text((300, 110), "Router Admin Login", fill=(20, 30, 50), font=f1)
    d.text((290, 200), "Username", fill=(60, 70, 90), font=f2)
    d.rectangle([290, 235, 610, 275], outline=(170, 180, 195))
    d.text((290, 300), "Password", fill=(60, 70, 90), font=f2)
    d.rectangle([290, 335, 610, 375], outline=(170, 180, 195))
    d.rectangle([290, 405, 610, 450], fill=(37, 99, 235))
    d.text((415, 413), "Sign in", fill=(255, 255, 255), font=f2)
    return _png(img)


def mislabeled_doc() -> bytes:
    """A network topology captioned as a chart, and router CLI output captioned as a photo."""
    doc = Document()
    doc.add_paragraph("Cover")
    _page_break(doc)
    doc.add_heading("LO1 Network build", level=1)
    _pic(doc, network_image())
    doc.add_paragraph("Figure 1.1 Bar chart of monthly sales", style="Caption")
    _pic(doc, cli_image())
    doc.add_paragraph("Figure 1.2 Photo of the server room", style="Caption")
    return _save(doc)
