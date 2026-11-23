"""AutoFigure Vision — the offline image analyser behind AutoFigure AI.

Runs entirely on the server (no API key, no data leaves the machine):

* OCR (RapidOCR, ONNX models bundled in the pip package) reads text inside the image.
* Pixel analysis recognises bar charts, photographs and light UI screenshots.
* Domain rules turn the evidence into a grounded caption, focused on the figures that
  dominate coursework: network diagrams, device/CLI output, configuration screenshots,
  login pages, web pages, charts and tables.

Every caption is built only from text actually read in the image or from measured shapes,
so it never invents content. When the evidence is weak the result says so (low confidence).
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

log = logging.getLogger("autofigure.vision")

_OCR = None
_OCR_FAILED = False


def _ocr_engine():
    global _OCR, _OCR_FAILED
    import os
    if os.environ.get("AUTOFIGURE_DISABLE_OCR"):
        return None
    if _OCR is None and not _OCR_FAILED:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except Exception as e:  # package missing → analysis continues without OCR
            log.warning("ocr_unavailable code=%s", type(e).__name__)
            _OCR_FAILED = True
    return _OCR


@dataclass
class TextLine:
    text: str
    x: float
    y: float
    h: float
    conf: float


@dataclass
class VisionResult:
    caption: Optional[str]
    kind: str  # screenshot | cli | diagram | network_diagram | chart | table | photograph | illustration | other
    confidence: str  # high | medium | low
    evidence: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


DEVICES = {
    "router": "router", "switch": "switch", "pc": "PC", "laptop": "laptop", "server": "server", "firewall": "firewall",
    "printer": "printer", "phone": "IP phone", "ap": "access point", "accesspoint": "access point", "hub": "hub",
    "cloud": "cloud", "internet": "Internet", "isp": "ISP", "modem": "modem", "host": "host", "client": "client",
    "workstation": "workstation", "wlc": "wireless controller", "nas": "NAS",
}
CLI_PROMPT = re.compile(r"^(?:[A-Za-z][\w\-.]{0,30}(?:\([\w\-]+\))?[#>]|[$#]\s|C:\\[^>]*>|PS [A-Z]:\\[^>]*>)\s*(.*)$")
CLI_CMD = re.compile(r"\b(show\s+[a-z\-]+(?:\s+[a-z\-]+){0,3}|ping\s+\S+|tracert\s+\S+|traceroute\s+\S+|ipconfig(?:\s*/all)?|"
                     r"ifconfig|nslookup\s+\S+|netstat\s*\S*|ip\s+route\s*\S*|conf(?:igure)?\s+t(?:erminal)?|"
                     r"interface\s+\S+|router\s+(?:ospf|eigrp|rip|bgp)\b[\s\d]*|vlan\s+\d+|ssh\s+\S+|telnet\s+\S+)", re.I)
TOPICS = [
    (r"\bvlan\b", "VLAN"), (r"\bospf\b", "OSPF"), (r"\beigrp\b", "EIGRP"), (r"\bbgp\b", "BGP"), (r"\brip\b", "RIP"),
    (r"\bdhcp\b", "DHCP"), (r"\bdns\b", "DNS"), (r"\bnat\b", "NAT"), (r"\bacl\b|access-list", "access control list"),
    (r"\bstp\b|spanning-tree", "spanning tree"), (r"\btrunk", "trunk"), (r"\bssh\b", "SSH"), (r"\bwi-?fi|ssid|wlan", "wireless"),
    (r"\bip address|\bipv4|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "IP addressing"), (r"\bipv6\b", "IPv6"),
    (r"\bfirewall\b", "firewall"), (r"\bvpn\b", "VPN"), (r"\bping\b|reply from|bytes=", "ping test"),
    (r"\binterface\b|\bgi\d|\bfa\d|gigabitethernet|fastethernet", "interface"), (r"\broute\b|routing table|gateway of last resort", "routing"),
    (r"\bmac address|\bmac\b", "MAC address"), (r"\bpassword|username|log ?in|sign ?in", "login"),
    (r"\bdatabase|\bsql\b|select .* from", "database"), (r"\bdef |function|class |import |public static", "source code"),
]
KNOWN_COMMANDS = [
    "show ip interface brief", "show ip route", "show ip ospf neighbor", "show ip protocols", "show ip nat translations",
    "show running-config", "show startup-config", "show vlan brief", "show interfaces trunk", "show interfaces status",
    "show interfaces", "show cdp neighbors", "show mac address-table", "show spanning-tree", "show version",
    "show access-lists", "show ip dhcp binding", "show arp", "show ipv6 route", "show ipv6 interface brief",
    "ipconfig /all", "ipconfig", "ifconfig", "ip addr", "ip route", "netstat", "nslookup", "tracert", "traceroute", "ping",
]
_KNOWN = sorted(((c.replace(" ", "").lower(), c) for c in KNOWN_COMMANDS), key=lambda x: -len(x[0]))
CMD_TOPIC = {"route": "routing table", "interface brief": "interface status", "vlan": "VLANs", "ospf": "OSPF neighbours",
             "nat": "NAT translations", "running-config": "running configuration", "startup-config": "startup configuration",
             "cdp": "CDP neighbours", "mac address": "MAC address table", "spanning": "spanning tree", "version": "device version",
             "access-lists": "access lists", "dhcp": "DHCP bindings", "arp": "ARP table", "trunk": "trunk links"}


def known_command(text: str) -> Optional[str]:
    squashed = re.sub(r"\s+", "", text).lower().replace("rl#", "r1#")
    for key, cmd in _KNOWN:
        if key in squashed:
            return cmd
    return None


def _terminal_like(im: Image.Image) -> bool:
    small = im.convert("L").resize((200, max(1, int(200 * im.height / im.width))))
    px = list(small.getdata())
    dark = sum(1 for v in px if v < 60) / len(px)
    bright = sum(1 for v in px if v > 110) / len(px)  # downscaled glyphs are grey, not white
    return dark > 0.7 and 0.002 < bright < 0.2  # mostly black with sparse light text


def _dark(im: Image.Image) -> bool:
    from PIL import ImageStat
    return ImageStat.Stat(im.convert("L").resize((64, 64))).mean[0] < 90


UI_TITLE_STOP = re.compile(r"^(file|edit|view|help|tools|window|home|insert|ok|cancel|apply|close|\d+|[\W_]+)$", re.I)


def _to_rgb(image_jpeg: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(image_jpeg))
    im.load()
    im = im.convert("RGB")
    im.thumbnail((1400, 1400))
    return im


def _ocr(im: Image.Image) -> list[TextLine]:
    eng = _ocr_engine()
    if eng is None:
        return []
    import numpy as np
    try:
        res, _ = eng(np.array(im))
    except Exception as e:
        log.warning("ocr_failed code=%s", type(e).__name__)
        return []
    out = []
    for box, text, conf in res or []:
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        text = " ".join(str(text).split())
        if not text or conf < 0.55:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append(TextLine(text, min(xs) / im.width, min(ys) / im.height, (max(ys) - min(ys)) / im.height, conf))
    out.sort(key=lambda t: (round(t.y, 2), t.x))
    return out


def _bars(im: Image.Image) -> int:
    """Count vertical bars sharing a common baseline (bar chart detector)."""
    import numpy as np
    a = np.asarray(im.resize((300, int(300 * im.height / im.width) or 1)).convert("HSV")).astype(float) / 255
    h, w = a.shape[:2]
    mask = (a[..., 1] > 0.35) & (a[..., 2] > 0.25)
    if mask.mean() < 0.02 or mask.mean() > 0.7:
        return 0
    col = mask.mean(axis=0)
    runs, start = [], None
    for x in range(w):
        on = col[x] > 0.06
        if on and start is None:
            start = x
        if (not on or x == w - 1) and start is not None:
            end = x if not on else x + 1
            if end - start >= max(2, w * 0.012):
                runs.append((start, end))
            start = None
    if len(runs) < 3:
        return 0
    bottoms = []
    for s, e in runs:
        rows = np.where(mask[:, s:e].mean(axis=1) > 0.5)[0]
        if len(rows) < h * 0.04:
            return 0
        bottoms.append(rows.max())
        # a bar is a solid block: its filled rows should be contiguous
        if (rows.max() - rows.min() + 1) > len(rows) * 1.35:
            return 0
    if max(bottoms) - min(bottoms) > h * 0.04:
        return 0
    return len(runs)


def _photo_like(im: Image.Image) -> bool:
    small = im.copy()
    small.thumbnail((200, 200))
    colors = small.getcolors(200 * 200) or []
    return len(colors) > 7000


def _light_ui(im: Image.Image) -> tuple[float, float]:
    from PIL import ImageStat
    small = im.copy()
    small.thumbnail((256, 256))
    hsv = small.convert("HSV")
    light = sum(1 for v in hsv.split()[2].getdata() if v > 215) / (small.width * small.height)
    sat = ImageStat.Stat(hsv.split()[1]).mean[0] / 255
    return light, sat


def _topics(text: str) -> list[str]:
    found = []
    for rx, name in TOPICS:
        if re.search(rx, text, re.I) and name not in found:
            found.append(name)
    return found


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"a {word}" if word[0].lower() not in "aeiou" else f"an {word}"
    irregular = {"switch": "switches", "IP phone": "IP phones", "access point": "access points"}
    return f"{n} {irregular.get(word, word + 's')}"


def _clean_title(t: str) -> str:
    t = re.sub(r"[|_~`]+", " ", t).strip(" -:·•")
    return " ".join(t.split())[:60]


def lines_from_client(raw: list[dict]) -> list[TextLine]:
    """Text lines read by the visitor's browser (Tesseract), with coordinates normalised to 0..1."""
    out = []
    for r in raw[:400]:
        try:
            text = " ".join(str(r.get("text", "")).split())[:300]
            conf = float(r.get("conf", 0))
            conf = conf / 100.0 if conf > 1 else conf
            if not text or conf < 0.55:
                continue
            out.append(TextLine(text, float(r.get("x", 0)), float(r.get("y", 0)), float(r.get("h", 0)), conf))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda t: (round(t.y, 2), t.x))
    return out


