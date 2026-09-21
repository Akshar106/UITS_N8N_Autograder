"""Turn a submitted file into the text the grader reads.

The job here is not only to get text out, but to know when text was *missed*.
A student who answers everything inside a Word table used to extract as their
name and nothing else -- graded, scored zero, with a confident justification and
no sign anything had gone wrong. A silent partial extraction is worse than a
loud failure, so every reader reports what it saw as well as what it read, and
the caller can refuse to grade a file that looks like it lost content.

Supported: .pdf, .docx, .txt, .ipynb, .py. Everything else is refused by name.
"""
import html
import io
import json
import logging
import re
from dataclasses import dataclass, field

import pdfplumber
from charset_normalizer import from_bytes
from docx import Document

logger = logging.getLogger(__name__)

# WordprocessingML namespaces. The 'm' one is the equation editor: Big-O written
# with it lives in <m:t> elements that a plain <w:t> sweep never sees.
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_NS = {"w": _W, "m": _M}

# A page of real prose is hundreds of characters. Well under a hundred per page,
# with images on those pages, is the signature of a scan rather than a document.
_PDF_CHARS_PER_PAGE_FLOOR = 60
# Body text below this, in a file that also holds pictures, reads as "we missed
# something" rather than "the student wrote very little".
_DOCX_SHORT_BODY = 200
# Headers and footers are usually a name and a page number, which is noise in a
# rubric prompt. They are only worth including when the body came back nearly
# empty and they might be where the answers actually are.
_HEADER_FALLBACK_THRESHOLD = 120
_PAGE_NUMBER_RE = re.compile(r"^(page\s*)?\d+(\s*(of|/)\s*\d+)?$", re.IGNORECASE)

SUPPORTED_EXTENSIONS = ("pdf", "docx", "txt", "ipynb", "py")

# Formats students submit by accident. Naming the fix is worth more than
# "Unsupported file type": the student can act on it, a stack trace they cannot.
_KNOWN_UNSUPPORTED = {
    "doc": "This is the older Word format. Open it in Word and use File > Save As "
           "to save a .docx, then resubmit.",
    "pages": "Apple Pages files cannot be read. In Pages use File > Export To > Word "
             "to produce a .docx, then resubmit.",
    "odt": "OpenDocument files cannot be read. Save as .docx or .pdf and resubmit.",
    "rtf": "Rich Text files cannot be read. Save as .docx or .pdf and resubmit.",
    "zip": "Archives cannot be read. Please upload the notebook or document "
           "itself rather than a zip of it.",
    "html": "This looks like a notebook exported to HTML. Please submit the .ipynb "
            "file itself, so the grader can read each cell and its output.",
    "r": "R scripts cannot be read. This assignment expects a Python notebook "
         "(.ipynb) or script (.py).",
    "png": "Images cannot be read; the grader works from text. Please submit your "
           "written answers as a .pdf, .docx or .txt.",
    "jpg": "Images cannot be read; the grader works from text. Please submit your "
           "written answers as a .pdf, .docx or .txt.",
    "jpeg": "Images cannot be read; the grader works from text. Please submit your "
            "written answers as a .pdf, .docx or .txt.",
    "heic": "Images cannot be read; the grader works from text. Please submit your "
            "written answers as a .pdf, .docx or .txt.",
}


class ExtractionError(Exception):
    """Nothing usable could be read. The submission must not be graded."""


@dataclass
class Extracted:
    """Text, plus everything needed to judge whether that text is complete."""

    text: str = ""
    # Human-readable warnings, shown to faculty next to the grade.
    notes: list = field(default_factory=list)
    # What the reader saw: pages, tables, images, equations, characters.
    stats: dict = field(default_factory=dict)

    @property
    def chars(self):
        return len(self.text.strip())

    @property
    def looks_incomplete(self):
        """True when the evidence suggests content was lost, not merely absent.

        This is the distinction that matters. A near-empty answer from a student
        who wrote two lines must still be graded, and score accordingly. A
        near-empty *extraction* from a file that visibly contains pictures or
        tables is a reading failure, and grading it invents a zero.
        """
        return bool(self.stats.get("incomplete"))


