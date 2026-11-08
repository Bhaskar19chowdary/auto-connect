"""
The bank's books for the trade finance back office: customer master, facilities,
accounts, general ledger, deals, SWIFT traffic, exception queue, reference data.

State is persisted as JSON (AUTO_CONNECT_STATE_FILE) so the system survives restarts.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, TypeAdapter

from .models import (
    Account,
    Application,
    Customer,
    Deal,
    ExceptionItem,
    Facility,
    LedgerEntry,
    SwiftMessage,
)
from .rules import DEFAULT_TARIFF


def make_iban(country: str, bban: str) -> str:
    """Builds an IBAN with correct ISO 13616 check digits."""
    rearranged = bban + country + "00"
    digits = "".join(str(int(c, 36)) for c in rearranged)
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{bban}"


class SanctionsEntry(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)
    country: str
    list_name: str
    entry_id: str


class ReferenceData(BaseModel):
    fx_eur_per_unit: Dict[str, Decimal] = Field(default_factory=dict)
    fx_rates_as_of: Optional[date] = None
    sanctions_entities: List[SanctionsEntry] = Field(default_factory=list)
    embargoed_countries: Dict[str, str] = Field(default_factory=dict)
    embargoed_regions: List[str] = Field(default_factory=list)
    dual_use_keywords: List[str] = Field(default_factory=list)
    blocked_vessels: List[str] = Field(default_factory=list)
    tariff: Dict[str, Decimal] = Field(default_factory=lambda: dict(DEFAULT_TARIFF))
    nostro_by_currency: Dict[str, str] = Field(default_factory=dict)
    correspondent_bic_by_currency: Dict[str, str] = Field(default_factory=dict)
    temporary_excess_max_pct: Decimal = Decimal("10")
    sanctions_hit_threshold: float = 0.92
    sanctions_review_threshold: float = 0.80


class BankState(BaseModel):
    business_date: date = Field(default_factory=date.today)
    customers: Dict[str, Customer] = Field(default_factory=dict)
    facilities: Dict[str, Facility] = Field(default_factory=dict)
    accounts: Dict[str, Account] = Field(default_factory=dict)
    ledger: List[LedgerEntry] = Field(default_factory=list)
    deals: Dict[str, Deal] = Field(default_factory=dict)
    applications: Dict[str, Application] = Field(default_factory=dict)
    swift_messages: Dict[str, SwiftMessage] = Field(default_factory=dict)
    exceptions: Dict[str, ExceptionItem] = Field(default_factory=dict)
    sanctions_cases: List[Dict[str, Any]] = Field(default_factory=list)
    notifications: List[Dict[str, Any]] = Field(default_factory=list)
    documents: List[Dict[str, Any]] = Field(default_factory=list)
    outbox: Dict[str, Dict[str, Any]] = Field(default_factory=dict,
                                              description="Emails / SWIFT messages waiting for the transaction to commit")
    reference: ReferenceData = Field(default_factory=ReferenceData)
    bindings: Dict[str, Dict[str, Any]] = Field(default_factory=dict, description="Connected applications serving engine roles")
    counters: Dict[str, int] = Field(default_factory=dict)
    portal_secret: str = Field(default_factory=lambda: secrets.token_hex(16))

    # ------------------------------------------------------------------
    def next_ref(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}{self.business_date.year}{n:06d}"

    def ledger_balanced(self) -> bool:
        """Double-entry invariant: all balances (incl. opening equity) sum to zero per currency."""
        totals: Dict[str, Decimal] = {}
        for acc in self.accounts.values():
            totals[acc.currency] = totals.get(acc.currency, Decimal("0")) + acc.balance
        return all(v == 0 for v in totals.values())


_DEAL_ADAPTER = TypeAdapter(Deal)


class StateStore:
    """Thread-safe holder + JSON persistence of BankState (demo / tests). Production uses DatabaseStore.

    Every engine action runs as a unit of work: begin() -> changes -> commit(), or rollback() on any error,
    which restores the books exactly as they were before the action (nothing half-booked)."""

    def __init__(self, path: str = "", seed: bool = True):
        self.path = path
        self.lock = threading.RLock()
        self.state = self._load() if path and os.path.exists(path) else (seed_state() if seed else BankState())
        self.in_transaction = False
        self._snapshot: Optional[BankState] = None
        self._files: Dict[str, Any] = {}  # demo / test mode: original documents (file_id -> (meta, bytes))
        self._pending_files: Dict[str, Any] = {}

    def begin(self) -> None:
        self._snapshot = self.state.model_copy(deep=True)
        self.in_transaction = True

    def commit(self, dirty_deals=None) -> None:
        self.in_transaction = False
        self._snapshot = None
        self._files.update(self._pending_files)
        self._pending_files = {}
        self.save()

    def rollback(self) -> None:
        if self._snapshot is not None:
            self.state = self._snapshot
        self._snapshot = None
        self._pending_files = {}
        self.in_transaction = False

    def save_file(self, meta: Dict[str, Any], data: bytes) -> None:
        (self._pending_files if self.in_transaction else self._files)[meta["file_id"]] = (dict(meta), bytes(data))

    def load_file(self, file_id: str):
        return self._files.get(file_id, (None, None))

    def refresh_if_changed(self) -> bool:
        return False  # a single process owns the file

    def mark(self, table: str, key: str) -> None:
        pass  # the JSON file is always written as a whole

    def describe(self) -> str:
        return f"json:{self.path}" if self.path else "memory"

    def _load(self) -> BankState:
        with open(self.path, "r", encoding="utf-8") as f:
            return BankState.model_validate_json(f.read())

    def save(self, dirty_deals=None) -> None:
        if not self.path:
            return
        with self.lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self.state.model_dump_json(indent=1))
            os.replace(tmp, self.path)

    def reset(self) -> None:
        with self.lock:
            self.state = seed_state()
            self.save()


# ---------------------------------------------------------------------------
# Seed data (fictional customers; reference data samples)
# ---------------------------------------------------------------------------

def seed_state(today: Optional[date] = None) -> BankState:
    from .. import settings

    today = today or date.today()
    s = BankState(business_date=today)
    rm_email = settings.bank_sender_email()

    customers = [
        Customer(
            customer_id="DE-CORP-99214", name="Hanse Solartechnik GmbH", address="Grosse Elbstrasse 145, 22767 Hamburg",
            country="DE", email=settings.client_email("DE-CORP-99214"), relationship_manager_email=rm_email, kyc_status="VERIFIED", kyc_next_review=today + timedelta(days=300),
            risk_rating="LOW", current_account_iban=make_iban("DE", "70020270" + "0015620099"),
            auto_waive_discrepancy_codes=["LATE_PRESENTATION", "BENEFICIARY_COUNTRY"],
        ),
        Customer(
            customer_id="DE-CORP-40551", name="Bayerische Maschinenbau AG", address="Leopoldstrasse 210, 80807 Muenchen",
            country="DE", email=settings.client_email("DE-CORP-40551"), relationship_manager_email=rm_email, kyc_status="VERIFIED", kyc_next_review=today + timedelta(days=200),
            risk_rating="MEDIUM", current_account_iban=make_iban("DE", "70020270" + "0033871244"),
        ),
        Customer(
            customer_id="DE-CORP-77310", name="Rhein Bau GmbH & Co. KG", address="Deutzer Freiheit 30, 50679 Koeln",
            country="DE", email=settings.client_email("DE-CORP-77310"), relationship_manager_email=rm_email, kyc_status="VERIFIED", kyc_next_review=today + timedelta(days=150),
            risk_rating="MEDIUM", current_account_iban=make_iban("DE", "70020270" + "0048120555"),
        ),
    ]
    for c in customers:
        s.customers[c.customer_id] = c

    def fac(fid, cid, product, limit, transfer=None):
        s.facilities[fid] = Facility(facility_id=fid, customer_id=cid, product=product, limit=Decimal(limit),
                                     expiry=today + timedelta(days=365), can_transfer_to=transfer or [])

    fac("FAC-99214-LC", "DE-CORP-99214", "LC", "400000")
    fac("FAC-99214-GTEE", "DE-CORP-99214", "GUARANTEE", "250000")
    fac("FAC-99214-WC", "DE-CORP-99214", "WORKING_CAPITAL", "1000000", ["LC", "GUARANTEE", "SURETY"])
    fac("FAC-40551-LC", "DE-CORP-40551", "LC", "2000000")
    fac("FAC-40551-GTEE", "DE-CORP-40551", "GUARANTEE", "1500000")
    fac("FAC-77310-SURETY", "DE-CORP-77310", "SURETY", "800000")
    fac("FAC-77310-GTEE", "DE-CORP-77310", "GUARANTEE", "300000")
    fac("FAC-77310-WC", "DE-CORP-77310", "WORKING_CAPITAL", "250000", ["SURETY", "GUARANTEE"])

    # counterparty limits on correspondent banks (for confirming their export credits)
    for bic8, limit in [("NCBKSAJE", "5000000"), ("BKCHCNBJ", "10000000"), ("CHASUS33", "20000000"),
                        ("BNCAITMM", "15000000"), ("EBILAEAD", "4000000"), ("ICICINBB", "3000000")]:
        s.facilities[f"FAC-BANK-{bic8}"] = Facility(facility_id=f"FAC-BANK-{bic8}", customer_id=f"BANK:{bic8}",
                                                   product="BANK_CONFIRMATION", limit=Decimal(limit),
                                                   expiry=today + timedelta(days=365))

    def acc(aid, owner, ccy="EUR", balance="0", overdraft="0", on_bs=True):
        s.accounts[aid] = Account(account_id=aid, owner=owner, currency=ccy, balance=Decimal(balance),
                                  overdraft_limit=Decimal(overdraft), on_balance_sheet=on_bs)

    # customer accounts (opening balances offset against equity)
    opening = {"DE-CORP-99214": "650000", "DE-CORP-40551": "1200000", "DE-CORP-77310": "180000"}
    for c in customers:
        acc(c.current_account_iban, c.customer_id, balance=opening[c.customer_id], overdraft="100000")
    for gl in ["GL_INTEREST_INCOME", "GL_CASH_COLLATERAL", "GL_IMPORT_LOANS", "GL_ACCEPTANCES_PAYABLE",
               "GL_GUARANTEE_PAYMENTS_RECEIVABLE", "GL_FX_POSITION_EUR"]:
        acc(gl, "BANK")
    # Sign convention for every account: balance = credits - debits.
    # Customer deposits and income show positive balances; assets (nostro, loans) show negative balances.
    for ccy in ["EUR", "USD", "GBP", "CHF", "CNY", "JPY"]:
        acc(f"GL_NOSTRO_{ccy}", "BANK", ccy=ccy, balance="-50000000")
        acc(f"GL_EQUITY_{ccy}", "BANK", ccy=ccy, balance="50000000")
        acc(f"GL_FEE_INCOME_{ccy}", "BANK", ccy=ccy)
        if ccy != "EUR":
            acc(f"GL_FX_POSITION_{ccy}", "BANK", ccy=ccy)
    s.accounts["GL_NOSTRO_EUR"].balance -= sum(Decimal(v) for v in opening.values())
    for prod in ["LC", "GTEE", "SURETY"]:
        acc(f"OB_{prod}_CUSTOMER_LIABILITY", "BANK", on_bs=False)
        acc(f"OB_{prod}_BANK_COMMITMENT", "BANK", on_bs=False)

    ref = s.reference
    ref.fx_eur_per_unit = {"EUR": Decimal("1"), "USD": Decimal("0.9200"), "GBP": Decimal("1.1700"),
                           "CHF": Decimal("1.0500"), "CNY": Decimal("0.1280"), "JPY": Decimal("0.0061")}
    ref.fx_rates_as_of = today
    ref.nostro_by_currency = {c: f"GL_NOSTRO_{c}" for c in ref.fx_eur_per_unit}
    ref.correspondent_bic_by_currency = {"EUR": "DEUTDEFFXXX", "USD": "CHASUS33XXX", "GBP": "BARCGB22XXX",
                                         "CHF": "UBSWCHZH80A", "CNY": "BKCHCNBJXXX", "JPY": "BOTKJPJTXXX"}
    ref.sanctions_entities = [
        SanctionsEntry(name="Volkov Maritime Trading LLC", aliases=["Volkov Maritime"], country="RU",
                       list_name="EU 833/2014 Annex (sample)", entry_id="EU-SAMPLE-001"),
        SanctionsEntry(name="Pyongyang Heavy Industries Corporation", aliases=["PHI Corp"], country="KP",
                       list_name="UN 1718 (sample)", entry_id="UN-SAMPLE-002"),
        SanctionsEntry(name="Tehran Precision Components Company", aliases=["TPC Co"], country="IR",
                       list_name="OFAC SDN (sample)", entry_id="OFAC-SAMPLE-003"),
        SanctionsEntry(name="Black Sea Shadow Logistics FZE", aliases=[], country="AE",
                       list_name="OFAC SDN (sample)", entry_id="OFAC-SAMPLE-004"),
    ]
    ref.embargoed_countries = {"KP": "Comprehensive UN/EU embargo", "IR": "EU/US restrictive measures",
                               "SY": "EU restrictive measures", "CU": "US embargo (OFAC)", "BY": "EU restrictive measures"}
    ref.embargoed_regions = ["crimea", "sevastopol", "donetsk", "luhansk"]
    ref.dual_use_keywords = ["centrifuge", "uranium", "maraging steel", "night vision", "missile", "explosive",
                             "rocket", "drone engine", "5-axis machine tool", "encryption module"]
    ref.blocked_vessels = ["IMO9999999", "IMO9123456"]
    return s


def deal_from_dict(data: Dict[str, Any]):
    return _DEAL_ADAPTER.validate_python(data)


def to_jsonable(obj: Any) -> Any:
    """Converts pydantic models / Decimals / dates into JSON-safe values."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def dump_state_summary(state: BankState) -> Dict[str, Any]:
    return {
        "business_date": state.business_date.isoformat(),
        "customers": len(state.customers),
        "deals": len(state.deals),
        "open_exceptions": sum(1 for e in state.exceptions.values() if e.status == "OPEN"),
        "swift_messages": len(state.swift_messages),
        "ledger_entries": len(state.ledger),
        "ledger_balanced": state.ledger_balanced(),
    }


__all__ = ["BankState", "StateStore", "seed_state", "make_iban", "to_jsonable", "deal_from_dict", "json"]
