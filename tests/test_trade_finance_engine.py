"""
End-to-end tests of the Trade Finance Operations Engine:
LCs (UCP 600), demand guarantees (URDG 758) and surety bonds, across all bank systems.
"""

import asyncio
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.vault import ZeroTrustVault
from auto_connect.trade import rules, swift
from auto_connect.trade.engine import TradeFinanceEngine, WorkflowError
from auto_connect.trade.models import (
    Demand, DocumentRequirement, GuaranteeTerms, LCTerms, Party, Presentation, PresentedDocument, SuretyClaim, SuretyTerms,
)
from auto_connect.trade.state import StateStore, seed_state

TODAY = date(2026, 11, 9)  # Monday


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def engine(tmp_path):
    store = StateStore(path=str(tmp_path / "state.json"), seed=False)
    store.state = seed_state(TODAY)
    vault, mesh = ZeroTrustVault(), A2AEcosystemMesh()
    eng = TradeFinanceEngine(store, vault, mesh, UniversalAPIExecutor(vault))
    return eng


def lc_terms(**over):
    base = dict(
        applicant_customer_id="DE-CORP-99214",
        beneficiary=Party(name="Milano Meccanica S.p.A.", address="Via Dante 14, 20121 Milano", country="IT"),
        advising_bank_bic="BNCAITMMXXX",
        currency="EUR",
        amount=Decimal("300000"),
        tolerance_plus_pct=Decimal("5"),
        tolerance_minus_pct=Decimal("5"),
        expiry_date=TODAY + timedelta(days=90),
        expiry_place="Hamburg",
        latest_shipment_date=TODAY + timedelta(days=60),
        port_of_loading="Genoa, Italy",
        port_of_discharge="Hamburg, Germany",
        incoterms="CIF",
        incoterms_place="Hamburg",
        goods_description="Photovoltaic solar modules 450W",
        documents_required=[
            DocumentRequirement(doc_type="COMMERCIAL_INVOICE", originals=1, copies=2),
            DocumentRequirement(doc_type="BILL_OF_LADING", originals=3),
            DocumentRequirement(doc_type="INSURANCE_DOCUMENT", originals=1),
            DocumentRequirement(doc_type="PACKING_LIST", originals=1),
        ],
    )
    base.update(over)
    return LCTerms(**base)


def complying_docs(t: LCTerms, ship: date, amount: Decimal, applicant="Hanse Solartechnik GmbH", insurance=True):
    docs = [
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", originals=1, copies=2, issue_date=ship,
                          fields={"amount": str(amount), "currency": t.currency, "beneficiary_name": t.beneficiary.name,
                                  "beneficiary_country": "IT", "applicant_name": applicant,
                                  "goods_description": "Photovoltaic solar modules 450W, 1,200 pcs, CIF Hamburg"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=3, issue_date=ship,
                          fields={"on_board_date": ship.isoformat(), "port_of_loading": "Genoa",
                                  "port_of_discharge": "Hamburg", "clean": True, "signed_by": "CARRIER",
                                  "originals_issued": 3, "vessel_imo": "IMO9811000", "carrier": "Mediterranean Lines",
                                  "carrier_country": "CH", "goods_description": "solar modules"}),
        PresentedDocument(doc_type="PACKING_LIST", originals=1, issue_date=ship, fields={"goods_description": "solar modules"}),
    ]
    if insurance:
        docs.append(PresentedDocument(doc_type="INSURANCE_DOCUMENT", originals=1, issue_date=ship,
                                      fields={"coverage_amount": str(amount * Decimal("1.1")), "currency": t.currency}))
    return docs


# ---------------------------------------------------------------------------
# LETTERS OF CREDIT
# ---------------------------------------------------------------------------

