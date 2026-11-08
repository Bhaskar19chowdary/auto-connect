"""
SWIFT FIN MT message builders for trade finance (category 7) and payments
(MT103 / MT202 / MT756).

Messages are produced in FIN block format ({1:}{2:}{4:...-}) using the SWIFT
"X" character set, field line-length limits and the standard date/amount
formats, ready to hand to Alliance Access / Lite2 as a file or via MQ.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re
import textwrap
from typing import Iterable, List, Optional, Sequence, Tuple

_X_ALLOWED = re.compile(r"[^A-Za-z0-9/\-?:().,'+ \n]")
_BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")

Field = Tuple[str, str]


def is_valid_bic(bic: Optional[str]) -> bool:
    return bool(bic) and bool(_BIC_RE.match(bic.strip().upper()))


def x_charset(text: str) -> str:
    """Converts free text into the SWIFT X character set."""
    replacements = {"€": "EUR", "&": "+", "@": "(AT)", "_": "-", "%": " PCT", "#": "NO.", "\t": " ",
                    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", ";": ",",
                    '"': "'", "!": ".", "*": " ", "=": "-", "[": "(", "]": ")", "<": "(", ">": ")"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = _X_ALLOWED.sub(" ", text)
    return text


def swift_amount(amount: Decimal) -> str:
    """SWIFT amount format: comma as decimal separator, no thousands separator."""
    q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{q:.2f}".split(".")
    return f"{whole},{frac}"


def swift_date(d: date) -> str:
    return d.strftime("%y%m%d")


def lines(text: str, width: int = 65, max_lines: int = 100) -> str:
    out: List[str] = []
    for para in x_charset(text).split("\n"):
        wrapped = textwrap.wrap(para, width=width) or [""]
        out.extend(wrapped)
    return "\n".join(out[:max_lines])


def party_block(name: str, address: str = "", country: str = "") -> str:
    raw = [name] + [p.strip() for p in address.split(",") if p.strip()] + ([country] if country else [])
    out: List[str] = []
    for item in raw:
        out.extend(textwrap.wrap(x_charset(item), 35) or [""])
    return "\n".join(out[:4])


def _lt_address(bic: str) -> str:
    """12-char logical terminal: BIC8 + LT code + branch (XXX default)."""
    bic = bic.strip().upper()
    branch = bic[8:11] if len(bic) == 11 else "XXX"
    return f"{bic[:8]}A{branch}"


def _receiver_address(bic: str) -> str:
    bic = bic.strip().upper()
    branch = bic[8:11] if len(bic) == 11 else "XXX"
    return f"{bic[:8]}X{branch}"


def build_fin(mt: str, sender_bic: str, receiver_bic: str, fields: Sequence[Field]) -> str:
    if not is_valid_bic(sender_bic) or not is_valid_bic(receiver_bic):
        raise ValueError(f"Invalid BIC (sender={sender_bic}, receiver={receiver_bic})")
    body = "\n".join(f":{tag}:{value}" for tag, value in fields if value not in (None, ""))
    return (
        f"{{1:F01{_lt_address(sender_bic)}0000000000}}"
        f"{{2:I{mt}{_receiver_address(receiver_bic)}N}}"
        f"{{4:\n{body}\n-}}"
    )


def ref16(ref: str) -> str:
    """Field 20/21 references: max 16 chars, no leading/trailing slash or '//'."""
    r = x_charset(ref).replace(" ", "").replace("//", "/").strip("/")
    return r[:16]


# ---------------------------------------------------------------------------
# Documentary credits
# ---------------------------------------------------------------------------

_AVAILABLE_BY = {
    "SIGHT_PAYMENT": "BY PAYMENT",
    "DEFERRED_PAYMENT": "BY DEF PAYMENT",
    "ACCEPTANCE": "BY ACCEPTANCE",
    "NEGOTIATION": "BY NEGOTIATION",
}

_DOC_TEXT = {
    "COMMERCIAL_INVOICE": "SIGNED COMMERCIAL INVOICE",
    "BILL_OF_LADING": "FULL SET OF CLEAN ON BOARD OCEAN BILLS OF LADING MADE OUT TO ORDER, BLANK ENDORSED, MARKED FREIGHT PREPAID AND NOTIFY APPLICANT",
    "AIR_WAYBILL": "AIR WAYBILL CONSIGNED TO APPLICANT",
    "INSURANCE_DOCUMENT": "INSURANCE POLICY OR CERTIFICATE IN NEGOTIABLE FORM, BLANK ENDORSED, FOR 110 PCT OF CIF/CIP VALUE, COVERING INSTITUTE CARGO CLAUSES (A)",
    "PACKING_LIST": "PACKING LIST",
    "CERTIFICATE_OF_ORIGIN": "CERTIFICATE OF ORIGIN ISSUED BY CHAMBER OF COMMERCE",
    "INSPECTION_CERTIFICATE": "INSPECTION CERTIFICATE ISSUED BY AN INDEPENDENT SURVEYOR",
    "BENEFICIARY_STATEMENT": "BENEFICIARY'S SIGNED STATEMENT",
}


def mt700(lc, applicant, issue_date: date, sender_bic: str) -> str:
    t = lc.terms
    tol = ""
    if t.tolerance_plus_pct or t.tolerance_minus_pct:
        tol = f"{int(t.tolerance_plus_pct)}/{int(t.tolerance_minus_pct)}"
    docs = "\n".join(
        f"+{_DOC_TEXT.get(d.doc_type, d.doc_type)} IN {d.originals} ORIGINAL(S)"
        + (f" AND {d.copies} COPY(IES)" if d.copies else "")
        + (f" {d.note}" if d.note else "")
        for d in t.documents_required
    )
    drafts = ""
    if t.available_by in ("ACCEPTANCE", "NEGOTIATION"):
        drafts = "AT SIGHT" if not t.usance_days else f"{t.usance_days} DAYS AFTER B/L DATE"
    deferred = f"{t.usance_days} DAYS AFTER B/L DATE" if t.available_by == "DEFERRED_PAYMENT" else ""
    goods = t.goods_description + (f"\n{t.incoterms} {t.incoterms_place} (INCOTERMS 2020)" if t.incoterms else "")
    fields: List[Field] = [
        ("27", "1/1"),
        ("40A", "IRREVOCABLE"),
        ("20", ref16(lc.ref)),
        ("31C", swift_date(issue_date)),
        ("40E", "UCP LATEST VERSION"),
        ("31D", f"{swift_date(t.expiry_date)}{x_charset(t.expiry_place)[:29].upper()}"),
        ("50", party_block(applicant.name, applicant.address, applicant.country)),
        ("59", party_block(t.beneficiary.name, t.beneficiary.address, t.beneficiary.country)),
        ("32B", f"{t.currency}{swift_amount(t.amount)}"),
        ("39A", tol),
        ("41A", f"{t.advising_bank_bic}\n{_AVAILABLE_BY[t.available_by]}"),
        ("42C", drafts),
        ("42A", sender_bic if drafts else ""),
        ("42P", deferred),
        ("43P", "ALLOWED" if t.partial_shipments_allowed else "NOT ALLOWED"),
        ("43T", "ALLOWED" if t.transhipment_allowed else "NOT ALLOWED"),
        ("44E", lines(t.port_of_loading, 65, 1)),
        ("44F", lines(t.port_of_discharge, 65, 1)),
        ("44C", swift_date(t.latest_shipment_date)),
        ("45A", lines(goods)),
        ("46A", lines(docs)),
        ("47A", lines("\n".join(f"+{c}" for c in t.additional_conditions)) if t.additional_conditions else ""),
        ("71D", lines(t.charges, 35, 6)),
        ("48", f"{t.presentation_period_days}/DAYS AFTER THE DATE OF SHIPMENT"),
        ("49", t.confirmation),
        ("78", lines("UPON RECEIPT OF COMPLYING DOCUMENTS WE SHALL REMIT PROCEEDS AS PER PRESENTING BANK'S INSTRUCTIONS.")),
    ]
    return build_fin("700", sender_bic, t.advising_bank_bic, fields)


def mt707(lc, amendment_no: int, changes: dict, amend_date: date, sender_bic: str) -> str:
    t = lc.terms
    fields: List[Field] = [
        ("27", "1/1"),
        ("20", ref16(lc.ref)),
        ("21", "NONREF"),
        ("23", "ISSUANCE"),
        ("26E", str(amendment_no)),
        ("30", swift_date(amend_date)),
        ("22A", "ACNF"),
    ]
    delta = changes.get("amount_delta")
    if delta:
        d = Decimal(str(delta))
        fields.append(("32B" if d > 0 else "33B", f"{t.currency}{swift_amount(abs(d))}"))
    if changes.get("expiry_date"):
        fields.append(("31D", f"{swift_date(changes['expiry_date'])}{x_charset(t.expiry_place)[:29].upper()}"))
    if changes.get("latest_shipment_date"):
        fields.append(("44C", swift_date(changes["latest_shipment_date"])))
    narrative = changes.get("narrative")
    if narrative:
        fields.append(("47B", lines("/REPALL/" + narrative)))
    fields.append(("72Z", lines("ALL OTHER TERMS AND CONDITIONS REMAIN UNCHANGED.", 35, 6)))
    return build_fin("707", sender_bic, t.advising_bank_bic, fields)


def mt734(lc, presentation, discrepancies, refusal_date: date, sender_bic: str) -> str:
    text = "\n".join(f"{i}. {d.description} ({d.rule})" for i, d in enumerate(discrepancies, 1))
    fields: List[Field] = [
        ("20", ref16(lc.ref)),
        ("21", ref16(presentation.presenting_bank_ref or presentation.presentation_id)),
        ("32A", f"{swift_date(refusal_date)}{lc.terms.currency}{swift_amount(presentation.claimed_amount)}"),
        ("77J", lines(text, 50, 70)),
        ("77B", "/HOLD/ DOCUMENTS HELD PENDING\nFURTHER INSTRUCTIONS"),
    ]
    return build_fin("734", sender_bic, presentation.presenting_bank_bic, fields)


def mt732(lc, presentation, maturity: date, sender_bic: str) -> str:
    """Advice of discharge/acceptance of a usance drawing (used here as the maturity advice)."""
    fields: List[Field] = [
        ("20", ref16(lc.ref)),
        ("21", ref16(presentation.presenting_bank_ref or presentation.presentation_id)),
        ("30", swift_date(maturity)),
        ("32B", f"{lc.terms.currency}{swift_amount(presentation.claimed_amount)}"),
        ("72Z", lines(f"DOCUMENTS FOUND COMPLYING. WE WILL PAY AT MATURITY {maturity.isoformat()}.", 35, 6)),
    ]
    return build_fin("732", sender_bic, presentation.presenting_bank_bic, fields)


def mt756(lc, presentation, value_date: date, amount: Decimal, sender_bic: str) -> str:
    fields: List[Field] = [
        ("20", ref16(lc.ref)),
        ("21", ref16(presentation.presenting_bank_ref or presentation.presentation_id)),
        ("32B", f"{lc.terms.currency}{swift_amount(presentation.claimed_amount)}"),
        ("33A", f"{swift_date(value_date)}{lc.terms.currency}{swift_amount(amount)}"),
        ("72Z", lines("COMPLYING PRESENTATION HONOURED. COVER SENT VIA MT202.", 35, 6)),
    ]
    return build_fin("756", sender_bic, presentation.presenting_bank_bic, fields)


# ---------------------------------------------------------------------------
# Guarantees / standby LCs / surety bonds (SR2021 MT760 family, simplified sequence B)
# ---------------------------------------------------------------------------

def mt760(deal, applicant, beneficiary, receiver_bic: str, sender_bic: str, undertaking_text: str,
          form: str = "GUAR", rules: str = "URDG", underlying: str = "") -> str:
    expiry = getattr(deal.terms, "expiry_date", None)
    counter = getattr(deal.terms, "counter_guarantee", False)
    fields: List[Field] = [
        ("27", "1/1"),
        ("22A", "ICCO" if counter else "ISSU"),  # ICCO: issue counter-undertaking, request local undertaking
        ("15B", ""),
        ("20", ref16(deal.ref)),
        ("30", swift_date(deal.issue_date)),
        ("22D", form),
        ("40C", rules),
        ("23B", "FIXD" if expiry else "OPEN"),
        ("31E", swift_date(expiry) if expiry else ""),
        ("50", party_block(applicant.name, applicant.address, applicant.country)),
        ("52A", sender_bic),
        ("59", party_block(beneficiary.name, beneficiary.address, beneficiary.country)),
        ("32B", f"{deal.currency}{swift_amount(deal.amount)}"),
        ("45L", lines(underlying, 65, 50)),
        ("77U", lines(undertaking_text, 65, 150)),
    ]
    if counter:  # sequence C: the local undertaking the receiving bank is asked to issue
        local_exp = deal.terms.local_guarantee_expiry
        fields += [
            ("22D", "DGAR"),
            ("40C", rules),
            ("23B", "FIXD" if local_exp else "OPEN"),
            ("31E", swift_date(local_exp) if local_exp else ""),
            ("59", party_block(beneficiary.name, beneficiary.address, beneficiary.country)),
            ("32B", f"{deal.currency}{swift_amount(deal.amount)}"),
        ]
    return build_fin("760", sender_bic, receiver_bic, fields)


def mt767(deal, amendment_no: int, changes: dict, amend_date: date, receiver_bic: str, sender_bic: str) -> str:
    fields: List[Field] = [
        ("27", "1/1"),
        ("22A", "ISCA"),
        ("20", ref16(deal.ref)),
        ("21", ref16(deal.ref)),
        ("26E", str(amendment_no)),
        ("30", swift_date(amend_date)),
    ]
    delta = changes.get("amount_delta")
    if delta:
        d = Decimal(str(delta))
        fields.append(("32B" if d > 0 else "33B", f"{deal.currency}{swift_amount(abs(d))}"))
    if changes.get("expiry_date"):
        fields.append(("23B", "FIXD"))
        fields.append(("31E", swift_date(changes["expiry_date"])))
    if changes.get("narrative"):
        fields.append(("77U", lines(changes["narrative"])))
    return build_fin("767", sender_bic, receiver_bic, fields)


def mt769(deal, reduction: Decimal, outstanding: Decimal, on_date: date, receiver_bic: str, sender_bic: str,
          narrative: str) -> str:
    fields: List[Field] = [
        ("20", ref16(deal.ref)),
        ("21", ref16(deal.ref)),
        ("30", swift_date(on_date)),
        ("33B", f"{deal.currency}{swift_amount(reduction)}"),
        ("39C", ""),
        ("34B", f"{deal.currency}{swift_amount(outstanding)}"),
        ("72Z", lines(narrative, 35, 6)),
    ]
    return build_fin("769", sender_bic, receiver_bic, fields)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

def mt202(ref: str, related_ref: str, value_date: date, currency: str, amount: Decimal,
          beneficiary_bank_bic: str, receiver_bic: str, sender_bic: str) -> str:
    fields: List[Field] = [
        ("20", ref16(ref)),
        ("21", ref16(related_ref)),
        ("32A", f"{swift_date(value_date)}{currency}{swift_amount(amount)}"),
        ("58A", beneficiary_bank_bic),
    ]
    return build_fin("202", sender_bic, receiver_bic, fields)


def mt103(ref: str, value_date: date, currency: str, amount: Decimal, ordering_name: str, ordering_account: str,
          beneficiary_name: str, beneficiary_account: str, beneficiary_country: str, remittance: str,
          receiver_bic: str, sender_bic: str) -> str:
    fields: List[Field] = [
        ("20", ref16(ref)),
        ("23B", "CRED"),
        ("32A", f"{swift_date(value_date)}{currency}{swift_amount(amount)}"),
        ("50K", f"/{ordering_account}\n" + party_block(ordering_name)),
        ("59", f"/{beneficiary_account}\n" + party_block(beneficiary_name, "", beneficiary_country)),
        ("70", lines(remittance, 35, 4)),
        ("71A", "SHA"),
    ]
    return build_fin("103", sender_bic, receiver_bic, fields)


# ---------------------------------------------------------------------------
# Export LCs (we are advising / confirming / nominated bank)
# ---------------------------------------------------------------------------

def mt730(deal, sender_bic: str, receiver_bic: str, today: date, confirmed: bool) -> str:
    """Acknowledgement of the issuing bank's MT700 (advised, with or without our confirmation)."""
    t = deal.terms
    return build_fin("730", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.issuing_bank_lc_ref)), ("30", swift_date(today)),
        ("72Z", lines("CREDIT ADVISED TO BENEFICIARY" + (" WITH OUR CONFIRMATION ADDED" if confirmed else
                                                          " WITHOUT OUR CONFIRMATION"), 35, 6))])


