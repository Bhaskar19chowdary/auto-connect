"""Fixes from the independent client audit: reader vocabulary, legal forms, transport document, demand/claim upload."""
import base64
from datetime import timedelta

from fastapi.testclient import TestClient

from auto_connect.document_reader import DocumentReader
from auto_connect.trade.rules import different_legal_form, legal_form


def test_reader_understands_common_real_world_wording():
    inv = "INVOICE\nExporter: Milano Meccanica S.p.A.\nConsignee: Bayerische Maschinenbau AG\nGoods: CNC machines\nInvoice Value: EUR 300,000.00\n"
    bl = "OCEAN BILL OF LADING\nPort of Loading: Genoa\nPort of Discharge: Hamburg\nDate of Shipment: November 8, 2026\n" \
         "No. of Original B(s)/L: THREE (3)\nAs Carrier: MSC\n"
    r = DocumentReader(None).read([("inv.txt", inv.encode()), ("bl.txt", bl.encode())])
    d = {x["doc_type"]: x for x in r["documents"]}
    assert d["COMMERCIAL_INVOICE"]["fields"]["amount"] == "300000.00"
    assert d["COMMERCIAL_INVOICE"]["fields"]["applicant_name"] == "Bayerische Maschinenbau AG"
    assert d["BILL_OF_LADING"]["fields"]["on_board_date"] == "2026-11-08" and d["BILL_OF_LADING"]["originals"] == 3


def test_legal_forms():
    assert legal_form("Milano Meccanica S.p.A.") == "SPA" and legal_form("Rhein Bau GmbH & Co. KG") == "GMBH_CO_KG"
    assert different_legal_form("Milano Meccanica SRL", "Milano Meccanica S.p.A.")
    assert not different_legal_form("X GmbH", "X GMBH") and not different_legal_form("Stadt Koeln", "Stadt Koeln AG")


def _lc(today, **kw):
    t = {"applicant_customer_id": "DE-CORP-40551", "beneficiary": {"name": "Milano Meccanica S.p.A.", "country": "IT"},
         "advising_bank_bic": "BNCAITMMXXX", "amount": "100000", "incoterms": "FOB",
         "expiry_date": (today + timedelta(days=60)).isoformat(), "latest_shipment_date": (today + timedelta(days=30)).isoformat(),
         "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "goods_description": "machines",
         "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]}
    t.update(kw)
    return t


def test_transport_document_required_and_uploads_for_demands_and_claims():
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    lc = c.post("/api/v1/tf/lc", json=_lc(today, documents_required=[{"doc_type": "COMMERCIAL_INVOICE"}])).json()
    exc = [e for e in c.get("/api/v1/tf/exceptions").json() if e["deal_ref"] == lc["ref"]]
    assert lc["status"] == "ON_HOLD_DATA_QUALITY" and "TRANSPORT_DOCUMENT_MISSING" in exc[0]["reason"]
    assert c.post("/api/v1/tf/lc", json=_lc(today, incoterms="EXW", documents_required=[{"doc_type": "COMMERCIAL_INVOICE"}])
                  ).json()["status"] == "ISSUED"  # EXW: buyer arranges transport

    g = c.post("/api/v1/tf/guarantees", json={"applicant_customer_id": "DE-CORP-40551", "beneficiary": {"name": "Saudi Co", "country": "SA"},
               "amount": "200000", "expiry_date": (today + timedelta(days=300)).isoformat(), "underlying_transaction": "Contract 1"}).json()
    letter = "DEMAND FOR PAYMENT UNDER GUARANTEE\nAmount demanded: EUR 50.000,00\nStatement of breach: delivery delayed\n"
    up = c.post(f"/api/v1/tf/guarantees/{g['ref']}/demands/upload",
                json={"files": [{"filename": "d.txt", "text": letter}]}).json()
    assert up["deal"]["demands"][-1]["amount"] == "50000.00" and up["deal"]["demands"][-1]["status"] == "PAID"
    assert c.post(f"/api/v1/tf/guarantees/{g['ref']}/demands/upload",
                  json={"files": [{"filename": "x.txt", "text": "hello, nothing to read here at all"}]}).status_code == 422

    b = c.post("/api/v1/tf/sureties", json={"principal_customer_id": "DE-CORP-77310", "obligee": {"name": "Stadt Koeln", "country": "DE"},
               "amount": "100000", "contract_value": "1000000", "expiry_date": (today + timedelta(days=300)).isoformat(),
               "underlying_contract": "VOB/B contract"}).json()
    files = [{"filename": "c.txt", "text": "CLAIM UNDER SURETY BOND\nAmount claimed: EUR 10,000.00\nWe hereby claim payment."},
             {"filename": "n.txt", "text": "NOTICE OF DEFAULT\nThe contractor failed to remedy the defects."},
             {"filename": "e.txt", "text": "EXPERT REPORT\nEvidence of damage: remedy costs EUR 10,000."}]
    up = c.post(f"/api/v1/tf/sureties/{b['ref']}/claims/upload", json={"files": files}).json()
    assert up["deal"]["claims"][-1]["status"] == "AWAITING_PRINCIPAL"


def test_address_after_the_name_is_not_a_discrepancy():
    """UCP 600 Art. 14(j): a document may show the party's address after its name; only the name must match."""
    from datetime import date

    from auto_connect.trade import rules
    from auto_connect.trade.models import LetterOfCredit
    from auto_connect.trade.samples import sample_lc, sample_presentation

    today = date(2026, 11, 9)
    t = sample_lc(today)
    lc = LetterOfCredit(ref="ILC1", customer_id=t.applicant_customer_id, status="ISSUED", currency=t.currency,
                        amount=t.amount, outstanding=t.amount, terms=t)

    def codes(beneficiary, applicant):
        p = sample_presentation(t, today, today, "Hanse Solartechnik GmbH")
        inv = next(d for d in p.documents if d.doc_type == "COMMERCIAL_INVOICE")
        inv.fields.update(beneficiary_name=beneficiary, applicant_name=applicant)
        return {d.code for d in rules.examine_ucp600(lc, p, "Hanse Solartechnik GmbH")}

    ok = codes(f"{t.beneficiary.name}, 12 Some Street, Some City", "Hanse Solartechnik GmbH, Grosse Elbstrasse 145, Hamburg")
    assert not ok & {"INVOICE_NOT_ISSUED_BY_BENEFICIARY", "INVOICE_NOT_TO_APPLICANT",
                     "INVOICE_ISSUER_LEGAL_FORM", "INVOICE_APPLICANT_LEGAL_FORM"}
    wrong = codes("Completely Other Trading Ltd., 12 Some Street", "Other Buyer GmbH, Hamburg")
    assert {"INVOICE_NOT_ISSUED_BY_BENEFICIARY", "INVOICE_NOT_TO_APPLICANT"} <= wrong
