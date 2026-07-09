import io
import re

from docx import Document
from pypdf import PdfReader


class ExtractionError(Exception):
    pass


def extract_text(filename, content_bytes):
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return _extract_pdf(content_bytes)
    if ext == "docx":
        return _extract_docx(content_bytes)
    if ext == "txt":
        return content_bytes.decode("utf-8", errors="replace")
    raise ExtractionError(f"Unsupported file type: .{ext or 'unknown'}")


def _extract_pdf(content_bytes):
    reader = PdfReader(io.BytesIO(content_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_docx(content_bytes):
    doc = Document(io.BytesIO(content_bytes))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def strip_html(html_text):
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    return re.sub(r"\s+", " ", text).strip()
