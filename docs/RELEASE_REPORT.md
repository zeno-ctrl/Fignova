# Release report

Evidence that each major requirement of the AutoFigure Studio build prompt is implemented, where, how it is tested,
and where it is visible.

| # | Requirement | Implementation | Automated test | Screen |
|---|---|---|---|---|
| 07 / 47 | Word-only upload; PDF/JPG/PNG/.doc/.docm/zip rejected before processing | `engine/validation.py`, `web/static/js/upload.js` | `test_validation.py`, `test_api.py::test_rejects_non_word_files` | `screenshots/app-reject.png` |
| 08 / 48 | Original immutable; surgical edits only | `engine/ooxml.py`, `engine/writer.py`, `engine/verify.py` | `test_engine.py::test_only_intended_parts_changed`, owner original download hash check | — |
| 09 / 49 | Every displayed picture in reading order (inline, floating, table, same media twice, 100+) | `engine/locator.py` | `test_engine.py` (table, floating, same image twice, 120 images) | `review.png` |
| 10 / 52 | Page-1 exclusion from rendered original | `engine/pagination.py` | `test_scenario_cover_lo1_lo2`, `test_automatic_pagination_without_breaks`, `test_floating_cover_image_excluded` | `review.png` (“1 excluded on page 1”) |
| 11 / 50 / 51 | LO / Heading 1 numbering with resets | `engine/numbering.py` | `test_numbering.py`, `test_api.py::test_upload_reaches_review_with_correct_numbering` | `review.png` |
| 12 / 56 / 57 | Caption below image, centered italic, Caption style, SEQ fields | `engine/writer.py` | `test_caption_directly_below_picture_and_centered`, `test_seq_fields_with_section_restart` | `review-applied.png` |
| 13 / 58 | Existing captions, no duplicates, idempotent | `locator.detect_caption_paragraph`, `numbering` | `test_existing_captions_*`, `test_reprocessing_output_is_idempotent` | — |
| new | Mistakes in existing captions + **Correct all** | `pipeline.detect_mistakes`, `api/public.py::correct_mistakes`, `review.js` | `test_mistakes.py` | `mistakes.png` |
| new | AutoFigure Vision: offline OCR + shape analysis captions | `ai/local_vision.py` | `test_vision.py` | `docs/samples/coursework_input_AutoFigure.docx` |
| 14 / 54 / 83 / 84 | AutoFigure AI: grounded, validated, cross-checked, injection-resistant | `ai/autofigure_ai.py`, `ai/captioner.py`, `ai/openai_provider.py` | `test_ai.py` | `review-edit.png` |
| 16 / 60 | Preview from exact output revision; edits regenerate | `engine/process.py::render_preview`, `pipeline._build_and_render` | `test_preview_is_rendered_from_output_and_served`, `test_edit_caption_regenerates_docx_and_preview` (Test D) | `review-compare.png` |
| 17 / 59 | Three-pane review, mobile tabs, settings, chapter mapping | `web/templates/review.html`, `review.js` | browser run (no console errors) | `review*.png`, `mobile-review-*.png` |
| 18 | Verified download, safe filename, latest rendered revision | `api/public.py::download` | Test D | — |
| 19 / 23 / 69 | Single owner, password + TOTP, server-side checks, step-up | `api/owner.py`, `api/common.py`, `__main__.py owner-invite` | `test_owner_sees_all_uploads_with_mfa_and_audit`, `test_anonymous_cannot_reach_owner` (Test G) | `owner-enroll.png` |
| 20 / 21 / 34 / 71 | Owner library of all retained uploads, previews, downloads, metrics | `api/owner.py`, `web/templates/owner/*`, `owner.js` | Test I | `owner-dashboard.png`, `owner-uploads.png`, `owner-upload-preview.png` |
| 22 / 68 | Owner access disclosed before upload; AI provider disclosure | `partials/uploader.html`, `privacy.html` | `test_security_headers` (creator credit) | `app-ready.png` |
| 33 / 86 | Deletion and retention | `pipeline.delete_upload_content`, `retention_sweep` | `test_user_delete_removes_access` (Test J), `test_retention_expiry` | owner Retention page |
| 74 / 85 | Guest isolation (BOLA) | `api/common.py::guest_job` | `test_other_guest_cannot_access_job` (Test H) | — |
| 28 / 29 | CSRF, Origin, CSP, hostile XML/zip | `api/common.py`, `api/app.py`, `validation.py` | `test_consent_and_csrf_required`, `test_hostile_packages` | — |
| 25 / 61 / 77 | Durable jobs, real progress, retries, stalled recovery | `pipeline.py`, `queue.py` | all e2e tests drive the real worker | `processing.png` |

Test run: `python -m pytest -q` → **69 passed**.
