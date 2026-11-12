"""PDF and .docx attachments carry their text to the model (issue 0356)."""
import io

from easyagent import app
from easyagent.app import _attachment_text


def _make_docx(marker: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(marker)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_pdf(marker: str) -> bytes:
    """A minimal uncompressed one-page PDF with the marker drawn as text."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        None,  # content stream, filled in below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 24 Tf 72 720 Td ({marker}) Tj ET".encode("ascii")
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(xref_at).encode() + b"\n%%EOF\n")
    return bytes(out)


def test_docx_attachment_text_is_extracted():
    data = _make_docx("MARKER DOCX TEXT")
    text = _attachment_text("report.docx",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            data)
    assert text is not None and "MARKER DOCX TEXT" in text
    # The extension alone is enough even when the browser sends no media type.
    assert "MARKER DOCX TEXT" in _attachment_text("report.docx", "", data)


def test_pdf_attachment_text_is_extracted():
    data = _make_pdf("MARKER PDF TEXT")
    text = _attachment_text("paper.pdf", "application/pdf", data)
    assert text is not None and "MARKER PDF TEXT" in text
    assert "MARKER PDF TEXT" in _attachment_text("paper.pdf", "", data)


def test_corrupt_pdf_returns_none():
    assert _attachment_text("broken.pdf", "application/pdf", b"%PDF-1.4 garbage") is None


def test_corrupt_docx_returns_none():
    assert _attachment_text("broken.docx",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            b"\x00\x01random bytes that are not a zip\x02") is None


def test_oversized_extraction_is_clipped(monkeypatch):
    monkeypatch.setattr(app, "ATTACHMENT_TEXT_CHARS", 20)
    text = _attachment_text("big.docx", "", _make_docx("x" * 500))
    assert text is not None
    assert text.endswith("\n… the file continues past what is shown.")
    assert text.startswith("x" * 20)