def analyse(image_jpeg: Optional[bytes], lines: Optional[list[TextLine]] = None) -> VisionResult:
    if not image_jpeg:
        return VisionResult(None, "other", "low", ["no_image"])
    try:
        im = _to_rgb(image_jpeg)
    except Exception:
        return VisionResult(None, "other", "low", ["unreadable_image"])
    if lines is None:
        lines = _ocr(im)
    texts = [ln.text for ln in lines]
    alltext = " \n".join(texts)
    low = alltext.lower()
    ev = [f"ocr_lines:{len(lines)}"]

    # 1. command-line / device output
    prompts = sum(1 for t in texts if CLI_PROMPT.match(t))
    known = next((c for c in (known_command(t) for t in texts) if c), None)
    dark = _dark(im)
    if lines and (prompts >= 1 or (dark and (known or len(lines) >= 4))):
        cmd = known
        if not cmd:
            for t in texts:
                m = CLI_PROMPT.match(t)
                if m and CLI_CMD.search(m.group(1) or ""):
                    cmd = " ".join(CLI_CMD.search(m.group(1)).group(0).split()).lower()
                    break
        tops = [x for x in _topics(alltext) if x not in ("interface",)][:2]
        if cmd:
            what = next((v for k, v in CMD_TOPIC.items() if k in cmd), None)
            cap = f"Output of “{cmd}”"
            if what:
                cap += f" showing the {what}"
            elif tops and cmd.split()[0] not in ("ping", "tracert", "traceroute"):
                cap += f" showing {_join(tops)}"
        elif tops:
            cap = f"Command-line output showing {_join(tops)}"
        else:
            cap = "Command-line output"
        if re.search(r"reply from|bytes=|ttl=", low) and cmd and cmd.startswith("ping"):
            cap = "Successful ping test results" if not re.search(r"timed out|unreachable", low) else "Ping test with failed replies"
        return VisionResult(cap, "cli", "medium" if cmd else "low", ev + ["cli_output"], texts)

    # 2. network diagram: device labels ("Router", "Switch 1", "PC-3" …). Several labels may share one OCR line.
    labels: dict[str, dict] = {}
    dev_re = re.compile(r"\b(" + "|".join(sorted(DEVICES, key=len, reverse=True)) + r")(?![a-z])[\s\-_]*(\d{1,3})?", re.I)
    for t in texts:
        if len(t.split()) > 12:
            continue  # sentences are not diagram labels
        for m in dev_re.finditer(t):
            name = DEVICES[m.group(1).lower()]
            e = labels.setdefault(name, {"numbers": set(), "plain": 0})
            if m.group(2):
                e["numbers"].add(int(m.group(2)))
            else:
                e["plain"] += 1
    total = sum(len(e["numbers"]) + e["plain"] for e in labels.values())
    if total >= 3:
        parts = []
        for name, e in sorted(labels.items(), key=lambda kv: -(len(kv[1]["numbers"]) + kv[1]["plain"]))[:4]:
            nums = e["numbers"]
            if nums and not e["plain"]:
                # state a count only when the labels prove it: 1..N (or 0..N-1) all present
                exact = nums == set(range(1, max(nums) + 1)) or nums == set(range(0, max(nums) + 1))
                parts.append(_plural(len(nums), name) if exact else _plural(2, name).split(" ", 1)[1])
            elif nums or e["plain"] > 1:
                parts.append(_plural(2, name).split(" ", 1)[1])  # plural without a number
            else:
                parts.append(_plural(1, name))
        tops = [x for x in _topics(alltext) if x not in ("interface", "IP addressing")][:1]
        cap = f"Network topology with {_join(parts)}"
        if "IP addressing" in _topics(alltext):
            cap += " and IP addressing"
        elif tops:
            cap += f" ({tops[0]})"
        return VisionResult(cap, "network_diagram", "medium", ev + ["device_labels"], texts)

    # 3. bar chart
    nbars = _bars(im)
    if nbars >= 3:
        title = next((_clean_title(ln.text) for ln in lines if ln.y < 0.15 and len(ln.text.split()) >= 2), None)
        cap = f"Bar chart of {title[0].lower() + title[1:]}" if title else f"Bar chart comparing {nbars} values"
        return VisionResult(cap, "chart", "medium" if title else "low", ev + [f"bars:{nbars}"], texts)

    light, sat = _light_ui(im)
    # 4. login / web / configuration screenshot with readable text
    if len(lines) >= 3 and light > 0.3:
        tops = _topics(alltext)
        if "login" in tops and re.search(r"password", low):
            return VisionResult("Login page with username and password fields", "screenshot", "medium", ev + ["login_fields"], texts)
        big = sorted([ln for ln in lines if not UI_TITLE_STOP.match(ln.text) and 2 <= len(ln.text.split()) <= 8],
                     key=lambda ln: (-ln.h, ln.y))
        title = _clean_title(big[0].text) if big and big[0].h > 0.035 else None
        net = [x for x in tops if x not in ("login",)][:2]
        if "interface" in net and re.search(r"\b(up|down)\b", low):
            cap = "Screenshot of interface status"
            extra = [x for x in net if x != "interface"]
            if "VLAN" in _topics(alltext):
                cap += " with VLAN assignments"
            elif extra:
                cap += f" ({extra[0]})"
            return VisionResult(cap, "screenshot", "medium", ev + ["interface_status"], texts)
        if title and net:
            return VisionResult(f"Screenshot of {title} ({_join(net)})", "screenshot", "medium", ev, texts)
        if title:
            return VisionResult(f"Screenshot of {title}", "screenshot", "low", ev, texts)
        if net:
            return VisionResult(f"Screenshot showing {_join(net)} settings", "screenshot", "low", ev, texts)
        return VisionResult("Screenshot", "screenshot", "low", ev, texts)

    # 5. table image (grid of short text cells)
    if len(lines) >= 8:
        rows = Counter(round(ln.y, 2) for ln in lines)
        if sum(1 for c in rows.values() if c >= 3) >= 3:
            return VisionResult("Table", "table", "low", ev + ["grid_text"], texts)

    if not lines and _dark(im) and _terminal_like(im):
        # no OCR available (serverless build): a dark window with small light glyphs is a terminal/console
        return VisionResult("Command-line output", "cli", "low", ev + ["dark_terminal"], texts)
    if _photo_like(im):
        return VisionResult(None, "photograph", "low", ev + ["continuous_tone"], texts)
    return VisionResult(None, "illustration" if sat > 0.08 else "other", "low", ev, texts)


def ocr_available() -> bool:
    return _ocr_engine() is not None
