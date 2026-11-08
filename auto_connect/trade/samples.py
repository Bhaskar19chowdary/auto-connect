"""Sample transactions used by the dashboard demo button and the CLI demo."""

from datetime import date, timedelta
from decimal import Decimal

from .models import (
    Demand, DocumentRequirement, GuaranteeTerms, LCTerms, Party, Presentation, PresentedDocument, SuretyClaim, SuretyTerms,
)


def sample_lc(today: date, customer_id: str = "DE-CORP-99214", amount: str = "450000") -> LCTerms:
    return LCTerms(
        applicant_customer_id=customer_id,
        beneficiary=Party(name="Milano Meccanica S.p.A.", address="Via Dante 14, 20121 Milano", country="IT"),
        advising_bank_bic="BNCAITMMXXX",
        currency="EUR",
        amount=Decimal(amount),
        expiry_date=today + timedelta(days=90),
        expiry_place="Hamburg",
        latest_shipment_date=today + timedelta(days=60),
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


def sample_presentation(t: LCTerms, ship: date, presented: date, applicant_name: str, with_insurance: bool = True) -> Presentation:
    docs = [
        PresentedDocument(doc_type="COMMERCIAL_INVOICE", originals=1, copies=2, issue_date=ship,
                          fields={"amount": str(t.amount), "currency": t.currency, "beneficiary_name": t.beneficiary.name,
                                  "beneficiary_country": t.beneficiary.country, "applicant_name": applicant_name,
                                  "goods_description": f"{t.goods_description}, CIF Hamburg"}),
        PresentedDocument(doc_type="BILL_OF_LADING", originals=3, issue_date=ship,
                          fields={"on_board_date": ship.isoformat(), "port_of_loading": t.port_of_loading.split(",")[0],
                                  "port_of_discharge": t.port_of_discharge.split(",")[0], "clean": True,
                                  "signed_by": "CARRIER", "originals_issued": 3, "goods_description": "solar modules"}),
        PresentedDocument(doc_type="PACKING_LIST", originals=1, issue_date=ship, fields={"goods_description": "solar modules"}),
    ]
    if with_insurance:
        docs.append(PresentedDocument(doc_type="INSURANCE_DOCUMENT", originals=1, issue_date=ship,
                                      fields={"coverage_amount": str(t.amount * Decimal("1.1")), "currency": t.currency}))
    return Presentation(presenting_bank_bic=t.advising_bank_bic, presenting_bank_ref="DOCS-001", presentation_date=presented,
                        claimed_amount=t.amount, documents=docs)


def sample_guarantee(today: date) -> GuaranteeTerms:
    return GuaranteeTerms(
        applicant_customer_id="DE-CORP-40551",
        beneficiary=Party(name="Saudi Industrial Projects Co", address="Riyadh", country="SA",
                          iban="SA0380000000608010167519"),
        beneficiary_bank_bic="NCBKSAJEXXX",
        guarantee_type="PERFORMANCE",
        amount=Decimal("200000"),
        expiry_date=today + timedelta(days=365),
        underlying_transaction="Contract No. SIP-2026-114 for the supply of a CNC production line",
        underlying_contract_value=Decimal("2000000"),
    )


def sample_demand(presented: date, amount: str = "50000") -> Demand:
    return Demand(presentation_date=presented, amount=Decimal(amount), currency="EUR",
                  documents=[PresentedDocument(doc_type="DEMAND",
                                               fields={"breach_statement": "Delivery of the production line delayed by 12 weeks"})])


def sample_surety(today: date) -> SuretyTerms:
    return SuretyTerms(
        principal_customer_id="DE-CORP-77310",
        obligee=Party(name="Stadt Koeln - Gebaeudewirtschaft", address="Willy-Brandt-Platz 2, 50679 Koeln", country="DE"),
        bond_type="CONTRACT_PERFORMANCE",
        amount=Decimal("150000"),
        contract_value=Decimal("1500000"),
        expiry_date=today + timedelta(days=540),
        underlying_contract="Construction contract (VOB/B) school extension Koeln-Deutz, Lot 3",
    )


def sample_surety_claim(presented: date, amount: str = "40000") -> SuretyClaim:
    return SuretyClaim(presentation_date=presented, amount=Decimal(amount), currency="EUR",
                       documents=[PresentedDocument(doc_type="DEMAND"), PresentedDocument(doc_type="NOTICE_OF_DEFAULT"),
                                  PresentedDocument(doc_type="EVIDENCE_OF_CLAIM")])
