#!/usr/bin/env python3
"""Checks that a submitted file is read completely -- or reported as incomplete.

Builds real .docx, .pdf and .txt files in memory and runs them through the same
extractor the grading job uses. No network, no API key:

    python3 scripts/test_extraction.py

Every fixture is built in memory, so this needs no files on disk.

The case that motivates all of this: a student who answers inside a Word table
used to extract as their name alone, get graded, and score zero with a confident
justification. Nothing anywhere said extraction had failed. Every check here
exists to make sure that class of silent wrong grade cannot happen again.
"""
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document  # noqa: E402
from docx.shared import Inches  # noqa: E402

from app import extract  # noqa: E402

FAILS = []

ANSWER_1 = "A BST keeps left subtree values smaller than the node. Average case O(log n)."
ANSWER_2 = "Naive recursion is O(2^n) because it recomputes overlapping subproblems."


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILS.append(label)


def docx_bytes(build):
    document = Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def read(filename, content):
    return extract.extract_document(filename, content)


# --- .docx --------------------------------------------------------------------

def test_docx_tables():
    print("\n-- answers inside a Word table (the silent-zero bug) --")

    def build(d):
        d.add_paragraph("Name: Jordan Lee")
        table = d.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Q1"
        table.cell(0, 1).text = ANSWER_1
        table.cell(1, 0).text = "Q2"
        table.cell(1, 1).text = ANSWER_2

    result = read("table.docx", docx_bytes(build))
    check("a table answer is recovered", ANSWER_1 in result.text and ANSWER_2 in result.text)
    check("the question labels come with it", "Q1" in result.text and "Q2" in result.text)
    check("the table was counted", result.stats["tables"] == 1)
    # A short answer laid out in a table is a short answer, not a read failure.
    check("a fully-read table is not flagged incomplete", not result.looks_incomplete)

    def build_nested(d):
        outer = d.add_table(rows=1, cols=1)
        inner = outer.cell(0, 0).add_table(rows=1, cols=1)
        inner.cell(0, 0).text = ANSWER_1

    nested = read("nested.docx", docx_bytes(build_nested))
    check("a table nested inside a table is recovered", ANSWER_1 in nested.text)


def test_docx_reading_order():
    print("\n-- document order is preserved --")

    def build(d):
        d.add_paragraph("Q1 answer follows.")
        t1 = d.add_table(rows=1, cols=1)
        t1.cell(0, 0).text = ANSWER_1
        d.add_paragraph("Q2 answer follows.")
        t2 = d.add_table(rows=1, cols=1)
        t2.cell(0, 0).text = ANSWER_2

    text = read("order.docx", docx_bytes(build)).text
    positions = [text.index(s) for s in
                 ("Q1 answer follows", ANSWER_1, "Q2 answer follows", ANSWER_2)]
    check("paragraphs and tables interleave in document order",
          positions == sorted(positions))


