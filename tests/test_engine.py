"""Integration tests on synthetic Word fixtures: analysis, mutation, verification, rendering (directive 35)."""
import io
import re
import zipfile

import pytest
from docx import Document

from autofigure.engine.process import analyze, build_revision, plan, render_preview
from autofigure.engine.settings import CaptionSettings
from autofigure.engine.verify import OutputInvalid, verify_output
from tests import fixtures as fx


def run(raw, settings=None, captions=None):
    s = settings or CaptionSettings()
    ar = analyze(raw)
    for f in ar.analysis.figures:
        f.caption_text = (captions or {}).get(f.occurrence_index, f"Description {f.occurrence_index}")
    summary = plan(ar.analysis.figures, ar.analysis.headings, s)
    rev = build_revision(raw, ar.analysis.figures, s)
    return ar, summary, rev


def body_xml(docx):
    return zipfile.ZipFile(io.BytesIO(docx)).read("word/document.xml").decode()


def captions_in(docx):
    d = Document(io.BytesIO(docx))
    paras = list(d.paragraphs)
    for t in d.tables:
        for row in t.rows:
            for cell in row.cells:
                paras += cell.paragraphs
    return [p.text for p in paras if p.style.name.lower() == "caption"]


@pytest.fixture(scope="module")
def cover():
    return run(fx.cover_lo_doc(), captions={1: "A cat staring"})


def test_scenario_cover_lo1_lo2(cover):
    ar, summary, rev = cover
    figs = ar.analysis.figures
    assert figs[0].page == 1 and figs[0].status == "excluded_first_page"
    assert [f.label for f in figs if f.eligible] == ["1.1", "1.2", "2.1", "2.2", "2.3"]
    assert summary["invariant_problems"] == []
    assert captions_in(rev.docx) == ["Figure 1.1 A cat staring", "Figure 1.2 Description 2", "Figure 2.1 Description 3",
                                     "Figure 2.2 Description 4", "Figure 2.3 Description 5"]


def test_caption_directly_below_picture_and_centered(cover):
    _, _, rev = cover
    d = Document(io.BytesIO(rev.docx))
    ps = d.paragraphs
    for i, p in enumerate(ps):
        if p.text.startswith("Figure 1.1"):
            assert ps[i - 1]._p.xpath(".//w:drawing")  # previous paragraph holds the picture
            assert p.alignment == 1  # centered
            assert p.runs[0].italic
            assert ps[i - 1].paragraph_format.keep_with_next  # caption kept with image
            break
    else:
        pytest.fail("caption not found")


def test_seq_fields_with_section_restart(cover):
    _, _, rev = cover
    xml = body_xml(rev.docx)
    instr = re.findall(r"<w:instrText[^>]*>([^<]*)</w:instrText>", xml)
    assert instr == [" SEQ Figure \\* ARABIC \\r 1 ", " SEQ Figure \\* ARABIC ", " SEQ Figure \\* ARABIC \\r 1 ",
                     " SEQ Figure \\* ARABIC ", " SEQ Figure \\* ARABIC "]
    assert xml.count("_AFG_fig_") == 5


def test_only_intended_parts_changed(cover):
    _, _, rev = cover
    assert rev.verification["changed_parts"] == ["word/document.xml"]
    a = zipfile.ZipFile(io.BytesIO(fx.cover_lo_doc()))
    b = zipfile.ZipFile(io.BytesIO(rev.docx))
    assert sorted(a.namelist()) == sorted(b.namelist())
    for n in a.namelist():
        if n.startswith("word/media/"):
            assert len(a.read(n)) == len(b.read(n))


def test_static_mode_and_custom_format():
    s = CaptionSettings(field_mode="static", separator=": ", prefix="Fig.", end_punctuation=True)
    _, _, rev = run(fx.cover_lo_doc(), s)
    assert "instrText" not in body_xml(rev.docx)
    assert captions_in(rev.docx)[0] == "Fig. 1.1: Description 1."


def test_intro_before_lo_unassigned():
    ar, _, rev = run(fx.intro_then_lo_doc())
    st = [(f.status, f.label) for f in ar.analysis.figures]
    assert st == [("excluded_unassigned", None), ("insert", "1.1"), ("insert", "2.1"), ("insert", "3.1"), ("insert", "3.2")]


def test_heading1_without_lo_and_no_heading():
    ar, s, _ = run(fx.plain_heading_doc())
    assert s["mode"] == "heading1" and [f.label for f in ar.analysis.figures] == ["1.1", "1.2", "2.1"]
    ar, s, rev = run(fx.no_heading_doc())
    assert s["mode"] == "document" and captions_in(rev.docx) == ["Figure 1 Description 0", "Figure 2 Description 1"]


def test_text_pattern_lo_headings():
    ar, s, _ = run(fx.text_lo_doc())
    assert [f.label for f in ar.analysis.figures] == ["1.1", "2.1"]
    assert "headings_from_text_pattern" in s["warnings"]


