#!/usr/bin/env python3
"""Render an assignment's source text as a PDF.

    pip install reportlab
    python3 scripts/make_pdfs.py path/to/assignment.txt [path/to/rubric.txt ...]

The text files beside the PDFs are the source of truth -- edit those and re-run
this. Keeping the prose in plain text rather than only in a binary means the
assignment can be diffed, and means the grader reads the same words the student
does, since the app extracts these files back to text anyway.

Understands a small subset of Markdown: # / ## / ### headings, fenced code
blocks, bullet and numbered lists, > callouts, pipe tables, --- rules, and
inline **bold**, *italic* and `code`.
"""
import html
import re
import sys
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (HRFlowable, ListFlowable, ListItem, PageBreak,
                                    Paragraph, Preformatted, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
except ImportError:
    sys.exit("reportlab is not installed. Run: pip install reportlab")

REPO_ROOT = Path(__file__).resolve().parent.parent

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5a5a5a")
RULE = colors.HexColor("#c8c8c8")
CODE_BG = colors.HexColor("#f4f4f6")
CALLOUT_BG = colors.HexColor("#fdf6e3")


def styles():
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "body", parent=base["BodyText"], fontName="Times-Roman", fontSize=10.5,
        leading=14.5, spaceBefore=0, spaceAfter=7, alignment=TA_LEFT, textColor=INK)
    return {
        "body": body,
        "title": ParagraphStyle("title", parent=body, fontName="Helvetica-Bold",
                                fontSize=19, leading=23, spaceAfter=2, spaceBefore=0),
        "h1": ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold",
                             fontSize=13.5, leading=17, spaceBefore=16, spaceAfter=7,
                             keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold",
                             fontSize=11.5, leading=15, spaceBefore=11, spaceAfter=5,
                             keepWithNext=1),
        "code": ParagraphStyle("code", parent=body, fontName="Courier",
                               fontSize=8.6, leading=11.2, textColor=INK,
                               backColor=CODE_BG, borderPadding=7,
                               leftIndent=5, spaceBefore=4, spaceAfter=9),
        "callout": ParagraphStyle("callout", parent=body, fontName="Times-Italic",
                                  backColor=CALLOUT_BG, borderPadding=9,
                                  leftIndent=4, rightIndent=4,
                                  spaceBefore=6, spaceAfter=10),
        "bullet": ParagraphStyle("bullet", parent=body, spaceAfter=3),
        "cell": ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12.5,
                               spaceAfter=0),
    }


# The built-in PDF fonts (Times, Helvetica, Courier) are drawn with WinAnsi
# encoding (cp1252) and nothing else, and
# reportlab silently substitutes a wrong glyph for anything outside it -- an
# arrow came out as "fi". The source text keeps the real characters, because the
# app reads those files as text and Unicode is fine there; only the PDF gets
# these substitutions. Anything missed is caught by the assertion below rather
# than shipped as a wrong glyph.
_ASCII_FALLBACK = {
    "→": "->", "←": "<-", "≤": "<=", "≥": ">=",
    "≈": "~", "−": "-", "×": "x", "≡": "==",
    "Θ": "Theta", "α": "alpha", "π": "pi", "β": "beta",
    "λ": "lambda", "Ω": "Omega", "θ": "theta",
    "⁰": "^0", "¹": "^1", "⁴": "^4", "⁵": "^5",
    "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
    "₀": "_0", "₁": "_1", "₂": "_2", "∞": "infinity",
}


def to_pdf_charset(text):
    for source, replacement in _ASCII_FALLBACK.items():
        text = text.replace(source, replacement)
    try:
        text.encode("cp1252")
    except UnicodeEncodeError as exc:
        bad = text[exc.start:exc.end]
        raise SystemExit(
            f"character U+{ord(bad[0]):04X} ({bad!r}) is outside WinAnsi and "
            f"would render as the wrong glyph. Add it to _ASCII_FALLBACK."
        ) from exc
    return text


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S)
_CODE = re.compile(r"`([^`]+)`")