def test_docx_other_containers():
    print("\n-- text boxes, equations, hyperlinks, list items --")

    def build_list(d):
        d.add_paragraph(ANSWER_1, style="List Bullet")
        d.add_paragraph(ANSWER_2, style="List Number")

    listed = read("list.docx", docx_bytes(build_list))
    check("bulleted and numbered answers are read",
          ANSWER_1 in listed.text and ANSWER_2 in listed.text)

    # A hyperlink's label lives in a w:hyperlink run, which paragraph.text drops.
    def build_link(d):
        p = d.add_paragraph("See ")
        run = p.add_run()
        link = run._element.makeelement(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hyperlink", {})
        inner_run = run._element.makeelement(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r", {})
        text_el = run._element.makeelement(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t", {})
        text_el.text = "merge sort is O(n log n)"
        inner_run.append(text_el)
        link.append(inner_run)
        p._p.append(link)

    linked = read("link.docx", docx_bytes(build_link))
    check("hyperlink text is read", "merge sort is O(n log n)" in linked.text)

    # An equation-editor formula: <m:oMath><m:r><m:t>O(n log n)</m:t>
    def build_math(d):
        p = d.add_paragraph("Complexity: ")
        omath = p._p.makeelement(
            "{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath", {})
        m_r = p._p.makeelement(
            "{http://schemas.openxmlformats.org/officeDocument/2006/math}r", {})
        m_t = p._p.makeelement(
            "{http://schemas.openxmlformats.org/officeDocument/2006/math}t", {})
        m_t.text = "O(n log n)"
        m_r.append(m_t)
        omath.append(m_r)
        p._p.append(omath)

    math = read("math.docx", docx_bytes(build_math))
    check("equation-editor text is read", "O(n log n)" in math.text)
    check("equations are counted", math.stats["equations"] == 1)
    check("a note warns that notation may be imprecise",
          any("equation" in n for n in math.notes))


def test_docx_incomplete_detection():
    print("\n-- an unreadable document is refused, not graded --")

    # A picture of the answers plus a name: the exact shape of a submission that
    # would otherwise score a confident zero.
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
           b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
           b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

    def build(d):
        d.add_paragraph("Name: Jordan Lee")
        d.add_picture(io.BytesIO(png), width=Inches(1))

    result = read("scan.docx", docx_bytes(build))
    check("the image is counted", result.stats["images"] == 1)
    check("an image-only document is flagged incomplete", result.looks_incomplete)
    check("a note names the images", any("image" in n for n in result.notes))

    # The other side of that judgment: a genuinely short answer, no images, must
    # still be graded so it can score what it deserves.
    short = read("short.docx", docx_bytes(lambda d: d.add_paragraph("Q1: A tree.")))
    check("a genuinely short answer is NOT flagged incomplete", not short.looks_incomplete)
    check("the short answer's text is intact", "A tree" in short.text)

    def build_empty(d):
        d.add_paragraph("")

    try:
        read("empty.docx", docx_bytes(build_empty))
        check("an empty document raises", False)
    except extract.ExtractionError:
        check("an empty document raises", True)

    try:
        read("broken.docx", b"this is not a docx at all")
        check("a corrupt docx raises a readable error", False)
    except extract.ExtractionError as exc:
        check("a corrupt docx raises a readable error", "could not be opened" in str(exc))


def test_docx_header_fallback():
    print("\n-- header/footer used only as a fallback --")

    def build_normal(d):
        d.add_paragraph(ANSWER_1)
        d.add_paragraph(ANSWER_2)
        d.sections[0].header.paragraphs[0].text = "Jordan Lee - CSCI 101"

    normal = read("hdr.docx", docx_bytes(build_normal))
    check("a normal document does not pull in header noise",
          "CSCI 101" not in normal.text)

    def build_body_in_header(d):
        d.add_paragraph("Name")
        d.sections[0].header.paragraphs[0].text = ANSWER_1

    fallback = read("hdr2.docx", docx_bytes(build_body_in_header))
    check("a nearly empty body falls back to the header", ANSWER_1 in fallback.text)
    check("the fallback is noted", any("header" in n.lower() for n in fallback.notes))


# --- .pdf ---------------------------------------------------------------------

TINY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
            b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
            b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def pdf_bytes(lines, with_image=False):
    """A minimal single-page PDF with a real text layer, optionally an image too."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdf_canvas

    buffer = io.BytesIO()
    c = pdf_canvas.Canvas(buffer, pagesize=LETTER)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    if with_image:
        c.drawImage(ImageReader(io.BytesIO(TINY_PNG)), 72, 500, width=72, height=72, mask="auto")
    c.save()
    return buffer.getvalue()


def test_pdf():
    print("\n-- .pdf --")
    try:
        import reportlab  # noqa: F401
    except ImportError:
        print("SKIP  reportlab not installed; install it to test PDF extraction")
        return

    result = read("answers.pdf", pdf_bytes([f"Q1: {ANSWER_1}", f"Q2: {ANSWER_2}"]))
    check("pdf text is extracted", ANSWER_1 in result.text and ANSWER_2 in result.text)
    check("the page count is recorded", result.stats["pages"] == 1)
    check("a normal pdf is not flagged incomplete", not result.looks_incomplete)

    multi = read("multi.pdf", pdf_bytes([f"Q{i}: line {i}" for i in range(1, 30)]))
    check("every line of a longer pdf is read",
          all(f"line {i}" in multi.text for i in range(1, 30)))

    # Typed answers plus a diagram: the text grades fine, but the diagram was
    # skipped and the faculty member has to be told so.
    mixed = read("mixed.pdf", pdf_bytes([f"Q{i}: {ANSWER_1}" for i in range(1, 12)],
                                        with_image=True))
    check("an image alongside readable text is counted", mixed.stats["images"] >= 1)
    check("an image alongside readable text is reported",
          any("cannot read" in n for n in mixed.notes))
    check("text plus an image still grades (not refused)", not mixed.looks_incomplete)
    check("the text itself is intact", ANSWER_1 in mixed.text)

    # The other side: an image and almost no text is a scan, and is refused.
    scan = read("scanned.pdf", pdf_bytes(["p1"], with_image=True))
    check("an image with almost no text is refused as a scan", scan.looks_incomplete)

    plain_sparse = read("sparse.pdf", pdf_bytes(["hi"]))
    check("sparse text with no images is noted but still graded",
          not plain_sparse.looks_incomplete
          and any("very little" in n.lower() for n in plain_sparse.notes))

    try:
        read("broken.pdf", b"%PDF-1.4 truncated garbage")
        check("a corrupt pdf raises a readable error", False)
    except extract.ExtractionError as exc:
        check("a corrupt pdf raises a readable error", "could not be read" in str(exc))


# --- .txt ---------------------------------------------------------------------

def test_txt_encodings():
    print("\n-- .txt encodings --")

    plain = read("a.txt", ANSWER_1.encode("utf-8"))
    check("utf-8 is read", plain.text == ANSWER_1)
    check("the encoding is recorded", plain.stats["encoding"] == "utf-8")

    bom = read("b.txt", b"\xef\xbb\xbf" + ANSWER_1.encode("utf-8"))
    check("a utf-8 BOM is stripped, not read as a character",
          bom.text == ANSWER_1 and "﻿" not in bom.text)

    utf16 = read("c.txt", ANSWER_1.encode("utf-16"))
    check("utf-16 (Word's 'save as plain text') is read", utf16.text == ANSWER_1)

    # cp1252 smart quotes -- what Word produces, and not valid UTF-8.
    # cp1252 bytes directly: 0x93/0x94 are smart quotes, 0x96 an en dash.
    windows = b"Q1: the tree is \x93degenerate\x94 \x96 O(n)."
    cp = read("d.txt", windows)
    check("a cp1252 file is decoded rather than mangled",
          "degenerate" in cp.text and "�" not in cp.text)
    check("the non-utf-8 encoding is noted", any("UTF-8" in n for n in cp.notes))

    try:
        read("e.txt", b"")
        check("an empty file raises", False)
    except extract.ExtractionError:
        check("an empty file raises", True)


# --- refusals and text entry ---------------------------------------------------

def notebook(cells, **top):
    payload = {"nbformat": 4, "cells": cells}
    payload.update(top)
    return json.dumps(payload).encode()


def code_cell(source, count=1, outputs=()):
    return {"cell_type": "code", "execution_count": count,
            "source": source, "outputs": list(outputs)}


def test_notebook():
    print("\n-- .ipynb: cells, outputs, and whether the code was ever run --")

    result = read("hw.ipynb", notebook([
        {"cell_type": "markdown", "source": ["# Step 1\n", "Read the data."]},
        code_cell(["import pandas as pd\n", "df = pd.read_csv('f.csv')\n", "df.shape"],
                  count=1,
                  outputs=[{"output_type": "execute_result",
                            "data": {"text/plain": ["(7752, 25)"]}}]),
    ]))
    check("markdown cells are read", "Read the data." in result.text)
    check("code is kept verbatim so it can be quoted as evidence",
          "df = pd.read_csv('f.csv')" in result.text)
    check("the output of a cell is included", "(7752, 25)" in result.text)
    check("an executed cell says so", "ran as In[1]" in result.text)
    check("cells keep notebook order",
          result.text.index("# Step 1") < result.text.index("import pandas"))

    # The distinction the whole reader exists for: written but never run.
    result = read("hw.ipynb", notebook([
        code_cell(["df.drop_duplicates(inplace=True)"], count=None, outputs=[]),
    ]))
    check("a cell that was never run is labelled", "NEVER RUN" in result.text)
    check("a cell that was never run is reported to faculty",
          any("never" in note for note in result.notes))
    check("the code of an unrun cell is still read",
          "df.drop_duplicates(inplace=True)" in result.text)

    result = read("hw.ipynb", notebook([
        code_cell(["df.drop_duplictes()"], count=4, outputs=[{
            "output_type": "error", "ename": "AttributeError", "evalue": "nope",
            "traceback": ["\u001b[0;31mAttributeError\u001b[0m: no attribute 'drop_duplictes'"]}]),
    ]))
    check("a cell that raised is labelled", "--- ERROR ---" in result.text)
    check("the traceback is readable", "no attribute 'drop_duplictes'" in result.text)
    check("terminal colour codes are stripped", "\u001b[" not in result.text)
    check("an error is reported to faculty",
          any("error" in note for note in result.notes))

    # A plot is the deliverable for several steps. We cannot see it, but the
    # fact that one was produced is real evidence and must not be silently lost.
    result = read("hw.ipynb", notebook([
        code_cell(["sns.heatmap(df.corr())"], count=2, outputs=[{
            "output_type": "display_data",
            "data": {"image/png": "iVBORw0KGgo=", "text/plain": ["<Axes: >"]}}]),
    ]))
    check("a figure in the output is recorded", "a figure was produced" in result.text)
    check("the unreadable figure is disclosed to faculty",
          any("could not be read" in note for note in result.notes))
    check("a notebook with figures is not treated as a failed read",
          not result.looks_incomplete)

    # One cell can print seven thousand rows; the next question still has to fit.
    huge = read("hw.ipynb", notebook([
        code_cell(["df"], count=1, outputs=[{"output_type": "stream", "name": "stdout",
                                             "text": ["row %d\n" % i for i in range(500)]}]),
    ]))
    check("a runaway output is truncated", "[output truncated]" in huge.text)
    check("truncation keeps the beginning of the output", "row 0" in huge.text)
    check("truncation is bounded", len(huge.text) < 4000)

    # Jupyter leaves an empty cell at the bottom of almost every notebook. A
    # real submission tripped this: a complete paper carrying a false warning.
    tidy = read("hw.ipynb", notebook([
        code_cell(["df.head()"], count=1, outputs=[{"output_type": "stream",
                                                    "name": "stdout", "text": ["ok"]}]),
        {"cell_type": "code", "execution_count": None, "source": [], "outputs": []},
    ]))
    check("an empty trailing cell is not counted as code", tidy.stats["code_cells"] == 1)
    check("an empty trailing cell raises no warning", tidy.notes == [])

    check("a notebook with no code cells is reported, not refused",
          any("no code cells" in note for note in
              read("hw.ipynb", notebook([{"cell_type": "markdown",
                                          "source": ["just prose"]}])).notes))

    for payload, expected in ((b"not json at all", "not valid notebook JSON"),
                              (notebook([]), "no cells")):
        try:
            read("hw.ipynb", payload)
            check(f"a broken notebook is refused ({expected})", False)
        except extract.ExtractionError as exc:
            check(f"a broken notebook is refused ({expected})", expected in str(exc))

    check("a plain .py script is read as text",
          "import pandas" in read("hw.py", b"import pandas as pd\n").text)


def _png(text="handwritten answer"):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (900, 300), "white")
    ImageDraw.Draw(image).text((30, 30), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _scanned_pdf(pages=2):
    """A PDF whose pages are pictures of words -- no text layer at all."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl

    picture = io.BytesIO(_png())
    out = io.BytesIO()
    pdf = rl.Canvas(out, pagesize=(612, 792))
    for _ in range(pages):
        picture.seek(0)
        pdf.drawImage(ImageReader(picture), 40, 400, width=500, height=250)
        pdf.showPage()
    pdf.save()
    return out.getvalue()


def _with_backend(name, transcript):
    """Reload extract/ocr with a backend selected and the reader stubbed."""
    import importlib
    from unittest import mock
    import app.extract
    import app.ocr

    os.environ["OCR_BACKEND"] = name
    importlib.reload(app.ocr)
    importlib.reload(app.extract)
    reader = lambda image, index, context: app.ocr.PageText(index, transcript)  # noqa: E731
    return app.ocr, app.extract, mock.patch.object(app.ocr, "_read_page_vision", side_effect=reader)


def _restore():
    import importlib
    import app.extract
    import app.ocr
    os.environ.pop("OCR_BACKEND", None)
    importlib.reload(app.ocr)
    importlib.reload(app.extract)


TRANSCRIPT = ("Q1. A BST keps smaller values on the left.\n"
              "Worst case is O(n) when [?unbalanced].\n"
              "[struck: average is O(n^2)]\n"
              "[diagram: a tree with 7 nodes]\n"
              "[illegible]")


def test_handwriting_off_by_default():
    print("\n-- with no reader configured, images are refused, not graded --")
    from app import extract as ex

    check("image formats are not accepted by default",
          "png" not in ex.supported_extensions())
    try:
        ex.extract_document("page.png", _png())
        check("a photograph is refused when reading is off", False)
    except ex.ExtractionError as exc:
        # The refusal must say the feature is OFF, not that it does not exist:
        # a faculty member told "images cannot be read" chases the student for
        # a different file when the fix is a server setting.
        check("a photograph is refused when reading is off",
              "turned off on this server" in str(exc) and "OCR_BACKEND" in str(exc))
    try:
        ex.extract_document("scan.pdf", _scanned_pdf())
        check("a scanned PDF is refused when reading is off", False)
    except ex.ExtractionError as exc:
        check("a scanned PDF is refused when reading is off",
              "scan or photographs" in str(exc) and "turned off on this server" in str(exc))


def test_handwriting_transcription():
    print("\n-- handwriting and scans, once a reader is configured --")
    ocr, ex, patch = _with_backend("vision", TRANSCRIPT)
    try:
        check("image formats become acceptable", "png" in ex.supported_extensions())

        with patch:
            result = ex.extract_document("page.png", _png(), "DSA Midterm")

        check("the transcript reaches the grader", "A BST keps smaller values" in result.text)
        check("the page is labelled as a machine transcription",
              "machine transcription" in result.text)
        check("the submission is marked as transcribed, not read",
              result.stats["from_ocr"] is True)
        check("a transcribed page is not treated as a failed read",
              not result.looks_incomplete)
        check("faculty are told the grade rests on a reading of the writing",
              any("check it against the original" in n for n in result.notes))
        check("uncertainty markers are counted for the reviewer",
              result.stats["ocr_markers"] == 2)

        # The failure that no downstream check could ever catch.
        check("the student's own misspelling survives transcription",
              "keps" in result.text and "keeps smaller" not in result.text)

        with patch:
            scanned = ex.extract_document("scan.pdf", _scanned_pdf(2), "DSA")
        check("a scanned PDF is rasterised and transcribed page by page",
              scanned.stats["pages"] == 2 and scanned.stats["from_ocr"] is True)
        check("a transcribed scan is graded rather than refused",
              not scanned.looks_incomplete and scanned.chars > 0)
        check("the scan says why it needed transcribing",
              any("no text layer" in n for n in scanned.notes))

        # Colour PNG pages made a real 11-page submission 19 MB to send. Ink on
        # paper has no colour, so this is cost with nothing bought.
        rendered = ex._render_pdf_pages(_scanned_pdf(1))
        check("rendered pages are grayscale JPEG, not colour PNG",
              rendered[0][:3] == b"\xff\xd8\xff")
        check("the reader declares the format it is actually sending",
              ocr._media_type(rendered[0]) == "image/jpeg"
              and ocr._media_type(_png()) == "image/png")

        # A reader that fails must not hand back an empty submission, which
        # would be graded as though the student wrote nothing.
        from unittest import mock
        with mock.patch.object(ocr, "_read_page_vision",
                               side_effect=ocr.OCRError("reader unavailable")):
            try:
                ex.extract_document("page.png", _png())
                check("a failed read refuses instead of grading a blank", False)
            except ex.ExtractionError as exc:
                check("a failed read refuses instead of grading a blank",
                      "reader unavailable" in str(exc))

        with mock.patch.object(ocr, "_read_page_vision",
                               side_effect=lambda i, n, c: ocr.PageText(n, "")):
            try:
                ex.extract_document("page.png", _png())
                check("an empty transcript refuses instead of grading a blank", False)
            except ex.ExtractionError:
                check("an empty transcript refuses instead of grading a blank", True)
    finally:
        _restore()


def test_vision_gateway_shape():
    print("\n-- the request shape and model, as measured against IU's gateway --")
    import importlib
    import app.ocr

    os.environ["OCR_BACKEND"] = "vision"
    os.environ.pop("OCR_VISION_MODEL", None)
    os.environ.pop("OCR_TRY_ANTHROPIC_SHAPE", None)
    importlib.reload(app.ocr)
    o = app.ocr
    try:
        check("the default transcriber is a model probed to actually see images",
              o.VISION_MODEL == "gemma-4-31B-it")
        # The gateway answers the Anthropic block with a 500, so trying it
        # spends a failed request on every page of every submission.
        check("only the shape this gateway accepts is sent by default",
              len(o._vision_payloads(b"\x89PNG", 1, "ctx")) == 1)
        check("the sent shape is the OpenAI one",
              o._vision_payloads(b"\x89PNG", 1, "ctx")[0][1]["type"] == "image_url")

        os.environ["OCR_TRY_ANTHROPIC_SHAPE"] = "1"
        importlib.reload(app.ocr)
        check("the other shape is still available for a different gateway",
              len(app.ocr._vision_payloads(b"\x89PNG", 1, "ctx")) == 2)
        os.environ.pop("OCR_TRY_ANTHROPIC_SHAPE", None)

        # A document-conversion model answers in layout markup.
        doctags = ("<loc_0><loc_0><loc_500><loc_500><_unknown>"
                   "B <- empty array </line_code>")
        check("layout markup is stripped before the grader sees it",
              o.strip_doctags(doctags) == "B <- empty array")
        check("ordinary transcription is left alone",
              o.strip_doctags("A BST keeps values [?left]") == "A BST keeps values [?left]")
    finally:
        _restore()


def test_transcription_prompt():
    print("\n-- the transcription prompt defends against a helpful reader --")
    from app import ocr as o
    prompt = o.TRANSCRIPTION_PROMPT
    flat = " ".join(prompt.split())

    check("it forbids correcting the student's mistakes",
          "DO NOT CORRECT MISTAKES" in prompt)
    check("it says why: corrections become unearned marks",
          "marks the student did not earn" in flat)
    check("it forbids answering or grading",
          "NOT grading it and NOT answering it" in flat)
    check("it asks for illegible text to be marked", "[illegible]" in prompt)
    check("it asks for guesses to be marked", "[?guess]" in prompt)
    check("it keeps crossed-out work visible rather than deciding for the grader",
          "[struck:" in prompt and "the grader decides" in flat)
    # Both of these are failures observed on a real handwritten submission
    # rather than hypotheticals: a struck-and-replaced word came back as
    # "outer inner", and LaTeX leaked into 7 of 11 pages while the other 4 were
    # plain text.
    check("it handles a correction, not just a deletion",
          "[struck: outer] inner" in prompt and "NEVER as \"outer inner\"" in flat)
    check("it forbids LaTeX and Markdown markup",
          "No LaTeX and no Markdown" in prompt)
    check("it requires the same symbol to transcribe the same way on every page",
          "Be CONSISTENT across pages" in prompt)
    check("it keeps the student's notation rather than normalising it",
          'If they wrote "lg", write "lg", not "log"' in flat)
    check("it describes diagrams rather than interpreting them",
          "[diagram:" in prompt and "Do not interpret whether it is correct" in flat)


def test_refusals():
    print("\n-- unsupported formats are refused by name --")

    for ext, expected in (("doc", "Save As"), ("png", "turned off on this server"),
                          ("zip", "Archives cannot be read"), ("pages", "Export To"),
                          ("html", "submit the .ipynb file itself"),
                          ("heic", "Images cannot be read")):
        try:
            read(f"x.{ext}", b"content")
            check(f".{ext} is refused", False)
        except extract.ExtractionError as exc:
            check(f".{ext} is refused with actionable advice", expected in str(exc))

    try:
        read("x.xyz", b"content")
        check("an unknown extension names the accepted formats", False)
    except extract.ExtractionError as exc:
        check("an unknown extension names the accepted formats",
              ".pdf, .docx, .txt, .ipynb and .py" in str(exc))
        check("the accepted list is built from what is actually enabled",
              ".png" not in str(exc))

    try:
        read("noextension", b"content")
        check("a file with no extension is refused", False)
    except extract.ExtractionError:
        check("a file with no extension is refused", True)


def test_text_entry():
    print("\n-- Canvas text-entry HTML --")

    check("paragraphs stay separated",
          extract.strip_html("<p>Q1: first</p><p>Q2: second</p>") == "Q1: first\n\nQ2: second")
    check("list items stay separated",
          extract.strip_html("<ul><li>one</li><li>two</li></ul>") == "one\n\ntwo")
    check("line breaks survive", "\n" in extract.strip_html("a<br>b"))
    check("entities are decoded", extract.strip_html("<p>a &lt; b &amp; c</p>") == "a < b & c")
    check("table cells are separated",
          "|" in extract.strip_html("<table><tr><td>Q1</td><td>answer</td></tr></table>"))
    check("script content is dropped",
          "alert" not in extract.strip_html("<script>alert(1)</script><p>real</p>"))
    check("empty html is empty", extract.strip_html("") == "")
    check("None is handled", extract.strip_html(None) == "")


def main():
    test_docx_tables()
    test_docx_reading_order()
    test_docx_other_containers()
    test_docx_incomplete_detection()
    test_docx_header_fallback()
    test_pdf()
    test_txt_encodings()
    test_notebook()
    test_handwriting_off_by_default()
    test_handwriting_transcription()
    test_vision_gateway_shape()
    test_transcription_prompt()
    test_refusals()
    test_text_entry()

    print()
    if FAILS:
        print(f"{len(FAILS)} failure(s):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
