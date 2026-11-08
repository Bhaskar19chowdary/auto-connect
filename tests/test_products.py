"""Export LC (confirmed), documentary collection (import D/P) and outbox behaviour - fast, in memory."""
import asyncio
from datetime import timedelta
from decimal import Decimal

from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.trade.engine import TradeFinanceEngine
from auto_connect.trade.models import (CollectionTerms, DocumentRequirement, ExportLCTerms, Party, Presentation,
                                       PresentedDocument)
from auto_connect.trade.state import StateStore
from auto_connect.vault import ZeroTrustVault


def engine():
    vault = ZeroTrustVault()
    return TradeFinanceEngine(StateStore(path=""), vault, A2AEcosystemMesh(), UniversalAPIExecutor(vault))


def test_confirmed_export_lc_paid_and_reimbursed():
    eng = engine()
    today = eng.today
    t = ExportLCTerms(beneficiary_customer_id="DE-CORP-40551", issuing_bank_bic="BKCHCNBJXXX", issuing_bank_lc_ref="LC1",
                      applicant=Party(name="Shenzhen Tools Co Ltd", country="CN"), currency="EUR", amount=Decimal("100000"),
                      expiry_date=today + timedelta(days=60), latest_shipment_date=today + timedelta(days=30),
                      port_of_loading="Hamburg", port_of_discharge="Shenzhen", goods_description="machines",
                      documents_required=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"),
                                          DocumentRequirement(doc_type="BILL_OF_LADING", originals=3)],
                      confirmation_instructions="CONFIRM")
    d = asyncio.run(eng.receive_export_lc(t))
    assert d.status == "CONFIRMED" and eng.state.facilities["FAC-BANK-BKCHCNBJ"].earmarks[d.ref] == Decimal("100000")
    p = Presentation(presenting_bank_bic="DEMODEMMXXX", presentation_date=today, claimed_amount=Decimal("100000"), documents=[
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", issue_date=today, fields={
            "amount": "100000", "currency": "EUR", "beneficiary_name": "Bayerische Maschinenbau AG",
            "applicant_name": "Shenzhen Tools Co Ltd", "goods_description": "machines"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=3, issue_date=today, fields={
            "on_board_date": today.isoformat(), "port_of_loading": "Hamburg", "port_of_discharge": "Shenzhen",
            "clean": True, "signed_by": "CARRIER", "originals_issued": 3})])
    d = asyncio.run(eng.present_export_documents(d.ref, p))
    assert d.status == "PAID_AWAITING_REIMBURSEMENT" and d.receivable_outstanding == Decimal("100000")
    d = asyncio.run(eng.issuing_bank_response(d.ref, p.presentation_id, "PAID"))
    assert d.status == "CLOSED" and d.ref not in eng.state.facilities["FAC-BANK-BKCHCNBJ"].earmarks
    assert eng.state.ledger_balanced()
    assert all(i["status"] == "SENT" for i in eng.state.outbox.values())
    assert {"730", "754"} <= {eng.state.swift_messages[m].mt_type for m in d.swift_message_ids}


def test_import_collection_documents_against_payment():
    eng = engine()
    c = asyncio.run(eng.new_collection(CollectionTerms(
        direction="IMPORT", customer_id="DE-CORP-40551", counterparty=Party(name="Istanbul Makina AS", country="TR"),
        counterparty_bank_bic="AKBKTRISXXX", amount=Decimal("50000"),
        documents=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"), DocumentRequirement(doc_type="BILL_OF_LADING")])))
    assert c.status == "PRESENTED_TO_DRAWEE"
    iban = eng.state.customers["DE-CORP-40551"].current_account_iban
    before = eng.state.accounts[iban].balance
    c = asyncio.run(eng.drawee_decision(c.ref, "pay"))
    assert c.status == "PAID" and before - eng.state.accounts[iban].balance >= Decimal("50000")
    types = [eng.state.swift_messages[m].mt_type for m in c.swift_message_ids]
    assert types == ["410", "pacs.009", "400"] and eng.state.ledger_balanced()


def test_no_screening_against_incomplete_lists():
    """While the live EU / OFAC lists are still loading, deals wait in the operations queue instead of being
    screened against the built-in sample list only."""
    from auto_connect.trade.models import LCTerms
    from auto_connect.trade.reference_feeds import ListEntry, SanctionsIndex

    eng = engine()
    eng.reference.enabled, eng.reference.attempted = True, False
    today = eng.today
    terms = LCTerms(applicant_customer_id="DE-CORP-40551", beneficiary=Party(name="Milano Meccanica S.p.A.", country="IT"),
                    advising_bank_bic="BNCAITMMXXX", amount=Decimal("10000"), expiry_date=today + timedelta(days=60),
                    latest_shipment_date=today + timedelta(days=30), port_of_loading="Genoa", port_of_discharge="Hamburg",
                    incoterms="FOB", goods_description="machines", documents_required=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"),
                                                                      DocumentRequirement(doc_type="BILL_OF_LADING")])
    lc = asyncio.run(eng.apply_lc(terms))
    exc = next(e for e in eng.state.exceptions.values() if e.deal_ref == lc.ref)
    assert lc.status == "ON_HOLD_OPERATIONS" and "SANCTIONS_LISTS_NOT_LOADED" in exc.reason
    assert not lc.swift_message_ids  # nothing issued
    idx = SanctionsIndex()
    idx.add(ListEntry("X1", "OFAC SDN", "Some Sanctioned Entity", ["Some Sanctioned Entity"], {"RU"}, "entity"))
    eng.reference.index, eng.reference.attempted = idx, True
    res = asyncio.run(eng.resolve_exception(exc.exception_id, True, "ops.supervisor", "lists loaded - retry"))
    assert res["deal"].status == "ISSUED"