def mt754(deal, presentation, amount: Decimal, value_date: date, sender_bic: str, receiver_bic: str, honoured: bool) -> str:
    """Advice of payment / acceptance / negotiation to the issuing bank (documents follow by courier)."""
    t = deal.terms
    return build_fin("754", sender_bic, receiver_bic, [
        ("20", ref16(f"{deal.ref}{presentation.presentation_id[-4:]}")), ("21", ref16(t.issuing_bank_lc_ref)),
        ("32A" if honoured else "32B", (f"{swift_date(value_date)}{t.currency}{swift_amount(amount)}" if honoured
                                        else f"{t.currency}{swift_amount(amount)}")),
        ("77A", lines("DOCUMENTS COMPLY WITH CREDIT TERMS. DOCUMENTS SENT TO YOU BY COURIER." +
                      (" WE HAVE HONOURED AS CONFIRMING BANK. PLEASE REIMBURSE." if honoured else
                       " PLEASE HONOUR / REMIT PROCEEDS QUOTING OUR REFERENCE."), 35, 20))])


def mt742(deal, amount: Decimal, value_date: date, sender_bic: str, receiver_bic: str) -> str:
    """Reimbursement claim on the reimbursing bank named in the credit."""
    t = deal.terms
    return build_fin("742", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.issuing_bank_lc_ref)), ("52A", t.issuing_bank_bic),
        ("32B", f"{t.currency}{swift_amount(amount)}"), ("34A", f"{swift_date(value_date)}{t.currency}{swift_amount(amount)}")])


