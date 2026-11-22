"""Pure numbering invariants with simple figure lists (directive 51/95)."""
from autofigure.engine.model import ExistingCaption, FigureOccurrence, Heading
from autofigure.engine.numbering import check_invariants, plan_numbering
from autofigure.engine.settings import CaptionSettings, FigureOverride


def fig(i, order, page=2, w=10.0, existing=None, **kw):
    f = FigureOccurrence(figure_id=f"f{i}", order=order, occurrence_index=i, kind="inline", content_type="picture",
                         media_part="word/media/x.png", media_sha256="x", media_content_type="png", width_cm=w,
                         height_cm=w * 0.7, doc_pr_id=str(i), name=None, alt_text=None, in_table=False, in_textbox=False,
                         paragraph_has_text=False, images_in_paragraph=1, wrap=None, nearby_text="", **kw)
    f.page, f.page_confident, f.existing = page, True, existing
    return f


def H(order, text, lo=None, src="style"):
    return Heading(f"h{order}", order, text, 1, src, "Heading1", lo)


def labels(figs):
    return [f.label for f in figs]


def test_lo_reset():
    hs = [H(10, "LO1", 1), H(50, "LO2", 2), H(90, "LO3", 3)]
    figs = [fig(0, 5, page=1), fig(1, 20), fig(2, 30), fig(3, 60), fig(4, 70), fig(5, 80), fig(6, 95)]
    plan_numbering(figs, hs, CaptionSettings())
    assert labels(figs) == [None, "1.1", "1.2", "2.1", "2.2", "2.3", "3.1"]
    assert figs[0].status == "excluded_first_page"
    assert check_invariants(figs) == []


def test_intro_heading_does_not_shift_lo_numbers():
    hs = [H(5, "Introduction"), H(10, "LO1 Research", 1), H(50, "LO2", 2), H(80, "Conclusion")]
    figs = [fig(0, 7), fig(1, 20), fig(2, 60), fig(3, 85)]
    plan_numbering(figs, hs, CaptionSettings())
    assert figs[0].status == "excluded_unassigned"
    assert labels(figs)[1:] == ["1.1", "2.1", "2.2"]  # figure after last LO stays in LO2
    plan_numbering(figs, hs, CaptionSettings(unassigned_policy="first_chapter"))
    assert labels(figs) == ["1.1", "1.2", "2.1", "2.2"]


def test_visible_vs_ordinal_lo_numbers():
    hs = [H(10, "LO3 Evaluation", 3), H(50, "LO4", 4)]
    figs = [fig(0, 20), fig(1, 60)]
    plan_numbering(figs, hs, CaptionSettings())
    assert labels(figs) == ["3.1", "4.1"]
    plan_numbering(figs, hs, CaptionSettings(lo_number_mode="ordinal"))
    assert labels(figs) == ["1.1", "2.1"]


def test_same_lo_split_over_two_headings_continues():
    hs = [H(10, "LO1 Part A", 1), H(30, "LO1 Part B", 1), H(50, "LO2", 2)]
    figs = [fig(0, 20), fig(1, 40), fig(2, 60)]
    plan_numbering(figs, hs, CaptionSettings())
    assert labels(figs) == ["1.1", "1.2", "2.1"]


def test_heading1_fallback_and_document_wide():
    hs = [H(10, "Methods"), H(50, "Results")]
    figs = [fig(0, 20), fig(1, 30), fig(2, 60)]
    _, summary = plan_numbering(figs, hs, CaptionSettings())
    assert summary["mode"] == "heading1" and labels(figs) == ["1.1", "1.2", "2.1"]
    _, summary = plan_numbering(figs, [], CaptionSettings())
    assert summary["mode"] == "document" and labels(figs) == ["1", "2", "3"]
    plan_numbering(figs, hs, CaptionSettings(number_style="chapter_dash"))
    assert labels(figs) == ["1-1", "1-2", "2-1"]


def test_uncertain_page_requires_confirmation():
    hs = [H(10, "LO1", 1)]
    f = fig(0, 20)
    f.page_confident = False
    plan_numbering([f], hs, CaptionSettings())
    assert f.status == "excluded_page_uncertain" and not f.eligible
    plan_numbering([f], hs, CaptionSettings(figure_overrides={"f0": FigureOverride(page_confirmed_not_first=True)}))
    assert f.label == "1.1"


def test_decorative_and_manual_overrides():
    hs = [H(10, "LO1", 1)]
    figs = [fig(0, 20, w=0.8), fig(1, 30), fig(2, 40)]
    plan_numbering(figs, hs, CaptionSettings())
    assert figs[0].status == "excluded_decorative" and labels(figs) == [None, "1.1", "1.2"]
    plan_numbering(figs, hs, CaptionSettings(include_decorative=True))
    assert labels(figs) == ["1.1", "1.2", "1.3"]
    s = CaptionSettings(figure_overrides={"f1": FigureOverride(include=False), "f2": FigureOverride(chapter=5)})
    plan_numbering(figs, hs, s)
    assert figs[1].status == "excluded_manual" and figs[2].label == "5.1"


def test_existing_caption_policies():
    hs = [H(10, "LO1", 1)]
    good = ExistingCaption("Figure 1.1 Topology", "1.1", "Topology", "below", ["caption_style"], False, False)
    wrong = ExistingCaption("Fig. 7 Chart", "7", "Chart", "below", ["caption_style"], False, False)
    figs = [fig(0, 20, existing=good), fig(1, 30, existing=wrong), fig(2, 40)]
    plan_numbering(figs, hs, CaptionSettings())
    assert [f.status for f in figs] == ["preserve", "renumber", "insert"]
    plan_numbering(figs, hs, CaptionSettings(existing_mode="skip"))
    assert [f.status for f in figs] == ["skip_existing", "skip_existing", "insert"]
    assert "plan:existing_number_mismatch" in figs[1].warnings
    plan_numbering(figs, hs, CaptionSettings(existing_mode="replace"))
    assert [f.status for f in figs] == ["replace", "replace", "insert"]


def test_heading_mapping_overrides():
    hs = [H(10, "LO1", 1), H(30, "Appendix"), H(50, "LO2", 2)]
    figs = [fig(0, 20), fig(1, 40), fig(2, 60)]
    plan_numbering(figs, hs, CaptionSettings(heading_overrides={"h50": "ignore"}))
    assert labels(figs) == ["1.1", "1.2", "1.3"]