def _ext(filename):
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def extract_document(filename, content_bytes):
    """Read a submission. Raises ExtractionError when nothing usable came out."""
    ext = _ext(filename)
    if ext in _KNOWN_UNSUPPORTED:
        raise ExtractionError(_KNOWN_UNSUPPORTED[ext])
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(
            f"Unsupported file type: .{ext or 'unknown'}. Accepted formats are "
            f".pdf, .docx, .txt, .ipynb and .py."
        )
    if not content_bytes:
        raise ExtractionError("The file is empty.")

    reader = {"pdf": _read_pdf, "docx": _read_docx, "txt": _read_txt,
              "ipynb": _read_ipynb, "py": _read_txt}[ext]
    result = reader(content_bytes)
    result.text = _tidy(result.text)
    result.stats["chars"] = result.chars

    if not result.chars:
        raise ExtractionError(
            result.notes[0] if result.notes else "No text could be read from this file."
        )
    return result


def extract_text(filename, content_bytes):
    """Text only, for callers that have no use for the completeness signals."""
    return extract_document(filename, content_bytes).text


def _tidy(text):
    # Collapse the runs of blank lines that table and text-box extraction leave
    # behind, and strip trailing spaces, without touching paragraph structure --
    # the grader uses blank lines to tell one answer from the next.
    text = re.sub(r"[ \t]+(\n)", r"\1", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --- .docx --------------------------------------------------------------------

def _docx_paragraph_text(paragraph_el):
    """All text in a paragraph, in document order.

    Deliberately an XPath over descendants rather than python-docx's
    ``paragraph.text``: that property reads only direct runs, so it drops
    hyperlink labels, equation content, and anything inside a text box. Taking
    every <w:t> and <m:t> below the paragraph picks those up exactly once and in
    the order they appear on the page.
    """
    pieces = []
    # w:br and w:tab are included in the sweep so a manual line break inside a
    # paragraph survives as a line break instead of fusing two lines together.
    for node in paragraph_el.xpath("(.//w:t | .//m:t | .//w:br | .//w:tab)"):
        tag = node.tag
        if tag == f"{{{_W}}}br":
            pieces.append("\n")
        elif tag == f"{{{_W}}}tab":
            pieces.append("\t")
        else:
            pieces.append(node.text or "")
    return "".join(pieces).strip()


def _docx_table_text(table_el, stats, depth=0):
    """Render a table as one line per row, cells joined by ' | '.

    Nested tables recurse. Cell contents keep their own order, so a table used
    as an answer layout -- question in the left cell, answer in the right --
    reads the way a person would read it.
    """
    if depth > 4:
        return ""  # pathological nesting; stop rather than recurse forever
    stats["tables"] = stats.get("tables", 0) + 1

    lines = []
    for row in table_el.xpath("./w:tr"):
        cells = []
        for cell in row.xpath("./w:tc"):
            cells.append(_docx_block_text(cell, stats, depth + 1))
        cells = [c.strip() for c in cells]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _docx_block_text(container_el, stats, depth=0):
    """Walk a container's children in document order.

    Order is the point. Concatenating all paragraphs and then all tables would
    scramble a document that alternates between them, and a scrambled answer is
    a mis-graded answer.
    """
    parts = []
    for child in container_el.iterchildren():
        tag = child.tag
        if tag == f"{{{_W}}}p":
            text = _docx_paragraph_text(child)
            if text:
                parts.append(text)
        elif tag == f"{{{_W}}}tbl":
            table = _docx_table_text(child, stats, depth)
            if table:
                parts.append(table)
        elif tag == f"{{{_W}}}sdt":
            # Content control (a form field or template placeholder): the real
            # content sits inside w:sdtContent.
            for content in child.xpath("./w:sdtContent"):
                nested = _docx_block_text(content, stats, depth)
                if nested:
                    parts.append(nested)
    return "\n\n".join(parts)


def _docx_part_text(document, part_name):
    """Text from a related part (footnotes, endnotes) if the document has one."""
    try:
        part = next(
            rel.target_part for rel in document.part.rels.values()
            if rel.reltype.endswith(part_name)
        )
    except (StopIteration, AttributeError):
        return ""
    try:
        from lxml import etree
        root = etree.fromstring(part.blob)
    except Exception:  # noqa: BLE001 -- a malformed sub-part must not fail the file
        return ""
    nodes = root.xpath("(.//w:t | .//m:t)", namespaces=_NS)
    return " ".join((n.text or "").strip() for n in nodes if (n.text or "").strip())


def _docx_header_footer_text(document):
    lines = []
    for section in document.sections:
        for area in (section.header, section.footer):
            if area is None:
                continue
            for paragraph in area.paragraphs:
                text = paragraph.text.strip()
                # A page number is not an answer.
                if text and not _PAGE_NUMBER_RE.match(text):
                    lines.append(text)
    return "\n".join(lines)


def _read_docx(content_bytes):
    try:
        document = Document(io.BytesIO(content_bytes))
    except Exception as exc:  # noqa: BLE001 -- python-docx raises several unrelated types
        raise ExtractionError(
            "This .docx file could not be opened; it may be corrupted or may not "
            "really be a Word file."
        ) from exc

    result = Extracted()
    stats = result.stats
    body = document.element.body
    text = _docx_block_text(body, stats)

    equations = len(body.xpath(".//m:oMath"))
    text_boxes = len(body.xpath(".//w:txbxContent"))
    images = len(body.xpath(".//*[local-name()='blip' or local-name()='imagedata']"))
    # Charts and SmartArt carry no <a:blip>, so they need counting separately;
    # both are content the grader cannot read.
    charts = len(body.xpath(".//*[local-name()='chart' or local-name()='dgm']"))
    stats.update({
        "tables": stats.get("tables", 0),
        "equations": equations,
        "text_boxes": text_boxes,
        "images": images,
        "charts": charts,
    })

    for part_name, label in (("footnotes", "Footnotes"), ("endnotes", "Endnotes")):
        extra = _docx_part_text(document, part_name)
        if extra:
            text = f"{text}\n\n[{label}] {extra}"

    # Headers and footers are noise in the normal case, so they are a fallback
    # only: worth reaching for when the body came back nearly empty, because
    # then they might be where the answers are.
    if len(text.strip()) < _HEADER_FALLBACK_THRESHOLD:
        header_text = _docx_header_footer_text(document)
        if header_text:
            text = f"{text}\n\n[Header/footer] {header_text}".strip()
            result.notes.append(
                "The body of this document was nearly empty, so text from its "
                "header/footer was included."
            )

    result.text = text

    if images:
        result.notes.append(
            f"This document contains {images} image{'s' if images != 1 else ''}, which "
            f"the grader cannot read. Anything answered inside them was not graded."
        )
    if equations:
        result.notes.append(
            f"{equations} equation{'s' if equations != 1 else ''} were read as plain "
            f"text, so notation may be imprecise."
        )

    if charts:
        result.notes.append(
            f"This document contains {charts} chart or diagram object"
            f"{'s' if charts != 1 else ''}, which the grader cannot read."
        )

    # The signal that matters: little text in a document that visibly holds
    # content we cannot read. Tables are deliberately NOT part of this test --
    # they are read now, so a short table-formatted answer is a short answer,
    # not a failure, and flagging it would be noise.
    if len(text.strip()) < _DOCX_SHORT_BODY and (images or charts):
        stats["incomplete"] = True
        result.notes.append(
            "Very little readable text was found in a document that does contain "
            "images or diagrams -- this may be an extraction failure rather than a "
            "short answer. Please open the file before accepting this grade."
        )
    return result


# --- .pdf ---------------------------------------------------------------------

def _read_pdf(content_bytes):
    result = Extracted()
    try:
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            pages, images, tables = [], 0, 0
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
                images += len(page.images)
                # Counted, not extracted again: pdfplumber's page text already
                # contains the cell text, so re-adding it would duplicate every
                # table in the prompt.
                try:
                    tables += len(page.find_tables())
                except Exception:  # noqa: BLE001 -- table finding is best-effort
                    pass
            page_count = len(pdf.pages)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 -- pdfminer raises a wide range
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise ExtractionError(
                "This PDF is password-protected and cannot be read. Please remove "
                "the password and resubmit."
            ) from exc
        raise ExtractionError(
            "This PDF could not be read; it may be corrupted or may not really be a PDF."
        ) from exc

    result.text = "\n\n".join(p for p in pages if p.strip())
    result.stats.update({"pages": page_count, "images": images, "tables": tables})

    chars = len(result.text.strip())
    scarce_text = bool(page_count) and chars < _PDF_CHARS_PER_PAGE_FLOOR * page_count

    if images and scarce_text:
        # Almost certainly a scan or a photo exported to PDF: the pages carry
        # pictures of words, and there is no text layer to read at all.
        result.stats["incomplete"] = True
        result.notes.append(
            f"This PDF has {page_count} page{'s' if page_count != 1 else ''} but "
            f"almost no machine-readable text, and does contain images. It is "
            f"most likely a scan or photographs, which the grader cannot read."
        )
    elif images:
        # Readable text AND pictures: the answers were graded, but a diagram or
        # screenshot among them was not. Saying so is the whole point of these
        # notes -- silently grading two thirds of a submission is how a student
        # loses marks for work they actually did.
        result.notes.append(
            f"This PDF contains {images} image{'s' if images != 1 else ''}, which the "
            f"grader cannot read. Anything answered inside them was not graded."
        )
    elif scarce_text:
        result.notes.append("This PDF contains very little readable text.")

    if tables:
        result.notes.append(
            f"{tables} table{'s' if tables != 1 else ''} detected; text inside tables "
            f"may not read in the intended order."
        )
    return result


# --- .ipynb -------------------------------------------------------------------

# A single cell can print a 7,000-row dataframe. The grader needs to see that the
# cell ran and what it produced, not the whole frame, and a rubric prompt drowned
# in output is a rubric prompt that misses the next question.
_OUTPUT_LINES = 30
_OUTPUT_CHARS = 2000
# Tracebacks arrive wrapped in terminal colour codes, which are noise to a reader
# that is not a terminal.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _joined(source):
    """nbformat stores source and text either as a string or a list of lines."""
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source or "")


