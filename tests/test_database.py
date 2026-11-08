"""Bank books in a database: durability, all-or-nothing actions, shared database across servers, migration.
Runs on SQLite always; on PostgreSQL too when TEST_POSTGRES_URL is set (e.g. a docker postgres)."""
import asyncio
import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.trade.database import DatabaseStore
from auto_connect.trade.engine import TradeFinanceEngine, WorkflowError
from auto_connect.trade.models import (DocumentRequirement, LCTerms, Party, Presentation, PresentedDocument)
from auto_connect.trade.state import StateStore, seed_state
from auto_connect.trade.systems import BusError, SwiftGatewaySystem
from auto_connect.vault import ZeroTrustVault

URLS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_POSTGRES_URL") else [])


@pytest.fixture(params=URLS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'bank.db'}"
    import psycopg

    base = os.environ["TEST_POSTGRES_URL"]
    name = "tf_" + uuid.uuid4().hex[:10]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    return base.rsplit("/", 1)[0] + "/" + name


def engine_for(store):
    vault = ZeroTrustVault()
    return TradeFinanceEngine(store, vault, A2AEcosystemMesh(), UniversalAPIExecutor(vault))


def terms(today):
    return LCTerms(applicant_customer_id="DE-CORP-40551",
                   beneficiary=Party(name="Milano Meccanica S.p.A.", address="Milano", country="IT"),
                   advising_bank_bic="BNCAITMMXXX", amount=Decimal("300000"),
                   expiry_date=today + timedelta(days=90), latest_shipment_date=today + timedelta(days=60),
                   port_of_loading="Genoa", port_of_discharge="Hamburg", goods_description="CNC machines",
                   documents_required=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"),
                                       DocumentRequirement(doc_type="BILL_OF_LADING", originals=3),
                                       DocumentRequirement(doc_type="INSURANCE_DOCUMENT")])


def presentation(today):
    return Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=today, claimed_amount=Decimal("300000"), documents=[
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", issue_date=today, fields={
            "amount": "300000", "currency": "EUR", "beneficiary_name": "Milano Meccanica S.p.A.",
            "applicant_name": "Bayerische Maschinenbau AG", "goods_description": "CNC machines"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=3, issue_date=today, fields={
            "on_board_date": today.isoformat(), "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "clean": True,
            "signed_by": "CARRIER", "originals_issued": 3}),
        PresentedDocument(doc_type="INSURANCE_DOCUMENT", issue_date=today, fields={"coverage_amount": "330000", "currency": "EUR"})])