def test_lc_clean_sight_payment_end_to_end(engine):
    st = engine.state
    acct = st.customers["DE-CORP-99214"].current_account_iban
    bal0 = st.accounts[acct].balance
    lc = run(engine.apply_lc(lc_terms()))
    assert lc.status == "ISSUED", [e.detail for e in lc.history]
    mts = [st.swift_messages[m].mt_type for m in lc.swift_message_ids]
    assert mts == ["700"]
    fin = st.swift_messages[lc.swift_message_ids[0]].text
    assert ":32B:EUR300000,00" in fin and ":40E:UCP LATEST VERSION" in fin and ":39A:5/5" in fin
    assert os.path.exists(st.swift_messages[lc.swift_message_ids[0]].file_path)
    fac = st.facilities["FAC-99214-LC"]
    assert fac.utilised == Decimal("315000.00")  # amount + 5% tolerance
    assert any(f.fee_type == "LC_ISSUANCE_COMMISSION" for f in lc.fees)

    ship = TODAY + timedelta(days=20)
    run(engine.run_daily_batch(ship + timedelta(days=5)))
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presenting_bank_ref="UC-DOC-1", presentation_date=engine.today,
                     claimed_amount=Decimal("300000"), documents=complying_docs(lc.terms, ship, Decimal("300000")))
    lc = run(engine.present_documents(lc.ref, p))
    assert lc.presentations[0].status == "PAID", lc.presentations[0].discrepancies
    assert lc.status == "CLOSED"
    mts = [st.swift_messages[m].mt_type for m in lc.swift_message_ids]
    assert mts == ["700", "756", "pacs.009"]
    fees = sum(f.amount for f in lc.fees)
    assert st.accounts[acct].balance == bal0 - Decimal("300000") - fees
    assert fac.utilised == 0 and not fac.earmarks
    assert engine._contingent_open(lc, "LC") == 0
    assert st.ledger_balanced()


def test_lc_limit_shortfall_reallocated_then_discrepancy_waived_by_applicant(engine):
    st = engine.state
    lc = run(engine.apply_lc(lc_terms(amount=Decimal("450000"), tolerance_plus_pct=Decimal("0"), tolerance_minus_pct=Decimal("0"))))
    assert lc.status == "ISSUED"
    credit = next(e for e in lc.history if e.step == "CREDIT")
    assert credit.data["methods"][0]["method"] == "LIMIT_REALLOCATION"
    assert st.facilities["FAC-99214-WC"].limit == Decimal("950000.00")
    # credit_limits talked to the core ledger? (not needed here) - but engine talked to all systems:
    targets = {m.target_agent_id for m in engine.mesh.message_ledger}
    assert {"tf_customer_master", "tf_sanctions_screening", "tf_credit_limits", "tf_core_ledger", "tf_swift_gateway",
            "tf_client_portal", "tf_email_gateway"} <= targets
    # portal -> customer master -> email gateway: systems talk to each other
    assert any(m.sender_agent_id == "tf_client_portal" and m.target_agent_id == "tf_email_gateway"
               for m in engine.mesh.message_ledger)

    ship = TODAY + timedelta(days=10)
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=TODAY + timedelta(days=14),
                     claimed_amount=Decimal("450000"),
                     documents=complying_docs(lc.terms, ship, Decimal("450000"), insurance=False))
    run(engine.run_daily_batch(TODAY + timedelta(days=14)))
    lc = run(engine.present_documents(lc.ref, p))
    pres = lc.presentations[0]
    assert pres.status == "DISCREPANT_AWAITING_APPLICANT"
    assert "MISSING_INSURANCE_DOCUMENT" in [d.code for d in pres.discrepancies]
    note = st.notifications[-1]
    assert "Accept discrepancies and pay" in note["actions"]
    link = note["actions"]["Accept discrepancies and pay"]
    sig = link.split("sig=")[1]
    assert engine.verify(lc.ref, pres.presentation_id, "waive", sig)
    lc = run(engine.applicant_waiver_decision(lc.ref, pres.presentation_id, waive=True))
    assert lc.presentations[0].status == "PAID"
    assert lc.presentations[0].paid_amount == Decimal("449910.00")  # discrepancy fee deducted from proceeds
    assert lc.status == "CLOSED"
    assert st.ledger_balanced()


def test_lc_standing_waiver_instruction_pays_without_asking(engine):
    lc = run(engine.apply_lc(lc_terms()))
    ship = TODAY + timedelta(days=5)
    late = ship + timedelta(days=25)  # > 21 days after shipment -> LATE_PRESENTATION (customer pre-waived)
    run(engine.run_daily_batch(late))
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=late, claimed_amount=Decimal("300000"),
                     documents=complying_docs(lc.terms, ship, Decimal("300000")))
    lc = run(engine.present_documents(lc.ref, p))
    assert [d.code for d in lc.presentations[0].discrepancies] == ["LATE_PRESENTATION"]
    assert lc.presentations[0].waived_by == "STANDING_INSTRUCTION"
    assert lc.presentations[0].status == "PAID"