def _clip(text):
    text = _ANSI_RE.sub("", text).strip("\n")
    if not text:
        return ""
    lines = text.split("\n")
    clipped = False
    if len(lines) > _OUTPUT_LINES:
        lines, clipped = lines[:_OUTPUT_LINES], True
    text = "\n".join(lines)
    if len(text) > _OUTPUT_CHARS:
        text, clipped = text[:_OUTPUT_CHARS], True
    if clipped:
        text += "\n[output truncated]"
    return text


def _render_outputs(outputs, stats):
    """Turn a cell's outputs into text, recording what could not be rendered."""
    blocks = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        kind = output.get("output_type")

        if kind == "error":
            stats["errors"] = stats.get("errors", 0) + 1
            trace = "\n".join(_joined(line) for line in (output.get("traceback") or []))
            detail = _clip(trace) or f"{output.get('ename')}: {output.get('evalue')}"
            blocks.append(("ERROR", detail))
            continue

        if kind == "stream":
            blocks.append(("output", _clip(_joined(output.get("text")))))
            continue

        data = output.get("data") or {}
        # A plot is the point of several of these steps, and it comes back as a
        # PNG. We cannot read it -- but we can say it exists, which lets the
        # grader credit "the code plots a heatmap and a heatmap came out" without
        # anyone pretending to have looked at the picture.
        images = [mime for mime in data if mime.startswith("image/")]
        if images:
            stats["images"] = stats.get("images", 0) + len(images)
            blocks.append((f"output: {images[0]} (a figure was produced; "
                           f"the grader cannot see images)", ""))
        if "text/plain" in data:
            blocks.append(("output", _clip(_joined(data["text/plain"]))))
        elif not images and data:
            blocks.append((f"output: {sorted(data)[0]}", ""))

    return blocks