def test_images_in_table_keep_caption_in_cell():
    ar, _, rev = run(fx.table_doc())
    assert all(f.in_table for f in ar.analysis.figures)
    d = Document(io.BytesIO(rev.docx))
    cell0 = d.tables[0].cell(0, 0)
    texts = [p.text for p in cell0.paragraphs]
    assert "Figure 1.1 Description 0" in texts
    assert "Figure 1.2 Description 1" in [p.text for p in d.tables[0].cell(0, 1).paragraphs]


def test_existing_captions_preserved_without_duplicates():
    ar, _, rev = run(fx.existing_captions_doc(True))
    assert [f.status for f in ar.analysis.figures] == ["preserve", "insert"]
    caps = captions_in(rev.docx)
    assert caps.count("Figure 1.1 Network topology overview") == 1 and len(caps) == 2
    # "Figure 2 shows ..." prose is not treated as a caption and is untouched
    assert any(p.text.startswith("Figure 2 shows") for p in Document(io.BytesIO(rev.docx)).paragraphs)


def test_incorrect_existing_caption_renumbered():
    ar, _, rev = run(fx.existing_captions_doc(False))
    assert ar.analysis.figures[0].status == "renumber"
    caps = captions_in(rev.docx)
    assert caps[0] == "Figure 1.1 Network topology overview" and "Fig. 7" not in " ".join(caps)


def test_reprocessing_output_is_idempotent(cover):
    _, _, rev = cover
    ar2, _, rev2 = run(rev.docx, captions={i: "SHOULD NOT APPEAR" for i in range(10)})
    assert all(f.status in ("preserve", "excluded_first_page") for f in ar2.analysis.figures)
    assert captions_in(rev2.docx) == captions_in(rev.docx)


def test_same_media_twice_counts_as_two_figures():
    ar, _, _ = run(fx.same_image_twice_doc())
    f = ar.analysis.figures
    assert len(f) == 2 and f[0].media_sha256 == f[1].media_sha256 and f[0].figure_id != f[1].figure_id
    assert [x.label for x in f] == ["1.1", "1.2"]


def test_many_images():
    ar, s, rev = run(fx.many_images_doc(120))
    labels = [f.label for f in ar.analysis.figures]
    assert len(labels) == 120 and labels[0] == "1.1" and labels[29] == "1.30" and labels[30] == "2.1" and labels[-1] == "4.30"
    assert rev.verification["verified_captions"] == 120


def test_no_images():
    ar, s, rev = run(fx.no_images_doc())
    assert ar.analysis.figures == [] and rev.manifest == []


def test_automatic_pagination_without_breaks():
    ar, _, _ = run(fx.auto_pagination_doc())
    f = ar.analysis.figures
    assert f[0].page == 1 and f[0].status == "excluded_first_page"
    assert f[1].page >= 2 and f[1].label == "1.1"


def test_floating_cover_image_excluded():
    ar, _, rev = run(fx.floating_doc())
    f = ar.analysis.figures
    assert f[0].kind == "anchor" and f[0].page == 1 and f[0].status == "excluded_first_page"
    assert f[2].kind == "anchor" and f[2].label == "1.2" and "floating_image" in f[2].warnings


def test_decorative_icon_skipped():
    ar, _, _ = run(fx.icon_doc())
    assert [f.status for f in ar.analysis.figures] == ["excluded_decorative", "insert"]


def test_burmese_caption_font_and_text():
    ar, _, rev = run(fx.burmese_doc(), captions={0: "ကွန်ရက် ပုံကြမ်း"})
    xml = body_xml(rev.docx)
    assert "ကွန်ရက် ပုံကြမ်း" in xml and 'w:cs="Myanmar Text"' in xml


def test_xml_special_characters_are_escaped():
    _, _, rev = run(fx.cover_lo_doc(), captions={1: 'Router <config> & "quotes" \x07bell'})
    assert "Figure 1.1 Router <config> & \"quotes\" bell" in captions_in(rev.docx)


def test_verify_rejects_tampered_output(cover):
    _, _, rev = cover
    bad = rev.docx.replace(b"PK", b"PK", 1)
    manifest = list(rev.manifest)
    manifest[0] = dict(manifest[0], text="Figure 9.9 wrong")
    with pytest.raises(OutputInvalid):
        verify_output(fx.cover_lo_doc(), bad, manifest)


def test_preview_highlights_every_caption(cover):
    ar, _, rev = cover
    labels = {f.figure_id: f.label for f in ar.analysis.figures if f.eligible}
    pv = render_preview(rev.docx, labels, "Figure")
    assert set(pv.manifest["highlights"]) == set(labels)
    assert len(pv.pages) == pv.manifest["page_count"] and pv.pages[0][:4] == b"\x89PNG"
