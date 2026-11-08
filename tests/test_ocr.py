"""Scanned documents (images / image-only PDFs) read with Tesseract OCR. Skipped when the OCR tools are missing."""
import os
import shutil
import subprocess

import pytest

from auto_connect.document_reader import DocumentReader, pdf_text

pytestmark = pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")),
                                reason="needs tesseract and pdftoppm (brew install tesseract poppler)")

LINES = ["BILL OF LADING", "Shipper: Milano Meccanica S.p.A.", "Vessel: MSC AURORA IMO 9839131",
         "Port of loading: Genoa, Italy", "Port of discharge: Hamburg, Germany", "Shipped on board: 08.11.2026",
         "Number of original B/L: THREE (3)", "Clean on board", "Signed by: Mediterranean Shipping Company as carrier"]


def text_pdf(lines):
    content = "BT /F1 14 Tf 50 780 Td 22 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    return (out + f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF").encode("latin-1")


def scan(tmp_path):
    src = tmp_path / "bl.pdf"
    src.write_bytes(text_pdf(LINES))
    subprocess.run(["pdftoppm", "-r", "200", "-png", "-singlefile", str(src), str(tmp_path / "scan")], check=True)
    return (tmp_path / "scan.png").read_bytes()


def check_bl(result):
    d = result["documents"][0]
    assert d["doc_type"] == "BILL_OF_LADING", result
    f = d["fields"]
    assert f.get("on_board_date") == "2026-11-08" and f.get("signed_by") == "CARRIER", f
    assert "genoa" in f.get("port_of_loading", "").lower() and "hamburg" in f.get("port_of_discharge", "").lower(), f
    assert f.get("vessel_imo") == "IMO9839131", f
    assert d["originals"] == 3, d


def test_scanned_image_is_read(tmp_path):
    r = DocumentReader(None).read([("scan.png", scan(tmp_path))])
    assert "Tesseract" in r["documents"][0]["engine"]
    check_bl(r)


@pytest.mark.skipif(not shutil.which("sips"), reason="sips (macOS) builds the image-only PDF")
def test_image_only_pdf_is_read(tmp_path):
    png = tmp_path / "page.png"
    png.write_bytes(scan(tmp_path))
    subprocess.run(["sips", "-s", "format", "pdf", str(png), "--out", str(tmp_path / "scanned.pdf")], check=True,
                   capture_output=True)
    data = (tmp_path / "scanned.pdf").read_bytes()
    assert len(pdf_text(data).strip()) < 20  # really no text layer
    r = DocumentReader(None).read([("scanned.pdf", data)])
    assert "scanned PDF" in r["documents"][0]["engine"]
    check_bl(r)