def _read_ipynb(content_bytes):
    """Read a Jupyter notebook as an ordered transcript of cells and outputs.

    Code alone does not answer "did the student do this step". A cell that was
    never run, or that ended in a traceback, looks identical to a working one if
    you only read the source -- so the transcript carries the execution count,
    the output, and the error, and the grader is told which is which.
    """
    text = _read_txt(content_bytes).text
    try:
        notebook = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            "This .ipynb file is not valid notebook JSON and could not be opened. "
            "Re-save it from Jupyter and resubmit."
        ) from exc

    if not isinstance(notebook, dict):
        raise ExtractionError("This .ipynb file does not contain a notebook.")

    cells = notebook.get("cells")
    if cells is None and notebook.get("worksheets"):
        # nbformat 3, still occasionally exported by old tooling.
        cells = [c for sheet in notebook["worksheets"] for c in (sheet.get("cells") or [])]
    if not isinstance(cells, list) or not cells:
        raise ExtractionError("This notebook contains no cells.")

    result = Extracted()
    stats = result.stats
    stats.update(cells=len(cells), code_cells=0, unexecuted=0, errors=0, images=0)
    parts = []

    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        kind = cell.get("cell_type", "code")
        source = _joined(cell.get("source")).rstrip()

        if kind != "code":
            if source:
                parts.append(f"--- cell {index} ({kind}) ---\n{source}")
            continue

        stats["code_cells"] += 1
        count = cell.get("execution_count")
        outputs = [o for o in (cell.get("outputs") or []) if isinstance(o, dict)]
        if count is None and not outputs:
            stats["unexecuted"] += 1
            header = f"--- cell {index} (code, NEVER RUN -- no output) ---"
        else:
            ran_as = f"In[{count}]" if count is not None else "In[?]"
            header = f"--- cell {index} (code, ran as {ran_as}) ---"

        block = [header, source] if source else [header, "(empty cell)"]
        for label, body in _render_outputs(outputs, stats):
            block.append(f"--- {label} ---")
            if body:
                block.append(body)
        parts.append("\n".join(block))

    result.text = "\n\n".join(parts)

    if stats["unexecuted"]:
        result.notes.append(
            f"{stats['unexecuted']} of {stats['code_cells']} code cells were never "
            f"run, so there is no output to show they work."
        )
    if stats["errors"]:
        result.notes.append(
            f"{stats['errors']} cell(s) ended in an error when the student ran them."
        )
    if stats["images"]:
        result.notes.append(
            f"{stats['images']} figure(s) in the output could not be read. Plots were "
            f"judged from the code that drew them and the fact that a figure appeared."
        )
    # A notebook of nothing but prose is a student who pasted their write-up into
    # markdown cells and never wrote code. That is a grade of its own, not a
    # reading failure, so it is reported rather than refused.
    if not stats["code_cells"]:
        result.notes.append("This notebook contains no code cells.")
    return result