def test_lc_no_waiver_auto_refusal_then_expiry_release(engine):
    st = engine.state
    lc = run(engine.apply_lc(lc_terms(applicant_customer_id="DE-CORP-40551")))
    ship = TODAY + timedelta(days=3)
    docs = complying_docs(lc.terms, ship, Decimal("300000"), applicant="Bayerische Maschinenbau AG")
    docs[1].fields["clean"] = False  # claused B/L
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=TODAY + timedelta(days=7),
                     claimed_amount=Decimal("300000"), documents=docs)
    run(engine.run_daily_batch(TODAY + timedelta(days=7)))
    lc = run(engine.present_documents(lc.ref, p))
    assert lc.presentations[0].status == "DISCREPANT_AWAITING_APPLICANT"
    deadline = lc.presentations[0].applicant_decision_deadline
    result = run(engine.run_daily_batch(deadline + timedelta(days=1)))
    assert any("refused" in a for a in result["actions"])
    assert lc.presentations[0].status == "REFUSED"
    assert st.swift_messages[lc.swift_message_ids[-1]].mt_type == "734"
    assert ":77J:" in st.swift_messages[lc.swift_message_ids[-1]].text
    assert lc.status == "ISSUED"
    run(engine.run_daily_batch(lc.terms.expiry_date + timedelta(days=31)))
    assert lc.status == "EXPIRED"
    assert st.facilities["FAC-40551-LC"].utilised == 0
    assert engine._contingent_open(lc, "LC") == 0
    assert st.ledger_balanced()


def test_lc_usd_deferred_payment_paid_at_maturity(engine):
    st = engine.state
    t = lc_terms(applicant_customer_id="DE-CORP-40551", currency="USD", amount=Decimal("250000"),
                 available_by="DEFERRED_PAYMENT", usance_days=60, tolerance_plus_pct=Decimal("0"),
                 tolerance_minus_pct=Decimal("0"))
    lc = run(engine.apply_lc(t))
    assert lc.status == "ISSUED"
    assert lc.limit_utilisation_eur == Decimal("230000.00")  # USD 250k * 0.92
    ship = TODAY + timedelta(days=10)
    run(engine.run_daily_batch(TODAY + timedelta(days=12)))
    p = Presentation(presenting_bank_bic="CHASUS33XXX", presentation_date=engine.today, claimed_amount=Decimal("250000"),
                     documents=complying_docs(t, ship, Decimal("250000"), applicant="Bayerische Maschinenbau AG"))
    lc = run(engine.present_documents(lc.ref, p))
    pres = lc.presentations[0]
    assert pres.status == "ACCEPTED" and lc.status == "ACCEPTED_AWAITING_MATURITY"
    assert pres.maturity_date >= ship + timedelta(days=60)
    run(engine.run_daily_batch(pres.maturity_date))
    assert pres.status == "PAID" and lc.status == "CLOSED"
    assert st.accounts["GL_FX_POSITION_USD"].balance == 0 and st.accounts["GL_FX_POSITION_EUR"].balance == 0
    assert st.ledger_balanced()


def test_lc_amendment_increase_and_beneficiary_rejection_by_presentation(engine):
    lc = run(engine.apply_lc(lc_terms(tolerance_plus_pct=Decimal("0"), tolerance_minus_pct=Decimal("0"))))
    lc = run(engine.amend_lc(lc.ref, {"amount_delta": "50000", "expiry_date": (TODAY + timedelta(days=150)).isoformat()}))
    assert lc.amount == Decimal("350000.00") and lc.amendments[0].status == "PENDING_BENEFICIARY"
    assert engine.state.swift_messages[lc.swift_message_ids[-1]].mt_type == "707"
    # beneficiary presents for the ORIGINAL amount with partial shipments not allowed -> complies only with original terms
    ship = TODAY + timedelta(days=5)
    run(engine.run_daily_batch(TODAY + timedelta(days=6)))
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=engine.today, claimed_amount=Decimal("300000"),
                     documents=complying_docs(lc.terms, ship, Decimal("300000")))
    lc = run(engine.present_documents(lc.ref, p))
    assert lc.amendments[0].status == "REJECTED"
    assert lc.presentations[0].status == "PAID"
    assert engine.state.ledger_balanced()