def mt750(deal, presentation, discrepancies, sender_bic: str, receiver_bic: str) -> str:
    """Advice of discrepancy: asks the issuing bank to authorise payment despite the discrepancies."""
    t = deal.terms
    text = "; ".join(d.description for d in discrepancies)
    return build_fin("750", sender_bic, receiver_bic, [
        ("20", ref16(f"{deal.ref}{presentation.presentation_id[-4:]}")), ("21", ref16(t.issuing_bank_lc_ref)),
        ("32B", f"{t.currency}{swift_amount(presentation.claimed_amount)}"),
        ("77J", lines("DISCREPANCIES: " + text, 50, 70))])


def mt720(deal, transfer: dict, sender_bic: str, receiver_bic: str, today: date) -> str:
    """Transfer of a transferable credit to a second beneficiary (UCP 600 Art. 38)."""
    t = deal.terms
    sb = transfer["second_beneficiary"]
    return build_fin("720", sender_bic, receiver_bic, [
        ("27", "1/1"), ("40B", "IRREVOCABLE\nWITHOUT OUR CONFIRMATION"), ("20", ref16(transfer["transfer_ref"])),
        ("21", ref16(t.issuing_bank_lc_ref)), ("31C", swift_date(today)), ("40E", "UCP LATEST VERSION"),
        ("31D", f"{swift_date(transfer['expiry_date'])}GERMANY"),
        ("59", party_block(sb["name"], sb.get("address", ""), sb.get("country", ""))),
        ("32B", f"{t.currency}{swift_amount(transfer['amount'])}"), ("44C", swift_date(transfer["latest_shipment_date"])),
        ("45A", lines(t.goods_description, 65, 20))])


