"""
Data model of the trade finance back office: customers, facilities, accounts,
ledger, and the three products handled end to end:

* Documentary Letters of Credit (ICC UCP 600)
* Demand Guarantees / Standby LCs (ICC URDG 758)
* Surety bonds (German "Bürgschaft", accessory or on first demand, §§ 765 ff. BGB)
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
import uuid

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


# ---------------------------------------------------------------------------
# Parties & master data
# ---------------------------------------------------------------------------

class Party(BaseModel):
    name: str
    address: str = ""
    country: str = Field(description="ISO 3166 alpha-2 country code")
    bic: Optional[str] = None
    iban: Optional[str] = None
    lei: Optional[str] = None
    email: Optional[str] = None


class Customer(BaseModel):
    customer_id: str
    name: str
    address: str = ""
    country: str
    email: str
    relationship_manager_email: str = "rm@bank.local"
    kyc_status: Literal["VERIFIED", "PENDING", "EXPIRED", "BLOCKED"] = "VERIFIED"
    kyc_next_review: date
    risk_rating: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    current_account_iban: str
    # Standing instructions agreed with the client (allow full straight-through processing)
    auto_waive_discrepancy_codes: List[str] = Field(
        default_factory=list,
        description="Discrepancy codes the applicant has pre-agreed to waive (UCP 600 Art. 16(b) standing waiver)",
    )
    allow_cash_collateral: bool = True
    allow_limit_reallocation: bool = True
    auto_post_financing: bool = True


class Facility(BaseModel):
    facility_id: str
    customer_id: str
    product: Literal["LC", "GUARANTEE", "SURETY", "WORKING_CAPITAL", "BANK_CONFIRMATION"]
    currency: str = "EUR"
    limit: Decimal
    utilised: Decimal = Decimal("0")
    earmarks: Dict[str, Decimal] = Field(default_factory=dict)
    temporary_excess: Dict[str, Decimal] = Field(
        default_factory=dict, description="Deal-specific limit excess (credit approval / delegated authority), removed on release")
    expiry: Optional[date] = None
    can_transfer_to: List[str] = Field(default_factory=list, description="Products this facility may lend headroom to")

    @property
    def available(self) -> Decimal:
        return self.limit - self.utilised


class Account(BaseModel):
    account_id: str  # IBAN for customer accounts, GL code for internal accounts
    owner: str  # customer_id or "BANK"
    currency: str = "EUR"
    balance: Decimal = Decimal("0")
    overdraft_limit: Decimal = Decimal("0")
    blocked: Dict[str, Decimal] = Field(default_factory=dict, description="Cash collateral blocked per deal ref")
    on_balance_sheet: bool = True

    @property
    def free_balance(self) -> Decimal:
        return self.balance + self.overdraft_limit - sum(self.blocked.values(), Decimal("0"))


class LedgerEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: new_id("JE"))
    booking_date: date
    value_date: date
    deal_ref: str
    debit_account: str
    credit_account: str
    amount: Decimal
    currency: str
    narrative: str
    created_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Workflow bookkeeping
# ---------------------------------------------------------------------------

class WorkflowEvent(BaseModel):
    at: str = Field(default_factory=utcnow)
    business_date: Optional[date] = None
    step: str
    system: str
    outcome: str
    detail: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class ExceptionItem(BaseModel):
    exception_id: str = Field(default_factory=lambda: new_id("EXC"))
    deal_ref: str
    category: Literal["SANCTIONS", "KYC", "CREDIT", "LEGAL", "DATA_QUALITY", "OPERATIONS"]
    reason: str
    resume_step: Optional[str] = None
    status: Literal["OPEN", "APPROVED", "REJECTED"] = "OPEN"
    created_at: str = Field(default_factory=utcnow)
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    approvers: List[str] = Field(default_factory=list, description="Users who approved so far (four-eyes)")


class Discrepancy(BaseModel):
    code: str
    rule: str
    description: str


class SwiftMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: new_id("SWF"))
    mt_type: str
    direction: Literal["OUT", "IN"] = "OUT"
    sender_bic: str
    receiver_bic: str
    deal_ref: str
    text: str
    created_at: str = Field(default_factory=utcnow)
    file_path: Optional[str] = None
    status: str = Field(default="RELEASED", description="QUEUED until the transaction is committed, then RELEASED")


# ---------------------------------------------------------------------------
# Documents presented under an LC / supporting a demand or claim
# ---------------------------------------------------------------------------

DocType = Literal[
    "COMMERCIAL_INVOICE",
    "BILL_OF_LADING",
    "AIR_WAYBILL",
    "INSURANCE_DOCUMENT",
    "PACKING_LIST",
    "CERTIFICATE_OF_ORIGIN",
    "INSPECTION_CERTIFICATE",
    "BENEFICIARY_STATEMENT",
    "DEMAND",
    "STATEMENT_OF_BREACH",
    "NOTICE_OF_DEFAULT",
    "EVIDENCE_OF_CLAIM",
    "GUARANTOR_STATEMENT",
    "BILL_OF_EXCHANGE",
    "OTHER",
]


class DocumentRequirement(BaseModel):
    doc_type: DocType
    originals: int = 1
    copies: int = 0
    note: str = ""


class PresentedDocument(BaseModel):
    doc_type: DocType
    originals: int = 1
    copies: int = 0
    issue_date: Optional[date] = None
    issuer: Optional[str] = None
    fields: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Data extracted from the document, e.g. invoice: amount, currency, goods_description, "
            "beneficiary_name, beneficiary_address, applicant_name; bill of lading: on_board_date, "
            "port_of_loading, port_of_discharge, clean, signed_by, originals_issued, transhipment, containerised; "
            "insurance: coverage_amount, currency, covers_from, covers_to"
        ),
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

class LCTerms(BaseModel):
    applicant_customer_id: str
    beneficiary: Party
    advising_bank_bic: str
    currency: str = "EUR"
    amount: Decimal
    tolerance_plus_pct: Decimal = Decimal("0")
    tolerance_minus_pct: Decimal = Decimal("0")
    available_by: Literal["SIGHT_PAYMENT", "DEFERRED_PAYMENT", "ACCEPTANCE", "NEGOTIATION"] = "SIGHT_PAYMENT"
    usance_days: int = 0
    expiry_date: date
    expiry_place: str = "Germany"
    latest_shipment_date: date
    port_of_loading: str
    port_of_discharge: str
    incoterms: str = Field(default="CIF", description="EXW/FCA/FOB/CFR/CIF/CPT/CIP/DAP/DDP ...")
    incoterms_place: str = ""
    goods_description: str
    partial_shipments_allowed: bool = False
    transhipment_allowed: bool = False
    documents_required: List[DocumentRequirement]
    presentation_period_days: int = 21
    additional_conditions: List[str] = Field(default_factory=list)
    country_of_origin: Optional[str] = None
    export_license_ref: Optional[str] = None
    charges: str = "ALL CHARGES OUTSIDE GERMANY ARE FOR BENEFICIARY ACCOUNT"
    confirmation: Literal["WITHOUT", "MAY ADD", "CONFIRM"] = "WITHOUT"
    vessel_imo: Optional[str] = None


class Presentation(BaseModel):
    presentation_id: str = Field(default_factory=lambda: new_id("PRS"))
    presenting_bank_bic: str
    presenting_bank_ref: str = ""
    presentation_date: date
    claimed_amount: Decimal
    documents: List[PresentedDocument]
    status: Literal[
        "UNDER_EXAMINATION", "COMPLYING", "DISCREPANT_AWAITING_APPLICANT", "DISCREPANT_WAIVED", "REFUSED", "PAID", "ACCEPTED"
    ] = "UNDER_EXAMINATION"
    discrepancies: List[Discrepancy] = Field(default_factory=list)
    examination_deadline: Optional[date] = None
    applicant_decision_deadline: Optional[date] = None
    maturity_date: Optional[date] = None
    paid_amount: Decimal = Decimal("0")
    discrepancy_fee: Decimal = Decimal("0")
    waived_by: Optional[str] = None


class Amendment(BaseModel):
    number: int
    date: date
    changes: Dict[str, Any]
    status: Literal["PENDING_APPROVAL", "PENDING_BENEFICIARY", "ACCEPTED", "REJECTED", "EFFECTIVE"] = "EFFECTIVE"
    previous: Dict[str, Any] = Field(default_factory=dict, description="Values before the amendment (for Art. 10 rejection)")


class GuaranteeTerms(BaseModel):
    applicant_customer_id: str
    beneficiary: Party
    beneficiary_bank_bic: Optional[str] = None
    guarantee_type: Literal["PERFORMANCE", "ADVANCE_PAYMENT", "BID", "WARRANTY", "PAYMENT", "RETENTION", "STANDBY_LC"] = "PERFORMANCE"
    currency: str = "EUR"
    amount: Decimal
    expiry_date: Optional[date] = None
    underlying_transaction: str
    underlying_contract_value: Optional[Decimal] = None
    required_demand_documents: List[DocType] = Field(default_factory=lambda: ["DEMAND"])
    exclude_statement_of_breach: bool = False
    governing_law: str = "German law"
    rules: str = "URDG 758"
    reduction_clause: Optional[str] = None
    # counter-guarantee: we instruct the beneficiary's bank (beneficiary_bank_bic) to issue its own local guarantee
    counter_guarantee: bool = False
    local_guarantee_expiry: Optional[date] = Field(default=None, description="Expiry of the local guarantee issued by the foreign bank")


class Demand(BaseModel):
    demand_id: str = Field(default_factory=lambda: new_id("DMD"))
    presentation_date: date
    amount: Decimal
    currency: str
    documents: List[PresentedDocument]
    extend_or_pay: bool = False
    requested_new_expiry: Optional[date] = None
    status: Literal["UNDER_EXAMINATION", "COMPLYING", "REJECTED", "SUSPENDED_EXTEND_OR_PAY", "WITHDRAWN", "PAID"] = "UNDER_EXAMINATION"
    discrepancies: List[Discrepancy] = Field(default_factory=list)
    examination_deadline: Optional[date] = None
    suspension_end: Optional[date] = None


class SuretyTerms(BaseModel):
    principal_customer_id: str
    obligee: Party
    bond_type: Literal["CONTRACT_PERFORMANCE", "WARRANTY", "ADVANCE_PAYMENT", "RENT_DEPOSIT", "CUSTOMS", "BID"] = "CONTRACT_PERFORMANCE"
    on_first_demand: bool = False
    currency: str = "EUR"
    amount: Decimal
    expiry_date: Optional[date] = None
    underlying_contract: str
    contract_value: Optional[Decimal] = None
    advance_payment_amount: Optional[Decimal] = None
    monthly_rent: Optional[Decimal] = None
    principal_response_days: int = 10


class SuretyClaim(BaseModel):
    claim_id: str = Field(default_factory=lambda: new_id("CLM"))
    presentation_date: date
    amount: Decimal
    currency: str
    documents: List[PresentedDocument]
    status: Literal["UNDER_EXAMINATION", "AWAITING_PRINCIPAL", "DISPUTED", "REJECTED", "PAID"] = "UNDER_EXAMINATION"
    discrepancies: List[Discrepancy] = Field(default_factory=list)
    principal_deadline: Optional[date] = None
    principal_response: Optional[str] = None


class FeeCharge(BaseModel):
    date: date
    fee_type: str
    amount: Decimal
    currency: str = "EUR"
    period_end: Optional[date] = None


class _DealBase(BaseModel):
    ref: str
    customer_id: str
    status: str
    currency: str
    amount: Decimal
    outstanding: Decimal
    issue_date: Optional[date] = None
    created_at: str = Field(default_factory=utcnow)
    created_by: Optional[str] = Field(default=None, description="User who entered the deal (maker)")
    facility_id: Optional[str] = None
    limit_utilisation_eur: Decimal = Decimal("0")
    cash_collateral_eur: Decimal = Decimal("0")
    contingent_booked_eur: Optional[Decimal] = Field(
        default=None, description="Off-balance-sheet liability booked for this deal (tracked here, not read back from the ledger)")
    fees: List[FeeCharge] = Field(default_factory=list)
    commission_paid_until: Optional[date] = None
    swift_message_ids: List[str] = Field(default_factory=list)
    history: List[WorkflowEvent] = Field(default_factory=list)
    open_exceptions: List[str] = Field(default_factory=list)
    approvals: List[str] = Field(default_factory=list, description="Exception categories approved for this deal")
    amendments: List[Amendment] = Field(default_factory=list)
    attachments: List[Dict[str, Any]] = Field(
        default_factory=list, description="Original documents uploaded for this deal (stored in the database with SHA-256)")
    application_id: Optional[str] = Field(default=None, description="Online / branch application this deal came from")


class LetterOfCredit(_DealBase):
    product: Literal["LC"] = "LC"
    terms: LCTerms
    presentations: List[Presentation] = Field(default_factory=list)
    import_loan_outstanding: Decimal = Decimal("0")


class Guarantee(_DealBase):
    product: Literal["GUARANTEE"] = "GUARANTEE"
    terms: GuaranteeTerms
    demands: List[Demand] = Field(default_factory=list)


class SuretyBond(_DealBase):
    product: Literal["SURETY"] = "SURETY"
    terms: SuretyTerms
    claims: List[SuretyClaim] = Field(default_factory=list)


class ExportLCTerms(BaseModel):
    """A documentary credit issued by a foreign bank in favour of our client (received by MT700)."""
    beneficiary_customer_id: str
    issuing_bank_bic: str
    issuing_bank_lc_ref: str
    applicant: Party
    reimbursing_bank_bic: Optional[str] = None
    currency: str = "EUR"
    amount: Decimal
    tolerance_plus_pct: Decimal = Decimal("0")
    tolerance_minus_pct: Decimal = Decimal("0")
    available_by: Literal["SIGHT_PAYMENT", "DEFERRED_PAYMENT", "ACCEPTANCE", "NEGOTIATION"] = "SIGHT_PAYMENT"
    usance_days: int = 0
    expiry_date: date
    expiry_place: str = "Germany"
    latest_shipment_date: date
    port_of_loading: str
    port_of_discharge: str
    incoterms: str = "FOB"
    goods_description: str
    partial_shipments_allowed: bool = False
    transhipment_allowed: bool = False
    documents_required: List[DocumentRequirement]
    presentation_period_days: int = 21
    additional_conditions: List[str] = Field(default_factory=list)
    confirmation_instructions: Literal["WITHOUT", "MAY ADD", "CONFIRM"] = "WITHOUT"
    beneficiary_requests_confirmation: bool = False
    transferable: bool = False


class ExportLC(_DealBase):
    product: Literal["EXPORT_LC"] = "EXPORT_LC"
    terms: ExportLCTerms
    confirmed: bool = False
    presentations: List[Presentation] = Field(default_factory=list)
    transfers: List[Dict[str, Any]] = Field(default_factory=list)
    receivable_outstanding: Decimal = Decimal("0")


class CollectionTerms(BaseModel):
    """Documentary collection under ICC URC 522 (no bank undertaking - documents against payment / acceptance)."""
    direction: Literal["EXPORT", "IMPORT"]
    customer_id: str = Field(description="EXPORT: drawer (our exporter); IMPORT: drawee (our importer)")
    counterparty: Party = Field(description="EXPORT: drawee abroad; IMPORT: drawer abroad")
    counterparty_bank_bic: str = Field(description="EXPORT: collecting bank; IMPORT: remitting bank")
    counterparty_bank_ref: str = ""
    currency: str = "EUR"
    amount: Decimal
    release_against: Literal["PAYMENT", "ACCEPTANCE"] = "PAYMENT"
    usance_days: int = 0
    documents: List[DocumentRequirement]
    goods_description: str = ""
    protest_if_unpaid: bool = False
    charges_for: Literal["DRAWEE", "DRAWER"] = "DRAWEE"
    drawee_response_days: int = 7


class Collection(_DealBase):
    product: Literal["COLLECTION"] = "COLLECTION"
    terms: CollectionTerms
    maturity_date: Optional[date] = None
    accepted_on: Optional[date] = None
    response_deadline: Optional[date] = None
    paid_amount: Decimal = Decimal("0")


class Application(BaseModel):
    """A client's application exactly as submitted (client portal or entered by staff). It is saved before any
    processing starts, so nothing a client sends is ever lost - even if a bank system is down at that moment."""
    application_id: str = Field(default_factory=lambda: new_id("APP"))
    customer_id: str
    product: Literal["LC", "GUARANTEE", "SURETY", "COLLECTION"]
    details: Dict[str, Any] = Field(description="The terms as submitted (validated)")
    channel: Literal["CLIENT_PORTAL", "BANK_STAFF"] = "CLIENT_PORTAL"
    submitted_by: Optional[str] = None
    submitted_at: str = Field(default_factory=utcnow)
    status: Literal["SUBMITTED", "PROCESSED", "REJECTED", "WAITING_FOR_SYSTEM", "WITHDRAWN", "NEEDS_CORRECTION"] = "SUBMITTED"
    problems: List[Dict[str, Any]] = Field(default_factory=list, description="What the client must correct (field, message)")
    deal_ref: Optional[str] = None
    message: str = ""
    attempts: int = 0
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    history: List[Dict[str, Any]] = Field(default_factory=list)


Deal = Annotated[Union[LetterOfCredit, Guarantee, SuretyBond, ExportLC, Collection], Field(discriminator="product")]