def test_lc_sanctions_hit_escalates_and_rejection_declines(engine):
    t = lc_terms(beneficiary=Party(name="Volkov Maritime Trading LLC", address="Novorossiysk", country="RU"))
    lc = run(engine.apply_lc(t))
    assert lc.status == "ON_HOLD_SANCTIONS"
    exc = engine.state.exceptions[lc.open_exceptions[0]]
    assert exc.category == "SANCTIONS" and "BLOCKED" in exc.reason
    assert not lc.swift_message_ids  # nothing left the bank
    run(engine.resolve_exception(exc.exception_id, approve=False, resolved_by="compliance.officer", note="True match"))
    assert lc.status == "REJECTED"


def test_lc_similar_name_in_other_country_is_discounted(engine):
    # fuzzy similarity (~0.89) + different domicile -> false positive discounted automatically
    t = lc_terms(beneficiary=Party(name="Volkova Marine Trading SRL", address="Genova", country="IT"))
    lc = run(engine.apply_lc(t))
    sanc = next(e for e in lc.history if e.step == "SANCTIONS")
    assert lc.status == "ISSUED" and "DISCOUNTED" in sanc.detail
    # identical name but different domicile -> compliance must confirm identity
    t2 = lc_terms(beneficiary=Party(name="Volkov Maritime Trading SRL", address="Genova", country="IT"))
    lc2 = run(engine.apply_lc(t2))
    assert lc2.status == "ON_HOLD_SANCTIONS"
    assert "REVIEW_REQUIRED" in engine.state.exceptions[lc2.open_exceptions[0]].reason


def test_lc_embargoed_port_and_dual_use_goods(engine):
    lc = run(engine.apply_lc(lc_terms(port_of_loading="Sevastopol, Crimea")))
    assert lc.status == "ON_HOLD_SANCTIONS"
    lc2 = run(engine.apply_lc(lc_terms(goods_description="Night vision devices")))
    assert lc2.status == "ON_HOLD_SANCTIONS"
    lc3 = run(engine.apply_lc(lc_terms(goods_description="Night vision devices", export_license_ref="BAFA-2026-77812")))
    assert lc3.status == "ISSUED"


def test_lc_data_quality_and_credit_exceptions(engine):
    bad = run(engine.apply_lc(lc_terms(additional_conditions=["Goods must be shipped by vessel not older than 15 years"])))
    assert bad.status == "ON_HOLD_DATA_QUALITY"
    big = run(engine.apply_lc(lc_terms(applicant_customer_id="DE-CORP-40551", amount=Decimal("5000000"))))
    assert big.status == "ON_HOLD_CREDIT"
    exc_id = big.open_exceptions[0]
    run(engine.resolve_exception(exc_id, approve=True, resolved_by="credit.committee", note="Approved by committee"))
    assert big.status == "ISSUED"
    assert "CREDIT" not in big.approvals  # approval used once


def test_ucp600_rules_unit():
    t = lc_terms()
    from auto_connect.trade.models import LetterOfCredit
    lc = LetterOfCredit(ref="X", customer_id="C", status="ISSUED", currency="EUR", amount=t.amount, outstanding=t.amount, terms=t)
    ship = t.latest_shipment_date + timedelta(days=2)
    docs = complying_docs(t, ship, Decimal("330000"))
    docs[3].fields["coverage_amount"] = "300000"
    p = Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=ship + timedelta(days=1),
                     claimed_amount=Decimal("330000"), documents=docs)
    codes = {d.code for d in rules.examine_ucp600(lc, p, "Hanse Solartechnik GmbH")}
    assert {"LATE_SHIPMENT", "INVOICE_EXCEEDS_CREDIT", "INSURANCE_UNDERINSURED", "DRAWING_EXCEEDS_AVAILABLE"} <= codes


def test_swift_and_iban_formats(engine):
    lc = run(engine.apply_lc(lc_terms()))
    text = engine.state.swift_messages[lc.swift_message_ids[0]].text
    assert text.startswith("{1:F01DEMODEMMAXXX") and "{2:I700BNCAITMMXXXXN}" in text and text.endswith("-}")
    body = text.split("{4:\n")[1]
    assert all(len(line) <= 70 for line in body.splitlines())
    assert swift.swift_amount(Decimal("1234.5")) == "1234,50"
    assert rules.is_valid_iban("DE89370400440532013000")
    assert not rules.is_valid_iban("DE89370400440532013001")
    for c in engine.state.customers.values():
        assert rules.is_valid_iban(c.current_account_iban)


