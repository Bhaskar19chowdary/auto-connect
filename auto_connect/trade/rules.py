"""
Rule engines used by the trade finance workflows.

* Application checks (data quality, ISBP 821 good practice, non-documentary conditions)
* ICC UCP 600 document examination (Art. 6, 14, 16, 17, 18, 20, 27, 28, 30)
* ICC URDG 758 demand examination (Art. 14, 15, 20, 23, 24, 25)
* Surety bond underwriting and claim examination (§§ 765, 768, 771 BGB, § 551 BGB)
* Tariff / fee calculation

Every check returns structured results with the rule reference so that each
automatic decision is explainable and auditable.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from .models import (
    Demand,
    Discrepancy,
    GuaranteeTerms,
    LCTerms,
    LetterOfCredit,
    Presentation,
    PresentedDocument,
    SuretyClaim,
    SuretyTerms,
)
from .swift import is_valid_bic

CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def is_valid_iban(iban: Optional[str]) -> bool:
    """ISO 13616 mod-97 check."""
    if not iban:
        return False
    s = iban.replace(" ", "").upper()
    if not re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$", s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def normalise(text: Optional[str]) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    text = f" {text} "
    for word in (" gmbh ", " s p a ", " spa ", " ltd ", " limited ", " inc ", " ag ", " srl ", " s r l ", " co ", " kg ",
                 " sa ", " llc ", " fze ", " plc ", " bv ", " nv "):
        text = text.replace(word, " ")
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    a_n, b_n = normalise(a), normalise(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    sorted_a = " ".join(sorted(a_n.split()))
    sorted_b = " ".join(sorted(b_n.split()))
    return max(SequenceMatcher(None, a_n, b_n).ratio(), SequenceMatcher(None, sorted_a, sorted_b).ratio())


_LEGAL_FORMS = [  # (pattern at the end of a company name, canonical form) - longest first
    (r"gmbh\s*&\s*co\.?\s*kg", "GMBH_CO_KG"), (r"s\.?\s*p\.?\s*a\.?", "SPA"), (r"s\.?\s*r\.?\s*l\.?", "SRL"),
    (r"s\.?\s*a\.?\s*s\.?", "SAS"), (r"s\.?\s*a\.?\s*r\.?\s*l\.?", "SARL"), (r"gmbh", "GMBH"), (r"ag", "AG"), (r"kgaa", "KGAA"),
    (r"kg", "KG"), (r"ohg", "OHG"), (r"se", "SE"), (r"ug", "UG"), (r"plc", "PLC"), (r"llc", "LLC"), (r"l\.?l\.?p\.?", "LLP"),
    (r"inc\.?|incorporated", "INC"), (r"corp\.?|corporation", "CORP"), (r"co\.?,?\s*ltd\.?|ltd\.?|limited", "LTD"),
    (r"pte\.?\s*ltd\.?", "PTE_LTD"), (r"s\.?\s*a\.?", "SA"), (r"b\.?\s*v\.?", "BV"), (r"n\.?\s*v\.?", "NV"),
    (r"a\.?\s*s\.?", "AS"), (r"oy", "OY"), (r"ab", "AB"), (r"sp\.?\s*z\s*o\.?\s*o\.?", "SPZOO"), (r"fze", "FZE"),
    (r"fzco", "FZCO"), (r"jsc|ojsc|pjsc", "JSC"),
]


def legal_form(name: Optional[str]) -> Optional[str]:
    """Canonical legal form at the end of a company name ('Milano Meccanica S.p.A.' -> 'SPA'), None if none stated."""
    text = (name or "").strip().lower().rstrip(".,; ")
    for pattern, form in _LEGAL_FORMS:
        if re.search(r"(?:^|[\s,])(?:" + pattern + r")$", text):
            return form
    return None


def different_legal_form(a: Optional[str], b: Optional[str]) -> bool:
    fa, fb = legal_form(a), legal_form(b)
    return bool(fa and fb and fa != fb)


def name_on_document(document_text: Optional[str], expected: str) -> str:
    """The party name as written on a document, without the address that often follows it after a comma
    ('Coromandel Rice Exports Pvt. Ltd., 12 Harbour Road, Chennai' -> 'Coromandel Rice Exports Pvt. Ltd.').
    UCP 600 Art. 14(j): addresses need not be the same as in the credit, so only the name is compared."""
    parts = [x.strip() for x in re.split(r"[,\n;]", document_text or "") if x.strip()]
    if len(parts) <= 1:
        return (document_text or "").strip()
    candidates = [", ".join(parts[:i]) for i in range(1, len(parts) + 1)]
    return max(candidates, key=lambda c: (similarity(c, expected), -len(c)))


def corresponds(document_text: str, credit_text: str) -> bool:
    """UCP 600 Art. 18(c): invoice goods description must correspond with the credit.
    Implemented as: every significant word of the credit description appears in the document."""
    stop = {"and", "the", "of", "for", "with", "as", "per", "in", "to", "a", "an", "incoterms", "2020"}
    credit_words = {w for w in normalise(credit_text).split() if w not in stop and len(w) > 1}
    doc_words = set(normalise(document_text).split())
    if not credit_words:
        return True
    return len(credit_words - doc_words) == 0


def same_place(a: Optional[str], b: Optional[str]) -> bool:
    return similarity(a or "", b or "") >= 0.85 or normalise(a) in normalise(b) or normalise(b) in normalise(a)


def _d(code: str, rule: str, description: str) -> Discrepancy:
    return Discrepancy(code=code, rule=rule, description=description)


NON_DOCUMENTARY_HINTS = (
    "must be shipped by", "goods must be", "applicant must", "subject to", "upon receipt of import licence",
    "upon arrival", "after inspection by applicant", "at the discretion",
)

INSURANCE_INCOTERMS = {"CIF", "CIP"}


# ---------------------------------------------------------------------------
# LC application checks (before issuance)
# ---------------------------------------------------------------------------

def validate_lc_application(t: LCTerms, today: date) -> Tuple[List[Discrepancy], List[str]]:
    """Returns (blocking_errors, warnings)."""
    errors: List[Discrepancy] = []
    warnings: List[str] = []

    if t.amount <= 0:
        errors.append(_d("AMOUNT_INVALID", "Data", "Credit amount must be positive."))
    if not is_valid_bic(t.advising_bank_bic):
        errors.append(_d("ADVISING_BIC_INVALID", "SWIFT", f"Advising bank BIC '{t.advising_bank_bic}' is not a valid BIC."))
    if t.expiry_date <= today:
        errors.append(_d("EXPIRY_IN_PAST", "UCP 600 Art. 6(d)(i)", "Credit must state an expiry date in the future."))
    if t.latest_shipment_date > t.expiry_date:
        errors.append(_d("SHIPMENT_AFTER_EXPIRY", "UCP 600 Art. 6(d)", "Latest shipment date is after the expiry date."))
    if not t.documents_required:
        errors.append(_d("NO_DOCUMENTS", "UCP 600 Art. 5", "A documentary credit must call for at least one document."))
    if t.presentation_period_days > 21:
        warnings.append("Presentation period exceeds the UCP 600 Art. 14(c) default of 21 days.")
    if t.available_by in ("DEFERRED_PAYMENT", "ACCEPTANCE") and t.usance_days <= 0:
        errors.append(_d("USANCE_MISSING", "UCP 600 Art. 7(a)", "Deferred payment/acceptance credit needs a usance period."))
    if t.tolerance_plus_pct > 10 or t.tolerance_minus_pct > 10:
        warnings.append("Tolerance above 10% is unusual (UCP 600 Art. 30(a)).")
    doc_types = {d.doc_type for d in t.documents_required}
    if t.incoterms.upper() in INSURANCE_INCOTERMS and "INSURANCE_DOCUMENT" not in doc_types:
        errors.append(_d("INSURANCE_NOT_CALLED_FOR", "Incoterms 2020 / UCP 600 Art. 28",
                         f"Incoterm {t.incoterms} requires the seller to insure - the credit must call for an insurance document."))
    if t.incoterms.upper() not in INSURANCE_INCOTERMS and "INSURANCE_DOCUMENT" in doc_types:
        warnings.append(f"Insurance document called for although Incoterm {t.incoterms} puts insurance on the buyer.")
    if ("BILL_OF_LADING" not in doc_types) and ("AIR_WAYBILL" not in doc_types) and t.incoterms.upper() != "EXW":
        errors.append(_d("TRANSPORT_DOCUMENT_MISSING", "UCP 600 Art. 19-25 / ISBP 821",
                         f"Goods are shipped from {t.port_of_loading} to {t.port_of_discharge} but the credit calls for no "
                         f"transport document (bill of lading or air waybill) - there would be no evidence of shipment."))
    for cond in t.additional_conditions:
        if any(h in cond.lower() for h in NON_DOCUMENTARY_HINTS):
            errors.append(_d("NON_DOCUMENTARY_CONDITION", "UCP 600 Art. 14(h) / ISBP 821 A26",
                             f"Condition without a stipulated document: '{cond}'. Rephrase so it is evidenced by a document."))
    if t.beneficiary.iban and not is_valid_iban(t.beneficiary.iban):
        errors.append(_d("BENEFICIARY_IBAN_INVALID", "ISO 13616", "Beneficiary IBAN fails the mod-97 check."))
    return errors, warnings


# ---------------------------------------------------------------------------
# UCP 600 document examination
# ---------------------------------------------------------------------------

def _docs(presentation: Presentation, doc_type: str) -> List[PresentedDocument]:
    return [d for d in presentation.documents if d.doc_type == doc_type]


def credit_ceiling(t: LCTerms) -> Decimal:
    return money(t.amount * (1 + t.tolerance_plus_pct / 100))


def shipment_date(presentation: Presentation) -> Optional[date]:
    dates: List[date] = []
    for d in _docs(presentation, "BILL_OF_LADING") + _docs(presentation, "AIR_WAYBILL"):
        raw = d.fields.get("on_board_date") or d.fields.get("flight_date") or d.issue_date
        if raw:
            dates.append(date.fromisoformat(str(raw)) if not isinstance(raw, date) else raw)
    return max(dates) if dates else None


def examine_ucp600(lc: LetterOfCredit, presentation: Presentation, applicant_name: str,
                   already_drawn: Decimal = Decimal("0")) -> List[Discrepancy]:
    t = lc.terms
    out: List[Discrepancy] = []
    pres_date = presentation.presentation_date

    # Art. 6(d)(i) / 29 - presentation on or before expiry
    if pres_date > t.expiry_date:
        out.append(_d("LATE_PRESENTATION_EXPIRY", "UCP 600 Art. 6(d)(i)",
                      f"Documents presented {pres_date} after expiry of the credit {t.expiry_date}."))

    # Art. 17 / 14(a) - required documents & originals
    for req in t.documents_required:
        found = _docs(presentation, req.doc_type)
        if not found:
            out.append(_d(f"MISSING_{req.doc_type}", "UCP 600 Art. 14(a) / 17",
                          f"{req.doc_type.replace('_', ' ').title()} required by the credit was not presented."))
            continue
        originals = sum(d.originals for d in found)
        if originals < req.originals:
            out.append(_d(f"ORIGINALS_{req.doc_type}", "UCP 600 Art. 17(a)",
                          f"{req.doc_type.replace('_', ' ').title()}: {originals} original(s) presented, {req.originals} required."))

    # Art. 14(i) - documents not dated later than presentation
    for d in presentation.documents:
        if d.issue_date and d.issue_date > pres_date:
            out.append(_d("DOC_DATED_AFTER_PRESENTATION", "UCP 600 Art. 14(i)",
                          f"{d.doc_type} dated {d.issue_date} is later than the presentation date."))

    ship = shipment_date(presentation)
    # Art. 14(c) - presentation within N days after shipment
    if ship:
        if ship > t.latest_shipment_date:
            out.append(_d("LATE_SHIPMENT", "UCP 600 Art. 20(a)(ii) / 44C",
                          f"Shipment on {ship} is later than the latest shipment date {t.latest_shipment_date}."))
        if pres_date > ship + timedelta(days=t.presentation_period_days):
            out.append(_d("LATE_PRESENTATION", "UCP 600 Art. 14(c)",
                          f"Presented more than {t.presentation_period_days} days after shipment ({ship})."))

    # Art. 18 - commercial invoice
    invoice_amount: Optional[Decimal] = None
    for inv in _docs(presentation, "COMMERCIAL_INVOICE"):
        f = dict(inv.fields)
        if f.get("beneficiary_name"):
            f["beneficiary_name"] = name_on_document(f["beneficiary_name"], t.beneficiary.name)
        if f.get("applicant_name"):
            f["applicant_name"] = name_on_document(f["applicant_name"], applicant_name)
        if f.get("beneficiary_name") and similarity(f["beneficiary_name"], t.beneficiary.name) < 0.85:
            out.append(_d("INVOICE_NOT_ISSUED_BY_BENEFICIARY", "UCP 600 Art. 18(a)(i)",
                          f"Invoice issued by '{f['beneficiary_name']}', not by the beneficiary '{t.beneficiary.name}'."))
        elif f.get("beneficiary_name") and different_legal_form(f["beneficiary_name"], t.beneficiary.name):
            out.append(_d("INVOICE_ISSUER_LEGAL_FORM", "UCP 600 Art. 18(a)(i) / 14(d)",
                          f"Invoice issued by '{f['beneficiary_name']}' while the credit names '{t.beneficiary.name}': a "
                          f"different legal form indicates a different company."))
        if f.get("applicant_name") and similarity(f["applicant_name"], applicant_name) < 0.85:
            out.append(_d("INVOICE_NOT_TO_APPLICANT", "UCP 600 Art. 18(a)(ii)",
                          f"Invoice made out to '{f['applicant_name']}', not to the applicant '{applicant_name}'."))
        elif f.get("applicant_name") and different_legal_form(f["applicant_name"], applicant_name):
            out.append(_d("INVOICE_APPLICANT_LEGAL_FORM", "UCP 600 Art. 18(a)(ii) / 14(d)",
                          f"Invoice made out to '{f['applicant_name']}' while the applicant is '{applicant_name}': a "
                          f"different legal form indicates a different company."))
        if f.get("currency") and f["currency"] != t.currency:
            out.append(_d("INVOICE_CURRENCY", "UCP 600 Art. 18(a)(iii)",
                          f"Invoice currency {f['currency']} differs from credit currency {t.currency}."))
        if f.get("amount") is not None:
            invoice_amount = money(f["amount"])
            if invoice_amount > credit_ceiling(t):
                out.append(_d("INVOICE_EXCEEDS_CREDIT", "UCP 600 Art. 18(b) / 30",
                              f"Invoice amount {invoice_amount} exceeds the credit amount incl. tolerance {credit_ceiling(t)}."))
        if f.get("goods_description") and not corresponds(f["goods_description"], t.goods_description):
            out.append(_d("GOODS_DESCRIPTION", "UCP 600 Art. 18(c)",
                          f"Invoice goods description '{f['goods_description']}' does not correspond with the credit "
                          f"'{t.goods_description}'."))
        # Art. 14(j): addresses need not match, only the country must be the one stated in the credit
        if f.get("beneficiary_country") and t.beneficiary.country \
                and f["beneficiary_country"].upper() != t.beneficiary.country.upper():
            out.append(_d("BENEFICIARY_COUNTRY", "UCP 600 Art. 14(j)",
                          "Beneficiary address on the invoice is not in the country stated in the credit."))

    # Drawing amount
    available = credit_ceiling(t) - already_drawn
    if presentation.claimed_amount > available:
        out.append(_d("DRAWING_EXCEEDS_AVAILABLE", "UCP 600 Art. 30 / 18(b)",
                      f"Amount claimed {presentation.claimed_amount} exceeds available balance {available}."))
    if invoice_amount is not None and presentation.claimed_amount > invoice_amount:
        out.append(_d("CLAIM_EXCEEDS_INVOICE", "ISBP 821 C6",
                      f"Amount claimed {presentation.claimed_amount} exceeds the invoice amount {invoice_amount}."))
    if not t.partial_shipments_allowed and invoice_amount is not None:
        minimum = money(t.amount * (1 - t.tolerance_minus_pct / 100))
        lower_bound = minimum if t.tolerance_minus_pct else money(t.amount * Decimal("0.95"))
        if invoice_amount < lower_bound:
            out.append(_d("PARTIAL_SHIPMENT", "UCP 600 Art. 30(c) / 31",
                          f"Partial drawing {invoice_amount} although partial shipments are not allowed."))

    # Art. 20 / 27 - bill of lading
    for bl in _docs(presentation, "BILL_OF_LADING"):
        f = bl.fields
        if f.get("port_of_loading") and not same_place(f["port_of_loading"], t.port_of_loading):
            out.append(_d("BL_PORT_OF_LOADING", "UCP 600 Art. 20(a)(iii)",
                          f"B/L port of loading '{f['port_of_loading']}' differs from credit '{t.port_of_loading}'."))
        if f.get("port_of_discharge") and not same_place(f["port_of_discharge"], t.port_of_discharge):
            out.append(_d("BL_PORT_OF_DISCHARGE", "UCP 600 Art. 20(a)(iii)",
                          f"B/L port of discharge '{f['port_of_discharge']}' differs from credit '{t.port_of_discharge}'."))
        if f.get("clean") is False:
            out.append(_d("BL_NOT_CLEAN", "UCP 600 Art. 27",
                          "Transport document bears a clause declaring a defective condition of the goods or packaging."))
        if f.get("signed_by") and str(f["signed_by"]).upper() not in {"CARRIER", "MASTER", "AGENT_FOR_CARRIER", "AGENT_FOR_MASTER"}:
            out.append(_d("BL_SIGNATURE", "UCP 600 Art. 20(a)(i)",
                          "Bill of lading is not signed by the carrier, master or a named agent."))
        issued = f.get("originals_issued")
        if issued and bl.originals < int(issued):
            out.append(_d("BL_FULL_SET", "UCP 600 Art. 20(a)(iv)",
                          f"Full set not presented: {bl.originals} of {issued} originals."))
        if f.get("transhipment") and not t.transhipment_allowed and not f.get("containerised"):
            out.append(_d("TRANSHIPMENT", "UCP 600 Art. 20(c)",
                          "Transhipment effected although prohibited and goods are not shipped in containers."))

    # Art. 28 - insurance
    insurance_docs = _docs(presentation, "INSURANCE_DOCUMENT")
    for ins in insurance_docs:
        f = ins.fields
        base = invoice_amount if invoice_amount is not None else presentation.claimed_amount
        required = money(base * Decimal("1.10"))
        if f.get("coverage_amount") is not None and money(f["coverage_amount"]) < required:
            out.append(_d("INSURANCE_UNDERINSURED", "UCP 600 Art. 28(f)(ii)",
                          f"Insurance cover {money(f['coverage_amount'])} is below 110% of the invoice value ({required})."))
        if f.get("currency") and f["currency"] != t.currency:
            out.append(_d("INSURANCE_CURRENCY", "UCP 600 Art. 28(f)(i)", "Insurance is not in the currency of the credit."))
        if ins.issue_date and ship and ins.issue_date > ship and not f.get("cover_effective_from_shipment"):
            out.append(_d("INSURANCE_DATED_AFTER_SHIPMENT", "UCP 600 Art. 28(e)",
                          f"Insurance dated {ins.issue_date}, later than the shipment date {ship}."))

    # Certificate of origin
    if t.country_of_origin:
        for co in _docs(presentation, "CERTIFICATE_OF_ORIGIN"):
            origin = co.fields.get("country_of_origin")
            if origin and origin.upper() != t.country_of_origin.upper():
                out.append(_d("ORIGIN_MISMATCH", "UCP 600 Art. 14(d) / 14(f)",
                              f"Certificate shows origin {origin}, credit requires {t.country_of_origin}."))

    # Art. 14(d)/(e) - other documents may describe goods in general terms but must not conflict
    credit_words = set(normalise(t.goods_description).split())
    for d in presentation.documents:
        desc = d.fields.get("goods_description")
        if d.doc_type == "COMMERCIAL_INVOICE" or not desc:
            continue
        if not (set(normalise(desc).split()) & credit_words):
            out.append(_d("DATA_CONFLICT", "UCP 600 Art. 14(d)/(e)",
                          f"{d.doc_type.replace('_', ' ').title()} describes goods as '{desc}', which conflicts with the credit."))
            break

    # de-duplicate by code
    seen = set()
    unique = []
    for d in out:
        if d.code not in seen:
            unique.append(d)
            seen.add(d.code)
    return unique


# ---------------------------------------------------------------------------
# Guarantees (URDG 758)
# ---------------------------------------------------------------------------

def validate_guarantee_application(t: GuaranteeTerms, today: date) -> Tuple[List[Discrepancy], List[str]]:
    errors: List[Discrepancy] = []
    warnings: List[str] = []
    if t.amount <= 0:
        errors.append(_d("AMOUNT_INVALID", "Data", "Guarantee amount must be positive."))
    if t.expiry_date is None:
        errors.append(_d("OPEN_ENDED", "URDG 758 Art. 25 / credit policy",
                         "Guarantee has no expiry date - open-ended undertakings need credit approval."))
    elif t.expiry_date <= today:
        errors.append(_d("EXPIRY_IN_PAST", "URDG 758 Art. 25", "Expiry date must be in the future."))
    if t.beneficiary_bank_bic and not is_valid_bic(t.beneficiary_bank_bic):
        errors.append(_d("BENEFICIARY_BANK_BIC_INVALID", "SWIFT", "Beneficiary bank BIC is invalid."))
    if not t.underlying_transaction.strip():
        errors.append(_d("UNDERLYING_MISSING", "URDG 758 Art. 8(b)", "The underlying relationship must be identified."))
    if t.counter_guarantee:
        if not t.beneficiary_bank_bic:
            errors.append(_d("COUNTER_BANK_MISSING", "URDG 758 Art. 2", "A counter-guarantee is issued to the bank that "
                             "issues the local guarantee: beneficiary_bank_bic is required."))
        if t.expiry_date and t.local_guarantee_expiry and t.expiry_date < t.local_guarantee_expiry + timedelta(days=15):
            errors.append(_d("COUNTER_EXPIRY_TOO_SHORT", "URDG 758 Art. 25 / market practice",
                             "The counter-guarantee must run at least 15 days beyond the local guarantee so the local bank "
                             "can claim after paying a demand received on the last day."))
    if t.underlying_contract_value:
        pct = t.amount / t.underlying_contract_value * 100
        caps = {"PERFORMANCE": 15, "WARRANTY": 10, "BID": 5, "RETENTION": 10, "ADVANCE_PAYMENT": 100}
        cap = caps.get(t.guarantee_type)
        if cap and pct > cap:
            warnings.append(f"{t.guarantee_type} guarantee is {pct:.1f}% of contract value (market practice <= {cap}%).")
    return errors, warnings


def guarantee_wording(g, applicant_name: str, bank_name: str) -> str:
    t = g.terms
    expiry = t.expiry_date.isoformat() if t.expiry_date else "no fixed date"
    statement = "" if t.exclude_statement_of_breach else (
        " Any demand must be supported by a statement indicating in what respect the applicant is in breach "
        "of its obligations under the underlying relationship (URDG 758 Art. 15(a))."
    )
    extra = [d.replace("_", " ").lower() for d in t.required_demand_documents if d != "DEMAND"]
    docs = " accompanied by: " + ", ".join(extra) if extra else ""
    if t.counter_guarantee:
        local_exp = t.local_guarantee_expiry.isoformat() if t.local_guarantee_expiry else expiry
        return (
            f"Counter-Guarantee No. {g.ref}. At the request of {applicant_name} we, {bank_name}, request you to issue "
            f"under your own responsibility your {t.guarantee_type.replace('_', ' ').lower()} guarantee for "
            f"{t.currency} {money(t.amount):,} in favour of {t.beneficiary.name} in respect of {t.underlying_transaction}, "
            f"expiring on {local_exp}. In consideration we irrevocably undertake to pay you any sum or sums not exceeding "
            f"in total {t.currency} {money(t.amount):,} upon receipt of your complying demand supported by a statement "
            f"indicating that you have received a complying demand under your guarantee (URDG 758 Art. 15(b)). "
            f"This counter-guarantee expires on {expiry}. It is subject to the {t.rules}. Governing law: {t.governing_law}."
        )
    return (
        f"{t.guarantee_type.replace('_', ' ').title()} Guarantee No. {g.ref}. "
        f"We, {bank_name}, have been informed that {applicant_name} (the applicant) has entered into "
        f"{t.underlying_transaction} with you, {t.beneficiary.name}. At the request of the applicant we hereby "
        f"irrevocably undertake to pay you any sum or sums not exceeding in total {t.currency} {money(t.amount):,} "
        f"upon receipt of your complying demand{docs}.{statement} "
        f"This guarantee expires on {expiry}; any demand must be received by us on or before that date. "
        f"{(t.reduction_clause + ' ') if t.reduction_clause else ''}"
        f"This guarantee is subject to the {t.rules}. Governing law: {t.governing_law}."
    )


def examine_urdg_demand(g, demand: Demand) -> List[Discrepancy]:
    t = g.terms
    out: List[Discrepancy] = []
    if t.expiry_date and demand.presentation_date > t.expiry_date:
        out.append(_d("DEMAND_AFTER_EXPIRY", "URDG 758 Art. 25(a)",
                      f"Demand presented on {demand.presentation_date}, after expiry {t.expiry_date}."))
    if demand.currency != t.currency:
        out.append(_d("DEMAND_CURRENCY", "URDG 758 Art. 14(a)", "Demand currency differs from the guarantee currency."))
    if demand.amount > g.outstanding:
        out.append(_d("DEMAND_EXCEEDS_AMOUNT", "URDG 758 Art. 17",
                      f"Demand {demand.amount} exceeds the amount available {g.outstanding}."))
    if demand.amount <= 0:
        out.append(_d("DEMAND_AMOUNT_INVALID", "URDG 758 Art. 15", "Demand amount must be positive."))
    types = {d.doc_type for d in demand.documents}
    if "DEMAND" not in types:
        out.append(_d("NO_SIGNED_DEMAND", "URDG 758 Art. 15(a)", "No signed demand presented."))
    if t.counter_guarantee:  # URDG 758 Art. 15(b): statement that the guarantor received a complying demand
        has_stmt = "GUARANTOR_STATEMENT" in types or any(
            d.doc_type == "DEMAND" and d.fields.get("complying_demand_received") for d in demand.documents)
        if not has_stmt:
            out.append(_d("NO_GUARANTOR_STATEMENT", "URDG 758 Art. 15(b)",
                          "Demand under the counter-guarantee is not supported by a statement that the guarantor has "
                          "received a complying demand under its guarantee."))
    elif not t.exclude_statement_of_breach and "STATEMENT_OF_BREACH" not in types:
        stmt_in_demand = any(d.doc_type == "DEMAND" and d.fields.get("breach_statement") for d in demand.documents)
        if not stmt_in_demand:
            out.append(_d("NO_STATEMENT_OF_BREACH", "URDG 758 Art. 15(a)",
                          "Demand is not supported by a statement indicating in what respect the applicant is in breach."))
    for req in t.required_demand_documents:
        if req not in types:
            out.append(_d(f"MISSING_{req}", "URDG 758 Art. 15 / 19", f"Required document {req} not presented."))
    for d in demand.documents:
        if d.doc_type == "DEMAND" and d.fields.get("guarantee_ref") and d.fields["guarantee_ref"] != g.ref:
            out.append(_d("WRONG_GUARANTEE_REF", "URDG 758 Art. 14(c)", "Demand does not identify this guarantee."))
    return out


# ---------------------------------------------------------------------------
# Surety bonds (Bürgschaften)
# ---------------------------------------------------------------------------

def validate_surety_application(t: SuretyTerms, today: date) -> Tuple[List[Discrepancy], List[str]]:
    errors: List[Discrepancy] = []
    warnings: List[str] = []
    if t.amount <= 0:
        errors.append(_d("AMOUNT_INVALID", "Data", "Bond amount must be positive."))
    if t.expiry_date and t.expiry_date <= today:
        errors.append(_d("EXPIRY_IN_PAST", "Data", "Expiry date must be in the future."))
    if t.bond_type in ("CONTRACT_PERFORMANCE", "WARRANTY") and t.contract_value:
        pct = t.amount / t.contract_value * 100
        cap = 10 if t.bond_type == "CONTRACT_PERFORMANCE" else 5
        if pct > cap:
            errors.append(_d("BOND_PERCENTAGE", "Credit policy / VOB/B § 9c",
                             f"{t.bond_type} bond is {pct:.1f}% of the contract value; policy maximum is {cap}%."))
    if t.bond_type == "ADVANCE_PAYMENT":
        if not t.advance_payment_amount:
            errors.append(_d("ADVANCE_MISSING", "Data", "Advance payment bond needs the advance payment amount."))
        elif t.amount > t.advance_payment_amount:
            errors.append(_d("BOND_EXCEEDS_ADVANCE", "Credit policy", "Bond exceeds the advance payment it secures."))
    if t.bond_type == "RENT_DEPOSIT":
        if not t.monthly_rent:
            errors.append(_d("RENT_MISSING", "Data", "Rent deposit bond needs the monthly net rent."))
        elif t.amount > t.monthly_rent * 3:
            errors.append(_d("RENT_DEPOSIT_CAP", "§ 551 Abs. 1 BGB",
                             "Rent deposit for residential lease may not exceed three months' net rent."))
    if t.on_first_demand:
        warnings.append("Surety on first demand ('auf erstes Anfordern'): payable without proof of the secured claim.")
    if t.expiry_date is None:
        warnings.append("Bond without expiry: ends only on return of the deed or release by the obligee.")
    return errors, warnings


def surety_deed_text(bond, principal_name: str, bank_name: str) -> str:
    t = bond.terms
    kind = "auf erstes Anfordern" if t.on_first_demand else "selbstschuldnerisch (§ 773 Abs. 1 Nr. 1 BGB)"
    defence = (
        "We shall pay on first written demand stating that the secured claim exists."
        if t.on_first_demand
        else "We waive the defence of prior enforcement (Einrede der Vorausklage, § 771 BGB). "
             "The defences of the principal under § 768 BGB remain reserved."
    )
    return (
        f"Surety Bond ({t.bond_type.replace('_', ' ').title()}) No. {bond.ref} - Bürgschaft {kind}. "
        f"{bank_name} stands surety towards {t.obligee.name} for the obligations of {principal_name} "
        f"arising from {t.underlying_contract} up to a maximum amount of {t.currency} {money(t.amount):,}. {defence} "
        f"Validity: {t.expiry_date.isoformat() if t.expiry_date else 'until return of this deed'}. German law applies."
    )


def examine_surety_claim(bond, claim: SuretyClaim) -> List[Discrepancy]:
    t = bond.terms
    out: List[Discrepancy] = []
    if t.expiry_date and claim.presentation_date > t.expiry_date:
        out.append(_d("CLAIM_AFTER_EXPIRY", "Bond terms", f"Claim received {claim.presentation_date} after expiry {t.expiry_date}."))
    if claim.currency != t.currency:
        out.append(_d("CLAIM_CURRENCY", "Bond terms", "Claim currency differs from bond currency."))
    if claim.amount > bond.outstanding:
        out.append(_d("CLAIM_EXCEEDS_BOND", "§ 767 BGB / bond terms",
                      f"Claim {claim.amount} exceeds the outstanding bond amount {bond.outstanding}."))
    types = {d.doc_type for d in claim.documents}
    if "DEMAND" not in types:
        out.append(_d("NO_WRITTEN_DEMAND", "Bond terms", "No written claim signed by the obligee."))
    if not t.on_first_demand:
        if "NOTICE_OF_DEFAULT" not in types:
            out.append(_d("NO_NOTICE_OF_DEFAULT", "§ 765 BGB (accessory)",
                          "Accessory bond: obligee must show that the principal was notified of the default."))
        if "EVIDENCE_OF_CLAIM" not in types:
            out.append(_d("NO_EVIDENCE_OF_CLAIM", "§ 767 BGB (accessory)",
                          "Accessory bond: evidence of the secured claim and its amount is required."))
    return out


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------

DEFAULT_TARIFF: Dict[str, Decimal] = {
    "lc_issuance_rate_per_quarter_pct": Decimal("0.125"),
    "lc_issuance_min": Decimal("100"),
    "lc_payment_rate_pct": Decimal("0.15"),
    "lc_payment_min": Decimal("75"),
    "lc_acceptance_rate_pa_pct": Decimal("1.00"),
    "lc_amendment_flat": Decimal("50"),
    "discrepancy_fee": Decimal("90"),
    "swift_fee": Decimal("25"),
    "aval_rate_pa_pct_LOW": Decimal("0.80"),
    "aval_rate_pa_pct_MEDIUM": Decimal("1.20"),
    "aval_rate_pa_pct_HIGH": Decimal("2.00"),
    "aval_min_per_quarter": Decimal("50"),
    "guarantee_issuance_flat": Decimal("75"),
    "claim_handling_flat": Decimal("150"),
    "import_loan_margin_pa_pct": Decimal("2.50"),
    "export_advising_flat": Decimal("80"),
    "confirmation_rate_per_quarter_pct": Decimal("0.30"),
    "confirmation_min": Decimal("150"),
    "export_payment_rate_pct": Decimal("0.10"),
    "export_payment_min": Decimal("60"),
    "transfer_flat": Decimal("150"),
    "collection_rate_pct": Decimal("0.15"),
    "collection_min": Decimal("50"),
    "collection_max": Decimal("500"),
}


def quarters_started(start: date, end: date) -> int:
    if end <= start:
        return 1
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        months += 1
    return max(1, -(-months // 3))


def lc_issuance_commission(amount_eur: Decimal, issue: date, expiry: date, tariff=DEFAULT_TARIFF) -> Decimal:
    q = quarters_started(issue, expiry)
    fee = amount_eur * tariff["lc_issuance_rate_per_quarter_pct"] / 100 * q
    return money(max(fee, tariff["lc_issuance_min"]))


def lc_payment_commission(amount_eur: Decimal, tariff=DEFAULT_TARIFF) -> Decimal:
    return money(max(amount_eur * tariff["lc_payment_rate_pct"] / 100, tariff["lc_payment_min"]))


def aval_commission_for_quarter(amount_eur: Decimal, risk: str, tariff=DEFAULT_TARIFF) -> Decimal:
    rate = tariff[f"aval_rate_pa_pct_{risk}"]
    return money(max(amount_eur * rate / 100 / 4, tariff["aval_min_per_quarter"]))


def add_quarter(d: date) -> date:
    month = d.month + 3
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30,
                      31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)
