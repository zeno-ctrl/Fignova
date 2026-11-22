"""Isolated LibreOffice rendering + poppler rasterisation (directives 16, 60, 81).

Each conversion runs with a throw-away LibreOffice profile, a wall-clock
timeout, and — when the host supports unprivileged user namespaces — no
network access at all (``unshare -rn``).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lxml import etree


class RenderError(RuntimeError):
    pass


def soffice_path() -> Optional[str]:
    return os.environ.get("AUTOFIGURE_SOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")


def _sandbox_prefix() -> list[str]:
    mode = os.environ.get("AUTOFIGURE_RENDER_SANDBOX", "auto")
    if mode == "none":
        return []
    unshare = shutil.which("unshare")
    if unshare and _unshare_works(unshare):
        return [unshare, "-rn"]
    if mode == "unshare":
        raise RenderError("sandbox_unavailable")
    return []


_UNSHARE_OK: Optional[bool] = None


def _unshare_works(path: str) -> bool:
    global _UNSHARE_OK
    if _UNSHARE_OK is None:
        try:
            _UNSHARE_OK = subprocess.run([path, "-rn", "true"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            _UNSHARE_OK = False
    return _UNSHARE_OK


def renderer_status() -> dict:
    sp = soffice_path()
    version = None
    if sp:
        try:
            version = subprocess.run([sp, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:
            version = None
    return {
        "soffice": sp,
        "version": version,
        "pdftoppm": shutil.which("pdftoppm"),
        "pdftotext": shutil.which("pdftotext"),
        "network_sandbox": bool(_sandbox_prefix()) if sp else False,
    }


def docx_to_pdf(docx: bytes, timeout: int = 180) -> bytes:
    sp = soffice_path()
    if not sp:
        raise RenderError("renderer_not_installed")
    with tempfile.TemporaryDirectory(prefix="afs-render-") as td:
        tdp = Path(td)
        src = tdp / "input.docx"
        src.write_bytes(docx)
        profile = tdp / "profile"
        cmd = _sandbox_prefix() + [
            sp, "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nologo",
            f"-env:UserInstallation=file://{profile}",
            "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(tdp / "out"), str(src),
        ]
        env = {"HOME": str(tdp), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=td)
        except subprocess.TimeoutExpired as e:
            raise RenderError("render_timeout") from e
        out = tdp / "out" / "input.pdf"
        if proc.returncode != 0 or not out.exists():
            raise RenderError("render_failed")
        return out.read_bytes()


@dataclass
class PageText:
    page: int
    width: float
    height: float
    lines: list[tuple[str, float, float, float, float]]  # text, xMin, yMin, xMax, yMax (pt)


def pdf_layout(pdf: bytes) -> list[PageText]:
    """Page sizes and text lines with bounding boxes (pdftotext -bbox-layout)."""
    with tempfile.TemporaryDirectory(prefix="afs-pdf-") as td:
        p = Path(td) / "d.pdf"
        p.write_bytes(pdf)
        out = Path(td) / "d.html"
        r = subprocess.run(["pdftotext", "-bbox-layout", str(p), str(out)], capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RenderError("pdf_text_failed")
        raw = out.read_bytes()
    raw = re.sub(rb"<!DOCTYPE[^>]*>", b"", raw)
    root = etree.fromstring(raw, etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    pages: list[PageText] = []
    for i, page in enumerate(root.iter("{http://www.w3.org/1999/xhtml}page"), start=1):
        lines = []
        for line in page.iter("{http://www.w3.org/1999/xhtml}line"):
            words = [w.text or "" for w in line.iter("{http://www.w3.org/1999/xhtml}word")]
            lines.append((" ".join(words), float(line.get("xMin")), float(line.get("yMin")),
                          float(line.get("xMax")), float(line.get("yMax"))))
        pages.append(PageText(i, float(page.get("width")), float(page.get("height")), lines))
    return pages


def pdf_images_per_page(pdf: bytes) -> dict[int, int]:
    with tempfile.TemporaryDirectory(prefix="afs-img-") as td:
        p = Path(td) / "d.pdf"
        p.write_bytes(pdf)
        r = subprocess.run(["pdfimages", "-list", str(p)], capture_output=True, text=True, timeout=60)
    counts: dict[int, int] = {}
    for line in r.stdout.splitlines()[2:]:
        cols = line.split()
        if len(cols) > 2 and cols[0].isdigit() and cols[2] == "image":
            counts[int(cols[0])] = counts.get(int(cols[0]), 0) + 1
    return counts


def rasterize(pdf: bytes, dpi: int = 110, thumb_width: int = 180) -> tuple[list[bytes], list[bytes]]:
    """Return (page PNGs, thumbnail PNGs)."""
    with tempfile.TemporaryDirectory(prefix="afs-ras-") as td:
        p = Path(td) / "d.pdf"
        p.write_bytes(pdf)
        r = subprocess.run(["pdftoppm", "-png", "-r", str(dpi), str(p), str(Path(td) / "page")],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RenderError("rasterize_failed")
        r = subprocess.run(["pdftoppm", "-png", "-scale-to", str(thumb_width), str(p), str(Path(td) / "thumb")],
                           capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RenderError("rasterize_failed")

        def ordered(prefix: str) -> list[bytes]:
            files = sorted(Path(td).glob(prefix + "-*.png"), key=lambda f: int(f.stem.split("-")[-1]))
            return [f.read_bytes() for f in files]

        return ordered("page"), ordered("thumb")