# --- .txt ---------------------------------------------------------------------

def _read_txt(content_bytes):
    """Decode without assuming UTF-8.

    Students submit files written on Windows in cp1252 and exported from Word as
    UTF-16, and a blind ``decode('utf-8', errors='replace')`` turns those into a
    wall of replacement characters that the grader reads as gibberish and marks
    wrong.
    """
    result = Extracted()

    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if content_bytes.startswith(bom):
            result.text = content_bytes.decode(encoding, errors="replace")
            result.stats["encoding"] = encoding
            return result

    try:
        result.text = content_bytes.decode("utf-8")
        result.stats["encoding"] = "utf-8"
        return result
    except UnicodeDecodeError:
        pass

    best = from_bytes(content_bytes).best()
    if best is not None:
        result.text = str(best)
        result.stats["encoding"] = best.encoding
        result.notes.append(
            f"This file is not UTF-8; it was read as {best.encoding}. Unusual "
            f"characters may be wrong."
        )
        return result

    result.text = content_bytes.decode("utf-8", errors="replace")
    result.stats["encoding"] = "utf-8 (with replacements)"
    result.notes.append(
        "The text encoding of this file could not be identified, so some "
        "characters may be wrong."
    )
    return result


def strip_html(html_text):
    """Plain text from a Canvas text-entry submission.

    Block-level tags become newlines before the rest are dropped, so paragraphs
    and list items stay separated instead of running into one another, and
    entities are decoded so the grader reads &amp;lt; as '<'.
    """
    text = html_text or ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote)\s*>", "\n\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", " | ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
