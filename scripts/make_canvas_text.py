#!/usr/bin/env python3
"""Convert an assignment's source text into forms Canvas accepts.

    python3 scripts/make_canvas_text.py [source.txt ...]

Canvas's editor does not understand Markdown. Pasting the source file straight
in prints the asterisks and hashes literally, so this writes two siblings of
each source file:

  *_canvas.txt   plain text with the markup resolved -- paste into the editor
  *_canvas.html  semantic HTML -- paste into the editor's HTML view, which is
                 the only way code blocks keep their indentation and monospace

Defaults to the Bike-Share assignment when given no arguments.
"""
import html
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT = REPO_ROOT / "sample_data/assignments/dsa_bikeshare/assignment.txt"

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S)
_CODE = re.compile(r"`([^`]+)`")


def strip_markup(text):
    """Drop the emphasis markers, keeping the words."""
    return _ITALIC.sub(r"\1", _BOLD.sub(r"\1", _CODE.sub(r"\1", text)))


def inline_html(text):
    out = _CODE.sub(lambda m: f"<code>{html.escape(m.group(1))}</code>",
                    html.escape(text, quote=False))
    return _ITALIC.sub(r"<em>\1</em>", _BOLD.sub(r"<strong>\1</strong>", out))


def blocks(text):
    """Walk the source once, yielding (kind, payload) for both renderers."""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith("```"):
            i += 1
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            yield "code", "\n".join(body)
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].strip()) <= set("|-: "):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                    rows.append(cells)
                i += 1
            yield "table", rows
            continue

        if not stripped:
            i += 1
            continue
        if stripped == "---":
            yield "rule", ""
        elif stripped.startswith("### "):
            yield "h3", stripped[4:]
        elif stripped.startswith("## "):
            yield "h2", stripped[3:]
        elif stripped.startswith("# "):
            yield "h1", stripped[2:]
        elif re.match(r"^[-*] ", stripped) or re.match(r"^\d+\. ", stripped):
            items = []
            ordered = bool(re.match(r"^\d+\. ", stripped))
            while i < len(lines):
                current = lines[i].strip()
                if re.match(r"^[-*] ", current):
                    items.append(current[2:])
                elif re.match(r"^\d+\. ", current):
                    items.append(re.sub(r"^\d+\.\s*", "", current))
                elif current and items and not current.startswith(("#", "|", "`", ">", "---")):
                    items[-1] += " " + current        # a wrapped list item
                else:
                    break
                i += 1
            yield ("ol" if ordered else "ul"), items
            continue
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(
                    r"^(#{1,3} |[-*] |\d+\. |\||```|>|---$)", lines[i].strip()):
                para.append(lines[i].strip())
                i += 1
            yield "p", " ".join(para)
            continue
        i += 1


def to_text(source):
    out = []
    for kind, payload in blocks(source):
        if kind == "code":
            out.append(payload)
            out.append("")
        elif kind == "table":
            widths = [max(len(r[c]) for r in payload) for c in range(len(payload[0]))]
            for index, row in enumerate(payload):
                out.append("  ".join(strip_markup(cell).ljust(widths[c])
                                     for c, cell in enumerate(row)).rstrip())
                if index == 0:
                    out.append("  ".join("-" * w for w in widths))
            out.append("")
        elif kind == "rule":
            out.append("-" * 72)
            out.append("")
        elif kind in ("h1", "h2", "h3"):
            title = strip_markup(payload)
            out.append(title.upper() if kind == "h1" else title)
            if kind == "h1":
                out.append("=" * len(title))
            out.append("")
        elif kind in ("ul", "ol"):
            for index, item in enumerate(payload, start=1):
                marker = f"{index}. " if kind == "ol" else "- "
                out.append(marker + strip_markup(item))
            out.append("")
        else:
            out.append(strip_markup(payload))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_html(source):
    out = []
    for kind, payload in blocks(source):
        if kind == "code":
            out.append(f"<pre><code>{html.escape(payload)}</code></pre>")
        elif kind == "table":
            rows = ["<tr>" + "".join(f"<th>{inline_html(c)}</th>" for c in payload[0]) + "</tr>"]
            rows += ["<tr>" + "".join(f"<td>{inline_html(c)}</td>" for c in row) + "</tr>"
                     for row in payload[1:]]
            out.append('<table border="1" cellpadding="6" cellspacing="0">'
                       + "".join(rows) + "</table>")
        elif kind == "rule":
            out.append("<hr>")
        elif kind in ("h1", "h2", "h3"):
            tag = {"h1": "h2", "h2": "h3", "h3": "h4"}[kind]   # Canvas owns <h1>
            out.append(f"<{tag}>{inline_html(payload)}</{tag}>")
        elif kind in ("ul", "ol"):
            items = "".join(f"<li>{inline_html(item)}</li>" for item in payload)
            out.append(f"<{kind}>{items}</{kind}>")
        else:
            out.append(f"<p>{inline_html(payload)}</p>")
    return "\n".join(out) + "\n"


def main():
    sources = [Path(a) for a in sys.argv[1:]] or [DEFAULT]
    for source in sources:
        source = source if source.is_absolute() else REPO_ROOT / source
        if not source.exists():
            print(f"missing {source}", file=sys.stderr)
            return 1
        text = source.read_text()
        for suffix, convert in ((".txt", to_text), (".html", to_html)):
            target = source.with_name(source.stem + "_canvas" + suffix)
            target.write_text(convert(text))
            print(f"  {target.relative_to(REPO_ROOT)}  ({len(target.read_text())} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
