import io
import zipfile

from autofigure.engine.validation import output_filename, safe_display_name, validate_docx
from tests.fixtures import cover_lo_doc


def z(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in entries.items():
            zf.writestr(n, d)
    return b.getvalue()


def test_accepts_real_docx():
    r = validate_docx("a.docx", cover_lo_doc())
    assert r.ok and r.media_count >= 6


def test_rejections():
    good = cover_lo_doc()
    assert validate_docx("a.docx", b"").code == "empty_file"
    assert validate_docx("a.pdf", b"%PDF-1.5").code == "wrong_type_pdf"
    assert validate_docx("a.docx", b"%PDF-1.5").code == "wrong_type_pdf"
    assert validate_docx("a.rtf", b"{\\rtf1").code == "wrong_type_rtf"
    assert validate_docx("a.txt", b"hello").code == "wrong_extension"
    assert validate_docx("a.docm", good).code == "macro_enabled"
    assert validate_docx("a.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest").code == "encrypted_or_legacy"
    assert validate_docx("a.docx", b"PK\x03\x04garbage").code == "corrupt_package"
    assert validate_docx("a.docx", z({"x.txt": "hi"})).code == "not_ooxml"
    assert validate_docx("a.docx", good, type("L", (), {"max_bytes": 10, "max_uncompressed": 1, "max_entries": 1,
                                                        "max_ratio": 1, "max_media": 1, "max_xml_part": 1})()).code == "too_large"


def test_xlsx_is_not_word():
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
    assert validate_docx("a.docx", z({"[Content_Types].xml": ct, "xl/workbook.xml": "<w/>"})).code == "not_word"


def test_hostile_packages():
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    xxe = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><d>&x;</d>'
    assert validate_docx("a.docx", z({"[Content_Types].xml": ct, "word/document.xml": xxe})).code == "xml_doctype"
    assert validate_docx("a.docx", z({"[Content_Types].xml": ct, "../evil.xml": "<a/>", "word/document.xml": "<a/>"})).code == "unsafe_path"
    assert validate_docx("a.docx", z({"[Content_Types].xml": ct, "word/document.xml": "<a/>", "word/vbaProject.bin": "x"})).code == "macro_enabled"
    bomb = z({"[Content_Types].xml": ct, "word/document.xml": "<a/>", "word/media/big.bin": b"\0" * 30_000_000})
    assert validate_docx("a.docx", bomb).code == "zip_bomb"


def test_filenames():
    assert safe_display_name('../../etc/pa"ss<wd>.docx') == "passwd.docx"
    assert "/" not in safe_display_name("../../x.docx")
    assert output_filename("My Report.docx") == "My Report_AutoFigure.docx"
    assert output_filename('bad\r\nname";.docx').endswith("_AutoFigure.docx") and "\n" not in output_filename('a\nb.docx')