# ---------------------------------------------------------------------------
# GUARANTEES
# ---------------------------------------------------------------------------

def gtee_terms(**over):
    base = dict(
        applicant_customer_id="DE-CORP-40551",
        beneficiary=Party(name="Saudi Industrial Projects Co", address="Riyadh", country="SA",
                          iban="SA0380000000608010167519"),
        beneficiary_bank_bic="NCBKSAJEXXX",
        guarantee_type="PERFORMANCE",
        currency="EUR",
        amount=Decimal("200000"),
        expiry_date=TODAY + timedelta(days=365),
        underlying_transaction="Contract No. SIP-2026-114 for the supply of a CNC production line",
        underlying_contract_value=Decimal("2000000"),
    )
    base.update(over)
    return GuaranteeTerms(**base)


def test_guarantee_issue_demand_reject_pay_and_release(engine):
    st = engine.state
    g = run(engine.apply_guarantee(gtee_terms()))
    assert g.status == "ISSUED"
    fin = st.swift_messages[g.swift_message_ids[0]]
    assert fin.mt_type == "760" and ":40C:URDG" in fin.text and ":22D:GUAR" in fin.text
    # non-complying demand: no statement of breach
    d1 = Demand(presentation_date=TODAY + timedelta(days=30), amount=Decimal("50000"), currency="EUR",
                documents=[PresentedDocument(doc_type="DEMAND", fields={"guarantee_ref": g.ref})])
    run(engine.run_daily_batch(TODAY + timedelta(days=30)))
    g = run(engine.present_demand(g.ref, d1))
    assert g.demands[0].status == "REJECTED"
    assert "NO_STATEMENT_OF_BREACH" in [x.code for x in g.demands[0].discrepancies]
    # complying partial demand
    d2 = Demand(presentation_date=engine.today, amount=Decimal("50000"), currency="EUR",
                documents=[PresentedDocument(doc_type="DEMAND", fields={"guarantee_ref": g.ref,
                                                                        "breach_statement": "Delivery delayed by 12 weeks"})])
    g = run(engine.present_demand(g.ref, d2))
    assert g.demands[1].status == "PAID" and g.outstanding == Decimal("150000.00") and g.status == "ISSUED"
    assert "pacs.008" in [st.swift_messages[m].mt_type for m in g.swift_message_ids]
    # quarterly commission & expiry release
    run(engine.run_daily_batch(TODAY + timedelta(days=100)))
    assert sum(1 for f in g.fees if f.fee_type == "AVAL_COMMISSION") == 2
    run(engine.run_daily_batch(g.terms.expiry_date + timedelta(days=1)))
    assert g.status == "EXPIRED"
    assert st.swift_messages[g.swift_message_ids[-1]].mt_type == "769"
    assert st.facilities["FAC-40551-GTEE"].utilised == 0
    assert st.ledger_balanced()


def test_guarantee_extend_or_pay(engine):
    g = run(engine.apply_guarantee(gtee_terms(expiry_date=TODAY + timedelta(days=40))))
    new_exp = TODAY + timedelta(days=220)
    d = Demand(presentation_date=TODAY + timedelta(days=35), amount=Decimal("200000"), currency="EUR", extend_or_pay=True,
               requested_new_expiry=new_exp,
               documents=[PresentedDocument(doc_type="DEMAND", fields={"breach_statement": "Works incomplete"})])
    run(engine.run_daily_batch(TODAY + timedelta(days=35)))
    g = run(engine.present_demand(g.ref, d))
    assert g.demands[0].status == "SUSPENDED_EXTEND_OR_PAY"
    g = run(engine.extend_or_pay_decision(g.ref, d.demand_id, extend=True))
    assert g.demands[0].status == "WITHDRAWN" and g.terms.expiry_date == new_exp and g.status == "ISSUED"
    assert engine.state.swift_messages[g.swift_message_ids[-1]].mt_type == "767"


def test_open_ended_guarantee_needs_credit_approval(engine):
    g = run(engine.apply_guarantee(gtee_terms(expiry_date=None)))
    assert g.status == "ON_HOLD_CREDIT"
    run(engine.resolve_exception(g.open_exceptions[0], True, "credit.officer", "Open-ended accepted, secured"))
    assert g.status == "ISSUED"


# ---------------------------------------------------------------------------
# SURETY BONDS
# ---------------------------------------------------------------------------

