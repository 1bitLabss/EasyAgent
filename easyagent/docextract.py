"""Shared document text extraction for chat uploads and disk reads.

The chat upload path in app.py and the read tool in tools.py both need to turn
PDF and Word bytes into text. This module is the single copy; it must not
import either of them (app imports tools, so tools cannot import app).
"""

from pathlib import Path

DOCUMENT_SUFFIXES = {".pdf", ".docx"}


def extract_pdf_text(data: bytes) -> str | None:
    # Imported lazily so the app still boots without the library; a missing or
    # broken reader just means the upload keeps the "not shown here" fallback.
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None
    return text or None


def extract_docx_text(data: bytes) -> str | None:
    try:
        import io

        import docx

        document = docx.Document(io.BytesIO(data))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    except Exception:
        return None
    return text or None


def extract_document_text(path) -> str | None:
    """The text a PDF or Word file on disk carries. None for anything else.

    A file that cannot be read, cannot be parsed, or yields no text at all
    (encrypted PDF, corrupt bytes, missing library) returns None; the caller
    decides how to say so.
    """
    path = Path(path)
    extractor = {".pdf": extract_pdf_text, ".docx": extract_docx_text}.get(path.suffix.lower())
    if extractor is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    text = extractor(data)
    if not text or not text.strip():
        return None
    return text
