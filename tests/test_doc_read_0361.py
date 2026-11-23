"""0.3.61: the read tool reads PDF and Word documents from disk, not just uploads.

Before this the read tool ran read_text on every file, so a .pdf or .docx came
back as replacement-character mojibake and the bot called the file corrupt.
"""

import io

import pytest

from easyagent.docextract import extract_document_text, extract_docx_text, extract_pdf_text
from easyagent.store import Store
from easyagent.tools import ToolError, ToolRequest, _run_files


def _make_pdf_bytes(text: str) -> bytes:
    """A real one-page PDF with extractable text, built by hand."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    n = len(objs) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _encrypt_pdf_bytes(data: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(data)).pages:
        writer.add_page(page)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(*paragraphs: str) -> bytes:
    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "data")
    store.ensure()
    return store


def _read(store: Store, path, offset: int = 0, limit: int = 0) -> str:
    return _run_files(
        store,
        ToolRequest(kind="files", action="read", path=str(path), offset=offset, limit=limit),
    )


def test_read_tool_reads_a_docx_from_disk(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(_make_docx_bytes("Quarterly numbers look kind.", "Second paragraph here."))
    text = _read(_store(tmp_path), path)
    assert "Quarterly numbers look kind." in text
    assert "Second paragraph here." in text
    assert "\ufffd" not in text


def test_read_tool_reads_a_pdf_from_disk(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(_make_pdf_bytes("Hello from the PDF"))
    text = _read(_store(tmp_path), path)
    # Not just "contains the words" — a raw read_text of the PDF bytes would
    # pass that; the tool must return the extracted text, not the PDF source.
    assert text.strip() == "Hello from the PDF"
    assert "\ufffd" not in text


def test_encrypted_pdf_gives_a_plain_refusal_not_mojibake(tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(_encrypt_pdf_bytes(_make_pdf_bytes("secret body")))
    with pytest.raises(ToolError) as exc:
        _read(_store(tmp_path), path)
    assert "no text could be read from that file" in str(exc.value)


def test_corrupt_pdf_gives_a_plain_refusal_not_mojibake(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf at all")
    with pytest.raises(ToolError) as exc:
        _read(_store(tmp_path), path)
    assert "no text could be read from that file" in str(exc.value)
    assert "\ufffd" not in str(exc.value)


def test_offset_limit_windows_extracted_docx_text(tmp_path):
    path = tmp_path / "many.docx"
    path.write_bytes(_make_docx_bytes("one", "two", "three", "four"))
    text = _read(_store(tmp_path), path, offset=2, limit=2)
    assert "2|two" in text
    assert "3|three" in text
    assert "1|one" not in text
    assert "4|four" not in text
    assert "lines 2-3 of 4" in text
    assert "next_offset=4" in text


def test_plain_txt_read_is_unchanged(tmp_path):
    path = tmp_path / "plain.txt"
    original = "alpha\nbeta\n"
    path.write_text(original, encoding="utf-8")
    assert _read(_store(tmp_path), path) == original


def test_oversized_document_is_refused(tmp_path):
    path = tmp_path / "huge.pdf"
    path.write_bytes(b"%PDF-" + b"x" * 10_000_001)
    with pytest.raises(ToolError) as exc:
        _read(_store(tmp_path), path)
    assert "too large to read into the chat" in str(exc.value)


def test_upload_extraction_round_trips_through_the_shared_module(tmp_path):
    # The chat upload path in app.py now borrows these same functions.
    assert "Hello from the PDF" in (extract_pdf_text(_make_pdf_bytes("Hello from the PDF")) or "")
    assert extract_pdf_text(_encrypt_pdf_bytes(_make_pdf_bytes("x"))) is None
    assert extract_pdf_text(b"junk") is None
    docx_bytes = _make_docx_bytes("upload body")
    assert "upload body" in (extract_docx_text(docx_bytes) or "")
    assert extract_docx_text(b"junk") is None
    # The disk helper dispatches on suffix and refuses everything else.
    docx_path = tmp_path / "shared.docx"
    docx_path.write_bytes(docx_bytes)
    assert "upload body" in (extract_document_text(docx_path) or "")
    txt_path = tmp_path / "plain.txt"
    txt_path.write_text("plain", encoding="utf-8")
    assert extract_document_text(txt_path) is None
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"junk")
    assert extract_document_text(empty_pdf) is None