def inline(text):
    """Markdown emphasis to reportlab markup, escaping the text first."""
    out = html.escape(to_pdf_charset(text), quote=False)
    # Code first: its contents must not then be read as emphasis.
    out = _CODE.sub(lambda m: f'<font face="Courier" size="9.3">{m.group(1)}</font>', out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    return out


def table_flowable(rows, style):
    data = [[Paragraph(inline(cell), style["cell"]) for cell in row] for row in rows]
    table = Table(data, hAlign="LEFT", colWidths=None, repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, RULE),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, colors.HexColor("#e8e8e8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def is_divider(row):
    return all(re.fullmatch(r":?-{2,}:?", cell.strip() or "-") for cell in row)


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(text, style):
    """Markdown subset to a list of flowables."""
    flow = []
    lines = text.split("\n")
    i = 0
    pending, bullets = [], []

    def flush_paragraph():
        if pending:
            flow.append(Paragraph(inline(" ".join(pending)), style["body"]))
            pending.clear()

    def flush_bullets():
        if bullets:
            flow.append(ListFlowable(
                [ListItem(Paragraph(inline(b), style["bullet"]), leftIndent=16)
                 for b in bullets],
                bulletType="bullet", bulletFontName="Helvetica", bulletFontSize=6,
                bulletOffsetY=-3, leftIndent=14, spaceBefore=2, spaceAfter=8))
            bullets.clear()

    def flush():
        flush_paragraph()
        flush_bullets()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush()
            i += 1
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            flow.append(Preformatted(to_pdf_charset("\n".join(block)), style["code"]))
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and is_divider(split_row(lines[i + 1])):
            flush()
            rows = [split_row(lines[i])]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            flow.append(table_flowable(rows, style))
            flow.append(Spacer(1, 9))
            continue

        if stripped.startswith("> "):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            flow.append(Paragraph(inline(" ".join(quote)), style["callout"]))
            continue

        if not stripped:
            flush()
        elif stripped == "---":
            flush()
            flow.append(Spacer(1, 4))
            flow.append(HRFlowable(width="100%", thickness=0.6, color=RULE,
                                   spaceBefore=2, spaceAfter=12))
        elif stripped.startswith("### "):
            flush()
            flow.append(Paragraph(inline(stripped[4:]), style["h2"]))
        elif stripped.startswith("## "):
            flush()
            flow.append(Paragraph(inline(stripped[3:]), style["h1"]))
        elif stripped.startswith("# "):
            flush()
            flow.append(Paragraph(inline(stripped[2:]), style["title"]))
        elif re.match(r"^[-*] ", stripped):
            flush_paragraph()
            bullets.append(stripped[2:])
        elif re.match(r"^\d+\. ", stripped):
            flush_paragraph()
            bullets.append(re.sub(r"^\d+\.\s*", "", stripped))
        elif bullets:
            # A wrapped continuation of the list item above. Without this the
            # line becomes its own paragraph and any emphasis spanning the wrap
            # is split in half, printing the literal ** markers.
            bullets[-1] += " " + stripped
        else:
            pending.append(stripped)
        i += 1

    flush()
    return flow


def footer(canvas, doc, label):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.9 * inch, 0.6 * inch, label)
    canvas.drawRightString(LETTER[0] - 0.9 * inch, 0.6 * inch, f"Page {doc.page}")
    canvas.restoreState()


def build(source, target, label):
    style = styles()
    doc = SimpleDocTemplate(
        str(target), pagesize=LETTER, title=label, author="",
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch)
    story = render(source.read_text(), style)
    doc.build(story, onFirstPage=lambda c, d: footer(c, d, label),
              onLaterPages=lambda c, d: footer(c, d, label))
    print(f"  {target.relative_to(REPO_ROOT)}  ({target.stat().st_size // 1024} KB, "
          f"{doc.page} pages)")


def title_of(source):
    """The document's own '# ' heading, which is what belongs in the footer."""
    for line in source.read_text().split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return source.stem.replace("_", " ").title()


def main():
    if len(sys.argv) < 2:
        print("usage: make_pdfs.py path/to/source.txt [...]", file=sys.stderr)
        return 2
    jobs = []
    for argument in sys.argv[1:]:
        source = Path(argument)
        source = source if source.is_absolute() else REPO_ROOT / source
        jobs.append((source, source.with_suffix(".pdf"), title_of(source)))

    for source, target, label in jobs:
        if not source.exists():
            print(f"  missing {source}", file=sys.stderr)
            return 1
        build(source, target, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