def test_books_survive_restart_and_payment_is_all_or_nothing(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_OUTPUT_DIR", str(tmp_path / "out"))
    eng = engine_for(DatabaseStore(db_url))
    today = eng.today
    lc = asyncio.run(eng.apply_lc(terms(today), created_by="ops1"))
    assert lc.status == "ISSUED"

    # restart: a new server process reads the same books
    eng2 = engine_for(DatabaseStore(db_url))
    st = eng2.state
    assert st.deals[lc.ref].status == "ISSUED" and st.deals[lc.ref].created_by == "ops1"
    assert st.ledger_balanced() and len(st.ledger) == len(eng.state.ledger)
    held = st.facilities["FAC-40551-LC"].earmarks[lc.ref]

    # an invalid amendment is refused and leaves no trace (no half-created amendment)
    with pytest.raises(WorkflowError):
        asyncio.run(eng2.amend_lc(lc.ref, {"expiry_date": (today - timedelta(days=1)).isoformat()}))
    assert eng2.state.deals[lc.ref].amendments == []

    # the SWIFT gateway fails halfway through the payment (after debit, fees and MT756)
    original = SwiftGatewaySystem.op_release_message

    def broken(self, deal_ref, mt_type, receiver_bic, fin_text):
        if mt_type == "pacs.009":
            raise ConnectionError("SWIFT interface down")
        return original(self, deal_ref, mt_type, receiver_bic, fin_text)
    monkeypatch.setattr(SwiftGatewaySystem, "op_release_message", broken)
    ledger_before = len(eng2.state.ledger)
    balance_before = eng2.state.accounts[eng2.state.customers["DE-CORP-40551"].current_account_iban].balance
    import glob

    swift_dir = os.path.join(os.environ["AUTO_CONNECT_OUTPUT_DIR"], "swift")
    emails_before = set(glob.glob(os.path.join(os.environ["AUTO_CONNECT_OUTPUT_DIR"], f"{lc.ref}_*.eml")))
    with pytest.raises((BusError, ConnectionError)):
        asyncio.run(eng2.present_documents(lc.ref, presentation(today)))
    # outbox: the MT756 and the client's debit advice queued before the failure were never sent
    assert not glob.glob(os.path.join(swift_dir, f"{lc.ref}_MT756_*"))
    assert set(glob.glob(os.path.join(os.environ["AUTO_CONNECT_OUTPUT_DIR"], f"{lc.ref}_*.eml"))) == emails_before
    assert not eng2.state.outbox or all(i["status"] == "SENT" for i in eng2.state.outbox.values())
    for st in (eng2.state, DatabaseStore(db_url).state):  # in memory and in the database: nothing half-booked
        deal = st.deals[lc.ref]
        assert deal.status == "ISSUED" and deal.presentations == []
        assert len(st.ledger) == ledger_before and st.ledger_balanced()
        assert st.accounts[st.customers["DE-CORP-40551"].current_account_iban].balance == balance_before
        assert st.facilities["FAC-40551-LC"].earmarks[lc.ref] == held

    # the interface is back: the same presentation goes through completely
    monkeypatch.setattr(SwiftGatewaySystem, "op_release_message", original)
    lc = asyncio.run(eng2.present_documents(lc.ref, presentation(today)))
    assert lc.status == "CLOSED" and lc.presentations[0].status == "PAID"
    assert len(glob.glob(os.path.join(swift_dir, f"{lc.ref}_MT756_*"))) == 1  # released once, after the commit
    assert all(i["status"] == "SENT" for i in eng2.state.outbox.values())
    assert all(m.status == "RELEASED" for m in eng2.state.swift_messages.values())
    st = DatabaseStore(db_url).state
    assert st.deals[lc.ref].status == "CLOSED" and st.ledger_balanced() and lc.ref not in st.facilities["FAC-40551-LC"].earmarks


def test_two_servers_share_one_database(db_url):
    a, b = engine_for(DatabaseStore(db_url)), engine_for(DatabaseStore(db_url))
    today = a.today
    lc1 = asyncio.run(a.apply_lc(terms(today)))
    assert b.sync() and lc1.ref in b.state.deals  # B sees A's deal
    lc2 = asyncio.run(b.apply_lc(terms(today)))
    lc3 = asyncio.run(a.apply_lc(terms(today)))  # A reloads before acting: no duplicate reference
    assert len({lc1.ref, lc2.ref, lc3.ref}) == 3
    final = DatabaseStore(db_url).state
    assert {lc1.ref, lc2.ref, lc3.ref} <= set(final.deals) and final.ledger_balanced()
    used = final.facilities["FAC-40551-LC"].utilised
    assert used == sum(final.facilities["FAC-40551-LC"].earmarks.values())


def test_existing_json_books_are_migrated(tmp_path):
    legacy = StateStore(path=str(tmp_path / "bank_state.json"), seed=False)
    legacy.state = seed_state()
    eng = engine_for(legacy)
    lc = asyncio.run(eng.apply_lc(terms(eng.today)))
    store = DatabaseStore(f"sqlite:///{tmp_path / 'bank.db'}", legacy_json_path=legacy.path)
    assert lc.ref in store.state.deals and store.state.ledger_balanced()
    assert len(store.state.ledger) == len(legacy.state.ledger)