# ---------------------------------------------------------------------------
# Documentary collections (URC 522)
# ---------------------------------------------------------------------------

def mt410(deal, sender_bic: str, receiver_bic: str) -> str:
    """Acknowledgement of a collection received from the remitting bank."""
    t = deal.terms
    return build_fin("410", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.counterparty_bank_ref or deal.ref)),
        ("32A", f"{t.currency}{swift_amount(t.amount)}")])


def mt412(deal, maturity: date, sender_bic: str, receiver_bic: str) -> str:
    """Advice of acceptance of the bill of exchange by the drawee."""
    t = deal.terms
    return build_fin("412", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.counterparty_bank_ref or deal.ref)),
        ("32A", f"{swift_date(maturity)}{t.currency}{swift_amount(t.amount)}")])


def mt416(deal, reason: str, sender_bic: str, receiver_bic: str) -> str:
    """Advice of non-payment / non-acceptance."""
    t = deal.terms
    return build_fin("416", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.counterparty_bank_ref or deal.ref)),
        ("32B", f"{t.currency}{swift_amount(t.amount)}"), ("77A", lines(reason, 35, 20))])


def mt400(deal, amount: Decimal, value_date: date, sender_bic: str, receiver_bic: str) -> str:
    """Advice of payment of the collection proceeds (the funds move by pacs.009)."""
    t = deal.terms
    return build_fin("400", sender_bic, receiver_bic, [
        ("20", ref16(deal.ref)), ("21", ref16(t.counterparty_bank_ref or deal.ref)),
        ("32A", f"{swift_date(value_date)}{t.currency}{swift_amount(amount)}"),
        ("33A", f"{swift_date(value_date)}{t.currency}{swift_amount(amount)}")])