def surety_terms(**over):
    base = dict(
        principal_customer_id="DE-CORP-77310",
        obligee=Party(name="Stadt Koeln - Gebaeudewirtschaft", address="Willy-Brandt-Platz 2, 50679 Koeln", country="DE",
                      iban="DE12370501980001234567"),
        bond_type="CONTRACT_PERFORMANCE",
        amount=Decimal("150000"),
        contract_value=Decimal("1500000"),
        expiry_date=TODAY + timedelta(days=540),
        underlying_contract="Construction contract (VOB/B) school extension Koeln-Deutz, Lot 3",
    )
    base.update(over)
    return SuretyTerms(**base)


def claim_docs(accessory=True):
    docs = [PresentedDocument(doc_type="DEMAND", fields={"signed": True})]
    if accessory:
        docs += [PresentedDocument(doc_type="NOTICE_OF_DEFAULT"), PresentedDocument(doc_type="EVIDENCE_OF_CLAIM")]
    return docs


def test_surety_accessory_claim_paid_after_principal_silence(engine):
    st = engine.state
    b = run(engine.apply_surety(surety_terms()))
    assert b.status == "ISSUED"
    assert any(d["doc_type"] == "SURETY_DEED" for d in st.documents)
    run(engine.run_daily_batch(TODAY + timedelta(days=60)))
    c = SuretyClaim(presentation_date=engine.today, amount=Decimal("40000"), currency="EUR", documents=claim_docs())
    b = run(engine.present_surety_claim(b.ref, c))
    assert b.claims[0].status == "AWAITING_PRINCIPAL"
    run(engine.run_daily_batch(b.claims[0].principal_deadline + timedelta(days=1)))
    assert b.claims[0].status == "PAID" and b.outstanding == Decimal("110000.00")
    assert st.ledger_balanced()


def test_surety_disputed_claim_goes_to_legal_and_rejection(engine):
    b = run(engine.apply_surety(surety_terms()))
    c = SuretyClaim(presentation_date=TODAY, amount=Decimal("30000"), currency="EUR", documents=claim_docs())
    b = run(engine.present_surety_claim(b.ref, c))
    b = run(engine.principal_claim_response(b.ref, c.claim_id, dispute=True, reason="Defects remedied, acceptance protocol signed"))
    assert b.status == "ON_HOLD_LEGAL"
    run(engine.resolve_exception(b.open_exceptions[0], approve=False, resolved_by="legal", note="Claim unfounded"))
    assert b.claims[0].status == "REJECTED" and b.status == "ISSUED" and b.outstanding == Decimal("150000.00")


def test_surety_first_demand_and_underwriting_limits(engine):
    b = run(engine.apply_surety(surety_terms(on_first_demand=True)))
    c = SuretyClaim(presentation_date=TODAY, amount=Decimal("150000"), currency="EUR", documents=claim_docs(False))
    b = run(engine.present_surety_claim(b.ref, c))
    assert b.claims[0].status == "PAID" and b.status == "CLOSED"
    bad = run(engine.apply_surety(surety_terms(bond_type="RENT_DEPOSIT", amount=Decimal("9000"),
                                               monthly_rent=Decimal("2000"), contract_value=None)))
    assert bad.status == "ON_HOLD_DATA_QUALITY"
    too_big = run(engine.apply_surety(surety_terms(amount=Decimal("300000"))))  # 20% of contract value
    assert too_big.status == "ON_HOLD_DATA_QUALITY"


def test_kyc_overdue_blocks_and_persistence_roundtrip(engine, tmp_path):
    engine.state.customers["DE-CORP-77310"].kyc_next_review = TODAY - timedelta(days=1)
    b = run(engine.apply_surety(surety_terms()))
    assert b.status == "ON_HOLD_KYC"
    engine.store.save()
    again = StateStore(path=engine.store.path)
    assert again.state.deals[b.ref].status == "ON_HOLD_KYC"
    assert again.state.ledger_balanced()


def test_invalid_requests_raise(engine):
    with pytest.raises(WorkflowError):
        run(engine.present_documents("ILC2026999999", Presentation(presenting_bank_bic="BNCAITMMXXX", presentation_date=TODAY,
                                                                  claimed_amount=Decimal("1"), documents=[])))
    with pytest.raises(WorkflowError):
        run(engine.run_daily_batch(TODAY - timedelta(days=1)))
