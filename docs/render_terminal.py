"""Render fpl-coach text output as a terminal-window PNG for the README.

    fpl-coach analyze 1234567 --color | python docs/render_terminal.py out.png

With --color the real terminal colours are used; plain text falls back to simple highlighting rules.

Colouring mimics a terminal theme: headings, gains, flags and captaincy are highlighted.
"""

from __future__ import annotations

import argparse
import re
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (24, 26, 33)
BAR = (36, 39, 48)
FG = (214, 218, 226)
DIM = (110, 117, 133)
HEAD = (130, 214, 160)
ACCENT = (122, 178, 247)
GOOD = (136, 214, 120)
WARN = (240, 190, 100)
BAD = (240, 110, 110)
PROMPT = (199, 146, 234)

RULES = [  # (regex, colour, bold)
    (re.compile(r"\+\d+\.\d"), GOOD, True),
    (re.compile(r"-\d+ pts"), BAD, False),
    (re.compile(r"unavailable|\d+% to play"), BAD, True),
    (re.compile(r"rarely starts|poor form|tough fixtures|blank next GW"), WARN, False),
    (re.compile(r"\(C\)|\(V\)"), ACCENT, True),
    (re.compile(r"(?<!Triple )Captain:|Vice:|Formation \d-\d-\d:|Bench \(in order\):|Available now:|Later:"), ACCENT, True),
    (re.compile(r"^\d\. [^:]+:?|^\d\. Captain"), ACCENT, True),
    (re.compile(r"\bOUT\b"), BAD, True),
    (re.compile(r"\bIN\b"), GOOD, True),
    (re.compile(r"Expected GW\d+ points: [\d.]+"), GOOD, True),
    (re.compile(r"\[bench\]"), DIM, False),
]


def segments(line: str, heading: bool, header_row: bool):
    if heading:
        return [(line, HEAD, True)]
    if set(line.strip()) <= set("=-  ") and line.strip():
        return [(line, DIM, False)]
    if header_row:
        return [(line, FG, True)]
    spans = []
    for rx, colour, bold in RULES:
        for m in rx.finditer(line):
            if not any(s < m.end() and m.start() < e for s, e, *_ in spans):
                spans.append((m.start(), m.end(), colour, bold))
    spans.sort()
    out, pos = [], 0
    for s, e, colour, bold in spans:
        if s > pos:
            out.append((line[pos:s], FG, False))
        out.append((line[s:e], colour, bold))
        pos = e
    if pos < len(line):
        out.append((line[pos:], FG, False))
    return out


ANSI = re.compile(r"\x1b\[([0-9;]*)m")
PALETTE = {
    "31": (240, 110, 110), "32": (136, 214, 120), "33": (240, 190, 100), "34": (122, 178, 247),
    "35": (199, 146, 234), "36": (110, 200, 220), "37": (214, 218, 226),
    "91": (255, 120, 120), "92": (136, 220, 120), "93": (245, 200, 100), "94": (122, 178, 247),
    "95": (214, 140, 245), "96": (120, 214, 230),
}


def ansi_segments(line: str):
    """Split a line with ANSI colour codes into (text, colour, bold) segments."""
    out, pos, colour, bold, dim = [], 0, FG, False, False
    for m in ANSI.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], DIM if dim and colour == FG else colour, bold))
        for code in (m.group(1) or "0").split(";"):
            if code in ("0", ""):
                colour, bold, dim = FG, False, False
            elif code == "1":
                bold = True
            elif code == "2":
                dim = True
            elif code in PALETTE:
                colour = PALETTE[code]
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], DIM if dim and colour == FG else colour, bold))
    return out


def render(text: str, path: str, title: str, scale: int = 2, size: int = 14, min_cols: int = 0) -> None:
    has_ansi = "\x1b[" in text
    lines = text.rstrip("\n").split("\n")
    font = ImageFont.truetype(FONT, size * scale)
    bold = ImageFont.truetype(FONT_BOLD, size * scale)
    cw = font.getlength("M")
    lh = int(size * scale * 1.45)
    pad, bar = 22 * scale, 34 * scale
    cols = max([len(ANSI.sub("", l)) for l in lines + [title]] + [min_cols])
    w = int(cols * cw + 2 * pad)
    h = bar + pad + (len(lines) + 1) * lh + pad

    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, bar], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx, cy, r = pad + i * 20 * scale, bar // 2, 6 * scale
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    d.text((w // 2, bar // 2), title, font=font, fill=DIM, anchor="mm")

    y = bar + pad
    d.text((pad, y), "$ ", font=bold, fill=PROMPT)
    d.text((pad + 2 * cw, y), title, font=bold, fill=FG)
    y += lh
    for i, line in enumerate(lines):
        nxt = ANSI.sub("", lines[i + 1]) if i + 1 < len(lines) else ""
        heading = bool(line.strip()) and set(nxt.strip()) == {"="}
        header_row = bool(line.strip()) and nxt.startswith("---")
        x = pad
        segs = ansi_segments(line) if has_ansi else segments(line, heading, header_row)
        for seg, colour, is_bold in segs:
            d.text((x, y), seg, font=bold if is_bold else font, fill=colour)
            x += cw * len(seg)
        y += lh

    # rounded corners
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=12 * scale, fill=255)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    out.save(path, optimize=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--title", default="fpl-coach analyze 1234567")
    ap.add_argument("--cols", type=int, default=0, help="Minimum width in characters (keeps images the same scale)")
    a = ap.parse_args()
    render(sys.stdin.read(), a.out, a.title, min_cols=a.cols)
