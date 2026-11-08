"""The bank's systems are the record: customer data and account balances are taken from the customer master and
the core ledger, not from the engine's own copy; contingent liabilities are tracked on the deal."""
import asyncio
from datetime import timedelta
from decimal import Decimal

from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.trade.engine import TradeFinanceEngine
from auto_connect.trade.models import (DocumentRequirement, LCTerms, Party, Presentation, PresentedDocument)
from auto_connect.trade.state import StateStore
from auto_connect.vault import ZeroTrustVault


def test_engine_uses_master_data_and_ledger_balance_not_its_copy():
    vault = ZeroTrustVault()
    ex = UniversalAPIExecutor(vault)
    eng = TradeFinanceEngine(StateStore(path=""), vault, A2AEcosystemMesh(), ex)
    sim_cif, sim_ledger = ex._in_process_handlers["tf_customer_master"], ex._in_process_handlers["tf_core_ledger"]

    async def bank_cif(method, path, params, headers):  # the bank's customer master knows a newer contact address
        res = await sim_cif(method, path, params, headers)
        if path.endswith("get_customer") and res["status_code"] == 200:
            res["data"]["customer"]["email"] = "treasury@bm-ag.example"
        return res

    async def bank_ledger(method, path, params, headers):  # the core ledger shows the account nearly empty
        res = await sim_ledger(method, path, params, headers)
        if path.endswith("account_balance") and res["status_code"] == 200:
            res["data"]["free_balance"] = "1000.00"
        return res
    ex.register_in_process_handler("tf_customer_master", bank_cif)
    ex.register_in_process_handler("tf_core_ledger", bank_ledger)

    today = eng.today
    lc = asyncio.run(eng.apply_lc(LCTerms(
        applicant_customer_id="DE-CORP-40551", beneficiary=Party(name="Milano Meccanica S.p.A.", country="IT"),
        advising_bank_bic="BNCAITMMXXX", amount=Decimal("200000"), expiry_date=today + timedelta(days=60),
        latest_shipment_date=today + timedelta(days=30), port_of_loading="Genoa", port_of_discharge="Hamburg",
        incoterms="FOB", goods_description="machines",
        documents_required=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"), DocumentRequirement(doc_type="BILL_OF_LADING")])))
    assert lc.status == "ISSUED" and lc.contingent_booked_eur == Decimal("200000")
    sent_to = {i["payload"]["to"][0] for i in eng.state.outbox.values() if i["kind"] == "EMAIL" and i["deal_ref"] == lc.ref
               and "Documentary credit issued" in i["payload"]["subject"]}
    assert sent_to == {"treasury@bm-ag.example"}  # contact from the customer master, not the engine's copy

    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=today, claimed_amount=Decimal("200000"), documents=[
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", issue_date=today, fields={
            "amount": "200000", "currency": "EUR", "beneficiary_name": "Milano Meccanica S.p.A.",
            "applicant_name": "Bayerische Maschinenbau AG", "goods_description": "machines"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=1, issue_date=today, fields={
            "on_board_date": today.isoformat(), "port_of_loading": "Genoa", "port_of_discharge": "Hamburg",
            "clean": True, "signed_by": "CARRIER", "originals_issued": 1})])
    lc = asyncio.run(eng.present_documents(lc.ref, p))
    assert lc.status == "CLOSED" and lc.contingent_booked_eur == 0
    # the local copy had EUR 1.2m, the ledger said EUR 1,000: financing is decided on the ledger's figure
    assert lc.import_loan_outstanding > Decimal("198000")
