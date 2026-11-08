"""Original uploaded documents kept in the database (exact bytes, SHA-256, same transaction as the action, tamper
detection) and the read-only Excel report."""
import asyncio
import base64
import hashlib
import io
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

URLS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_POSTGRES_URL") else [])
INVOICE = (b"COMMERCIAL INVOICE\nSeller: Milano Meccanica S.p.A.\nBuyer: Bayerische Maschinenbau AG\n"
           b"Description of goods: machines\nTotal amount: EUR 100,000.00\n")
BL = (b"BILL OF LADING\nPort of loading: Genoa\nPort of discharge: Hamburg\nShipped on board: {d}\n"
      b"Number of original B/L: THREE (3)\nSigned by: MSC as carrier\n")


@pytest.fixture(params=URLS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'bank.db'}"
    import psycopg

    base = os.environ["TEST_POSTGRES_URL"]
    name = "f_" + uuid.uuid4().hex[:10]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    return base.rsplit("/", 1)[0] + "/" + name


def lc(today):
    return {"applicant_customer_id": "DE-CORP-40551", "beneficiary": {"name": "Milano Meccanica S.p.A.", "country": "IT"},
            "advising_bank_bic": "BNCAITMMXXX", "amount": "100000", "incoterms": "FOB",
            "expiry_date": (today + timedelta(days=60)).isoformat(), "latest_shipment_date": (today + timedelta(days=30)).isoformat(),
            "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "goods_description": "machines",
            "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]}


def files(today):
    bl = BL.replace(b"{d}", today.strftime("%d.%m.%Y").encode())
    return [{"filename": "invoice.txt", "content_base64": base64.b64encode(INVOICE).decode()},
            {"filename": "bill of lading.txt", "content_base64": base64.b64encode(bl).decode()}], bl


def test_originals_stored_exact_tamper_detected_and_survive_restart(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", db_url)
    monkeypatch.setenv("AUTO_CONNECT_STATE_FILE", str(tmp_path / "unused.json"))
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    deal = c.post("/api/v1/tf/lc", json=lc(today)).json()
    fs, bl = files(today)
    up = c.post(f"/api/v1/tf/lc/{deal['ref']}/presentations/upload",
                json={"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": today.isoformat(), "files": fs}).json()
    assert up["deal"]["presentations"][-1]["status"] == "PAID"
    listed = c.get(f"/api/v1/tf/deals/{deal['ref']}/files").json()
    assert [a["filename"] for a in listed] == ["invoice.txt", "bill of lading.txt"]
    assert all(a["item_id"] == up["presentation_id"] for a in listed)
    r = c.get(f"/api/v1/tf/files/{listed[0]['file_id']}")
    assert r.status_code == 200 and r.content == INVOICE and r.headers["x-sha256"] == hashlib.sha256(INVOICE).hexdigest()

    # a refused action (deal already closed) keeps no file
    before = len(c.get(f"/api/v1/tf/deals/{deal['ref']}/files").json())
    refused = c.post(f"/api/v1/tf/lc/{deal['ref']}/presentations/upload",
                     json={"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": today.isoformat(), "files": fs})
    assert refused.status_code == 409 and len(c.get(f"/api/v1/tf/deals/{deal['ref']}/files").json()) == before

    # restart: the originals are still there, byte for byte
    c2 = TestClient(create_app())
    r = c2.get(f"/api/v1/tf/files/{listed[1]['file_id']}")
    assert r.status_code == 200 and r.content == bl

    # someone changes a stored file directly in the database -> detected on download
    store = c2.app.state.trade_engine.store
    store._exec("UPDATE files SET content = ? WHERE file_id = ?", [b"forged", listed[0]["file_id"]])
    assert c2.get(f"/api/v1/tf/files/{listed[0]['file_id']}").status_code == 500


def test_file_not_kept_when_the_action_fails(db_url, monkeypatch):
    from auto_connect.executor import UniversalAPIExecutor
    from auto_connect.mesh import A2AEcosystemMesh
    from auto_connect.trade.database import DatabaseStore
    from auto_connect.trade.engine import TradeFinanceEngine
    from auto_connect.trade.models import LCTerms, Presentation, PresentedDocument
    from auto_connect.trade.systems import SwiftGatewaySystem
    from auto_connect.vault import ZeroTrustVault

    vault = ZeroTrustVault()
    eng = TradeFinanceEngine(DatabaseStore(db_url), vault, A2AEcosystemMesh(), UniversalAPIExecutor(vault))
    today = eng.today
    deal = asyncio.run(eng.apply_lc(LCTerms(**lc(today))))
    original = SwiftGatewaySystem.op_release_message

    def broken(self, deal_ref, mt_type, receiver_bic, fin_text):
        if mt_type == "756":
            raise ConnectionError("SWIFT down")
        return original(self, deal_ref, mt_type, receiver_bic, fin_text)
    monkeypatch.setattr(SwiftGatewaySystem, "op_release_message", broken)
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=today, claimed_amount=Decimal("100000"), documents=[
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", issue_date=today, fields={
            "amount": "100000", "currency": "EUR", "beneficiary_name": "Milano Meccanica S.p.A.",
            "applicant_name": "Bayerische Maschinenbau AG", "goods_description": "machines"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=3, issue_date=today, fields={
            "on_board_date": today.isoformat(), "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "clean": True,
            "signed_by": "CARRIER", "originals_issued": 3})])
    with pytest.raises(Exception):
        asyncio.run(eng.present_documents(deal.ref, p, files=[("invoice.pdf", b"%PDF-1.4 original")], uploaded_by="bank"))
    fresh = DatabaseStore(db_url)
    assert fresh.state.deals[deal.ref].attachments == []
    assert fresh._exec("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_excel_report():
    from openpyxl import load_workbook

    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    deal = c.post("/api/v1/tf/lc", json=lc(today)).json()
    fs, _ = files(today)
    c.post(f"/api/v1/tf/lc/{deal['ref']}/presentations/upload",
           json={"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": today.isoformat(), "files": fs})
    r = c.get("/api/v1/tf/export/excel")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Deals", "Bookings", "Fees", "Decisions", "SWIFT messages", "Documents", "About"]
    deals = list(wb["Deals"].iter_rows(min_row=2, values_only=True))
    row = next(x for x in deals if x[0] == deal["ref"])
    assert row[1] == "LC" and row[3] == "CLOSED" and row[5] == 100000.0 and row[11] == 2
    docs = list(wb["Documents"].iter_rows(min_row=2, values_only=True))
    assert {d[3] for d in docs} == {"invoice.txt", "bill of lading.txt"} and all(len(d[6]) == 64 for d in docs)
    assert len(list(wb["Bookings"].iter_rows(min_row=2))) >= 4 and wb["About"]["B5"].value == "YES"
