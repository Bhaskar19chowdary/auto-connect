"""
Plain-language views of the books for people who are not technical: the client (company) and the bank employee.

Everything a deal records (statuses, workflow steps, system calls, exceptions) is turned into short sentences.
The client view never shows internal checks (sanctions, KYC, credit) - the bank must not tip off a client about a
sanctions review - and shows only "Under review by the bank" while a person decides.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

PRODUCT_NAMES = {
    "LC": "Import Letter of Credit",
    "EXPORT_LC": "Export Letter of Credit",
    "GUARANTEE": "Bank Guarantee",
    "SURETY": "Surety Bond (Buergschaft)",
    "COLLECTION": "Documentary Collection",
}
PRODUCT_HELP = {
    "LC": "The bank promises to pay your foreign supplier once they show documents proving they shipped the goods.",
    "EXPORT_LC": "A foreign bank promises to pay you when you present the shipping documents.",
    "GUARANTEE": "The bank promises to pay the other party if you do not meet your contract obligations.",
    "SURETY": "German-law surety (Buergschaft): the bank stands in for your obligation, e.g. for a building contract.",
    "COLLECTION": "The bank sends your shipping documents to the buyer's bank and releases them only against payment "
                  "or acceptance of a bill.",
}

# status -> (short label, tone, explanation for staff, explanation for the client)
_S = {
    "RECEIVED": ("Received", "blue", "The application arrived and is being processed automatically.",
                 "We have received your application and are processing it."),
    "ISSUED": ("Active", "green", "Issued and sent to the other bank. It is valid until the expiry date.",
               "Issued and sent. It is now valid."),
    "CONFIRMED": ("Confirmed", "green", "We added our own payment promise (confirmation) to the foreign bank's credit.",
                  "We have confirmed the credit - you are paid by us if the documents are correct."),
    "ADVISED": ("Advised", "green", "The credit was checked and passed on to our client.", "The credit was passed on to you."),
    "PARTIALLY_DRAWN": ("Partly used", "green", "Part of the amount has been paid out; the rest is still available.",
                        "Part of the amount has been paid; the rest is still available."),
    "CLOSED": ("Completed", "gray", "Fully finished. Nothing is outstanding.", "Everything is finished. Nothing is outstanding."),
    "EXPIRED": ("Expired", "gray", "The validity date passed; the commitment has ended.", "The validity date has passed."),
    "REJECTED": ("Rejected", "red", "The application was declined after a decision by the bank.",
                 "We could not accept this application. Your relationship manager will contact you."),
    "BLOCKED_SANCTIONS": ("Blocked", "red", "Blocked by Compliance because of a confirmed sanctions match.",
                          "This transaction cannot be processed. Please contact your relationship manager."),
    "PAID": ("Paid", "green", "The money has been paid.", "The money has been paid."),
    "UNPAID": ("Unpaid", "red", "The buyer did not pay or accept.", "The buyer did not pay or accept the documents."),
    "SENT_TO_COLLECTING_BANK": ("Documents sent", "blue", "The documents are on their way to the buyer's bank.",
                                "Your documents have been sent to the buyer's bank."),
    "PRESENTED_TO_DRAWEE": ("Waiting for buyer", "amber", "The buyer has been asked to pay or accept.",
                            "The documents are waiting for your payment or acceptance."),
    "ACCEPTED_AWAITING_MATURITY": ("Accepted - waiting for due date", "blue",
                                   "The bill was accepted; payment follows on the due date.",
                                   "Accepted. Payment follows on the due date."),
    "PAID_AWAITING_REIMBURSEMENT": ("Paid - waiting for reimbursement", "blue",
                                    "We paid and are waiting to be reimbursed by the other bank.",
                                    "Paid. We are settling with the other bank."),
    "DOCUMENTS_WITH_ISSUING_BANK": ("Documents with foreign bank", "blue",
                                    "Documents were sent to the issuing bank for payment.",
                                    "Your documents are with the foreign bank for payment."),
    "SENT_FOR_HONOUR": ("Sent for payment", "blue", "Documents were sent to the issuing bank for payment.",
                        "Your documents were sent for payment."),
    "SENT_ON_APPROVAL": ("Sent on approval", "blue", "Discrepant documents were sent to the issuing bank to ask for "
                         "approval.", "Your documents were sent to the foreign bank asking it to accept them."),
    "DOCUMENTS_DISCREPANT": ("Documents not in order", "amber", "The documents do not match the credit terms.",
                             "Some documents do not match the terms."),
    "TRANSFERRED": ("Transferred", "blue", "The credit was transferred to a second beneficiary.",
                    "The credit was transferred."),
    "DEMAND_UNDER_EXAMINATION": ("Payment demand received", "amber", "The beneficiary asked for payment; the demand "
                                 "is being checked.", "The beneficiary has asked for payment; we are checking it."),
    "AWAITING_PRINCIPAL": ("Waiting for client", "amber", "A claim was received; we are waiting for the client's answer.",
                           "A claim was received. Please tell us whether you accept or dispute it."),
    "DISPUTED": ("Disputed", "amber", "The client disputes the claim; Legal decides.",
                 "You disputed the claim. Our legal team is reviewing it."),
    "RETURNED_FOR_CORRECTION": ("Returned to client", "gray",
                                "Sent back to the client to correct the application. A corrected application creates a new "
                                "transaction; nothing was booked for this one.",
                                "We sent this back to you for correction - please see My applications."),
    "PENDING_BENEFICIARY": ("Waiting for beneficiary", "blue", "An amendment waits for the beneficiary's consent.",
                            "A change waits for the other party's consent."),
}
_HOLD = {
    "SANCTIONS": "Possible sanctions match - Compliance must check it",
    "KYC": "Customer identity file (KYC) must be updated - Compliance decides",
    "CREDIT": "Not enough credit limit - the Credit department must approve",
    "DATA_QUALITY": "Something is incomplete or unclear - a document checker must look at it",
    "LEGAL": "A claim is disputed - Legal must decide",
    "OPERATIONS": "A bank system was not available - operations must retry",
}


def status_view(status: str, audience: str = "staff") -> Dict[str, str]:
    if status.startswith("ON_HOLD_"):
        cat = status[len("ON_HOLD_"):]
        if audience == "client":
            return {"label": "Under review", "tone": "amber",
                    "text": "Our team is reviewing your transaction. You do not need to do anything; we will inform you."}
        return {"label": "Waiting for a decision", "tone": "amber", "text": _HOLD.get(cat, f"Waiting for {cat.lower()}")}
    label, tone, staff, client = _S.get(status, (status.replace("_", " ").capitalize(), "blue", "", ""))
    return {"label": label, "tone": tone, "text": client if audience == "client" else staff}


APPLICATION_STATUS = {
    "SUBMITTED": ("Submitted", "blue", "Saved and being processed."),
    "PROCESSED": ("Processed", "green", "The transaction was created."),
    "REJECTED": ("Not accepted", "red", "The application could not be processed."),
    "WITHDRAWN": ("Withdrawn", "gray", "The application was withdrawn and will not be processed."),
    "NEEDS_CORRECTION": ("Please correct", "amber", "Some details need correcting before the bank can process the application."),
    "WAITING_FOR_SYSTEM": ("Saved - processing soon", "amber",
                           "A bank system was briefly unavailable. Your application is safely saved and will be processed "
                           "automatically."),
}

# exception category -> what an employee needs to know to decide
DECISIONS = {
    "SANCTIONS": {
        "title": "Possible sanctions match",
        "who": "Compliance",
        "explain": "A name, country, ship or goods description in this transaction looks similar to an entry on the EU or "
                   "US sanctions lists. A person must check whether it is really the same party.",
        "approve": "Not a match - continue", "reject": "Real match - stop the transaction",
        "if_approve": "The transaction continues automatically from where it stopped.",
        "if_reject": "The transaction is stopped and blocked. The client is NOT told the reason (no tipping-off).",
    },
    "KYC": {
        "title": "Customer identity file (KYC) not up to date",
        "who": "Compliance",
        "explain": "The client's identity documents or periodic review are missing or expired.",
        "approve": "KYC is fine - continue", "reject": "Stop the transaction",
        "if_approve": "The transaction continues automatically.", "if_reject": "The application is rejected.",
    },
    "CREDIT": {
        "title": "Not enough credit limit",
        "who": "Credit department",
        "explain": "The client's approved limit for this product is not enough for this amount, or the commitment has "
                   "no end date. Someone with credit authority must approve the extra risk.",
        "approve": "Approve the limit excess", "reject": "Decline",
        "if_approve": "A temporary limit excess is booked and the transaction continues.",
        "if_reject": "The application is declined and the client is informed.",
    },
    "DATA_QUALITY": {
        "title": "Check needed: incomplete or unclear data",
        "who": "Document checker",
        "explain": "Some details in the application or in the documents read from scans could not be confirmed "
                   "automatically. Please look at the details and the original documents.",
        "approve": "Details are correct - continue", "reject": "Not acceptable - reject",
        "return": "Send back to client to correct",
        "if_return": "Nothing was booked yet: the transaction is cancelled and the client's application opens again for "
                     "correction. The client gets a message with your note.",
        "if_approve": "The transaction continues with the details as they are.",
        "if_reject": "The application / presentation is rejected.",
    },
    "LEGAL": {
        "title": "Disputed claim",
        "who": "Legal",
        "explain": "The client disputes a claim made under the surety or guarantee. Legal must decide whether the bank "
                   "pays.",
        "approve": "Pay the claim", "reject": "Refuse the claim",
        "if_approve": "The claim is paid and the client's account is debited.",
        "if_reject": "The claim is refused and the claimant is informed.",
    },
    "OPERATIONS": {
        "title": "A bank system was not available",
        "who": "Operations",
        "explain": "One of the bank's systems did not answer while this transaction was processed. Nothing was "
                   "half-booked. Once the system works again, retry.",
        "approve": "Retry now", "reject": "Cancel the transaction",
        "if_approve": "The transaction is processed again from the step that failed.",
        "if_reject": "The transaction is cancelled.",
    },
}

SYSTEMS = {
    "tf_customer_master": "Customer records", "tf_sanctions_screening": "Sanctions check",
    "tf_market_data": "Exchange rates", "tf_core_ledger": "Accounting", "tf_credit_limits": "Credit limits",
    "tf_swift_gateway": "SWIFT", "tf_document_archive": "Document archive", "tf_email_gateway": "Email",
    "tf_client_portal": "Client portal", "tf_operations_engine": "Auto-connect",
}
SWIFT_MEANING = {
    "700": "Letter of credit issued to the seller's bank", "707": "Change (amendment) to a letter of credit",
    "710": "Letter of credit passed on with our confirmation", "720": "Letter of credit transferred",
    "730": "Acknowledgement", "732": "Documents released / discrepancies accepted", "734": "Documents refused",
    "740": "Reimbursement authorisation", "742": "Reimbursement claim", "750": "Discrepancy advice",
    "752": "Authorisation to pay despite discrepancies", "754": "Documents sent and payment claimed",
    "756": "Payment / reimbursement advice", "759": "Free-format trade message",
    "760": "Guarantee issued", "767": "Change (amendment) to a guarantee", "768": "Acknowledgement of a guarantee message",
    "769": "Reduction or release of a guarantee", "765": "Payment demand under a guarantee",
    "785": "Guarantee expiry / non-extension notice", "786": "Demand refusal", "787": "Demand acceptance",
    "400": "Collection payment advice", "410": "Collection acknowledgement", "412": "Advice of acceptance",
    "416": "Advice of non-payment / non-acceptance", "420": "Tracer", "430": "Amendment of collection instructions",
    "pacs.008": "Payment to a customer (ISO 20022)", "pacs.009": "Payment between banks (ISO 20022)",
}
DOC_NAMES = {
    "COMMERCIAL_INVOICE": "Commercial invoice", "BILL_OF_LADING": "Bill of lading (sea transport)",
    "AIR_WAYBILL": "Air waybill", "INSURANCE_DOCUMENT": "Insurance certificate", "PACKING_LIST": "Packing list",
    "CERTIFICATE_OF_ORIGIN": "Certificate of origin", "INSPECTION_CERTIFICATE": "Inspection certificate",
    "BENEFICIARY_STATEMENT": "Beneficiary's statement", "DEMAND": "Demand letter",
    "STATEMENT_OF_BREACH": "Statement of breach", "NOTICE_OF_DEFAULT": "Notice of default",
    "EVIDENCE_OF_CLAIM": "Evidence of claim", "GUARANTOR_STATEMENT": "Guarantor statement",
    "BILL_OF_EXCHANGE": "Bill of exchange", "OTHER": "Other document",
}
FEE_NAMES = {
    "LC_ISSUANCE_COMMISSION": "Issuing commission", "SWIFT": "SWIFT charge", "LC_PAYMENT_COMMISSION": "Payment commission",
    "GUARANTEE_ISSUANCE": "Guarantee issuing fee", "AVAL_COMMISSION": "Guarantee commission",
    "DISCREPANCY": "Discrepancy fee", "AMENDMENT": "Amendment fee",
}

_STEP_TITLES = [  # (step prefix, title)
    ("RECEIVED", "Application received"), ("VALIDATION", "Details checked"), ("KYC", "Identity check (KYC)"),
    ("SANCTIONS", "Sanctions check"), ("CREDIT", "Credit limit"), ("CONFIRMATION_LIMIT", "Bank limit check"),
    ("FEE_", "Fee"), ("BOOKING", "Accounting"), ("ISSUANCE", "Issued"), ("NOTIFY_CLIENT", "Client informed"),
    ("PRESENTATION", "Documents received"), ("EXAMINATION", "Documents checked"), ("PAYMENT", "Payment"),
    ("RELEASE", "Closed"), ("AMEND", "Amendment"), ("DEMAND", "Payment demand"), ("CLAIM", "Claim"),
    ("EOD", "Daily processing"), ("ADVICE", "Advised"), ("CONFIRMATION", "Confirmation"), ("ACCEPTANCE", "Accepted"),
    ("PROCEEDS", "Money received"), ("NON_PAYMENT", "Not paid"), ("DISPATCH", "Documents sent"),
    ("COUNTERPARTY_NOTICE", "Other party informed"), ("EXCEPTION", "Decision taken"), ("DOCUMENT_ARCHIVED", "Document stored"),
    ("ARCHIVE", "Archived"), ("FX", "Exchange rate"), ("INTERNAL_ALERT", "Staff alerted"), ("WAIVER", "Client decision"),
    ("TRANSFER", "Transfer"), ("REMINDER", "Reminder"), ("EXPIRY", "Expiry"), ("AMENDMENT", "Change (amendment)"),
]
_CLIENT_HIDDEN = ("KYC", "SANCTIONS", "CREDIT", "CONFIRMATION_LIMIT", "FEE_", "BOOKING", "NOTIFY_CLIENT", "FX",
                  "INTERNAL_ALERT", "ARCHIVE", "EXCEPTION", "EOD", "VALIDATION")


def _title(step: str) -> str:
    for prefix, title in _STEP_TITLES:
        if step.startswith(prefix):
            return title
    return step.split("_PRS")[0].replace("_", " ").capitalize()


def _nice(op: str) -> str:
    return op.replace("_", " ").lower().capitalize()


_REFUSAL_WORDS = ("INSUFFICIENT", "NOT_FOUND", "REJECT", "INVALID", "DENIED", "BLOCKED", "EXCEEDED", "UNKNOWN", "DUPLICATE")


def is_outage(event) -> bool:
    """A FAILED step is an outage (system down / timeout) unless the system answered with a business refusal."""
    if event.outcome != "FAILED":
        return False
    status = (event.data or {}).get("status_code")
    if isinstance(status, int) and status:
        return status >= 500 or status in (408, 429)
    code = (event.detail or "").split(":", 1)[0]
    return not any(w in code for w in _REFUSAL_WORDS)


def story(deal, audience: str = "staff") -> List[Dict[str, Any]]:
    """The deal's history as sentences. technical=True marks individual system calls (hidden by default)."""
    out: List[Dict[str, Any]] = []
    for e in deal.history:
        step, system, outcome, detail = e.step, e.system, e.outcome, e.detail or ""
        sysname = SYSTEMS.get(system, system)
        item = {"at": e.at, "business_date": e.business_date.isoformat() if e.business_date else None,
                "title": _title(step), "text": detail, "tone": "green", "technical": False, "system": sysname}
        if outcome == "FAILED":
            if audience == "client":
                continue
            if is_outage(e):
                item.update(tone="red", title=f"{sysname} did not answer", text=detail)
            else:
                code, _, rest = detail.partition(":")
                item.update(tone="amber", title=f"{sysname} said no: {code.replace('_', ' ').lower()}", text=rest.strip())
        elif outcome == "ESCALATED":
            if audience == "client":
                item.update(tone="amber", title="Under review", text="Our team is reviewing your transaction.")
            else:
                cat = detail.split(":", 1)[0]
                item.update(tone="amber", title=f"Stopped for a decision ({DECISIONS.get(cat, {}).get('who', cat)})",
                            text=detail.split(":", 1)[-1].strip())
        elif outcome == "OK":  # a single call to one of the bank's systems
            if audience == "client":
                continue
            text = {"POST_ACCOUNTING": "Booked in the accounts", "NOTIFY_CLIENT": "Client informed by email and portal",
                    "RELEASE_CREDIT_LIMIT": "Credit limit released", "ARCHIVE_DOCUMENT": "Copy stored in the archive"}.get(detail)
            if detail.startswith("SEND_MT"):
                mt = detail[len("SEND_MT"):]
                text = f"SWIFT message {mt} sent - {SWIFT_MEANING.get(mt, 'trade message')}"
            item.update(tone="gray", technical=True, text=text or _nice(detail), title=f"{_title(step)} - {sysname}")
            if step.startswith("FEE_"):
                item["text"] = f"Fee booked: {FEE_NAMES.get(step[4:], _nice(step[4:]))}"
        else:  # DONE: a business milestone
            if audience == "client" and step.startswith(_CLIENT_HIDDEN):
                continue
            if step == "SANCTIONS" or step.startswith("SANCTIONS_"):
                item["text"] = "No match on the EU and US sanctions lists" if detail.startswith("CLEAR") else detail
            elif step == "KYC" and detail == "KYC valid":
                item["text"] = "The client's identity file is valid"
            elif step == "VALIDATION" and detail == "Application checks passed":
                item["text"] = "All details are complete and follow the rules"
            elif step == "CREDIT" and detail.startswith("Limit reserved"):
                how = detail.split("via", 1)[-1].strip()
                item["text"] = "The amount is reserved on the client's credit limit" + (
                    " (headroom moved from another facility)" if "REALLOCATION" in how else
                    " (secured by cash collateral)" if "COLLATERAL" in how else "")
            item["text"] = _soften(item["text"])
        out.append(item)
    return out


def _soften(text: str) -> str:
    return (text.replace("Complying presentation (UCP 600 Art. 15)", "Documents checked: everything matches the credit terms")
            .replace("Credit fully utilised - closed", "The full amount was used - the credit is completed"))


def client_actions(deal) -> List[Dict[str, Any]]:
    """Decisions the client must take now (shown as buttons in the client portal)."""
    acts: List[Dict[str, Any]] = []
    p = deal.product
    if p == "LC":
        for pr in deal.presentations:
            if pr.status == "DISCREPANT_AWAITING_APPLICANT":
                acts.append({"item_id": pr.presentation_id, "kind": "waiver",
                             "title": "Documents do not fully match - do you accept them?",
                             "text": f"The seller's documents for {pr.claimed_amount:,.2f} {deal.currency} have these "
                                     "problems. If you accept, we pay the seller. If you refuse, the documents are not paid.",
                             "problems": [d.description for d in pr.discrepancies],
                             "deadline": pr.applicant_decision_deadline.isoformat() if pr.applicant_decision_deadline else None,
                             "options": [{"decision": "waive", "label": "Accept and pay", "tone": "green"},
                                         {"decision": "refuse", "label": "Refuse the documents", "tone": "red"}]})
    elif p == "EXPORT_LC":
        for pr in deal.presentations:
            if pr.status == "DISCREPANT_AWAITING_APPLICANT":
                acts.append({"item_id": pr.presentation_id, "kind": "instruction",
                             "title": "Your documents have discrepancies - what should we do?",
                             "text": "We can send them to the foreign bank asking it to accept them anyway, or you can "
                                     "correct and present them again.",
                             "problems": [d.description for d in pr.discrepancies], "deadline": None,
                             "options": [{"decision": "send_on_approval", "label": "Send them anyway", "tone": "blue"},
                                         {"decision": "correct", "label": "I will correct them", "tone": "gray"}]})
    elif p == "GUARANTEE":
        for d in deal.demands:
            if d.status == "SUSPENDED_EXTEND_OR_PAY":
                acts.append({"item_id": d.demand_id, "kind": "extend_or_pay",
                             "title": "Extend the guarantee or let the bank pay?",
                             "text": f"The beneficiary demands {d.amount:,.2f} {d.currency} unless the guarantee is "
                                     f"extended{(' to ' + d.requested_new_expiry.isoformat()) if d.requested_new_expiry else ''}.",
                             "problems": [], "deadline": d.suspension_end.isoformat() if d.suspension_end else None,
                             "options": [{"decision": "extend", "label": "Extend the guarantee", "tone": "green"},
                                         {"decision": "pay", "label": "Let the bank pay", "tone": "red"}]})
    elif p == "SURETY":
        for c in deal.claims:
            if c.status == "AWAITING_PRINCIPAL":
                acts.append({"item_id": c.claim_id, "kind": "claim",
                             "title": "A claim was made under your surety - do you accept it?",
                             "text": f"The obligee claims {c.amount:,.2f} {c.currency}. If you accept, we pay and debit "
                                     "your account. If you dispute it, please give a reason; our legal team decides.",
                             "problems": [], "deadline": c.principal_deadline.isoformat() if c.principal_deadline else None,
                             "options": [{"decision": "acknowledge", "label": "Accept the claim", "tone": "green"},
                                         {"decision": "dispute", "label": "Dispute the claim", "tone": "red",
                                          "needs_reason": True}]})
    elif p == "COLLECTION":
        if deal.terms.direction == "IMPORT" and deal.status == "PRESENTED_TO_DRAWEE":
            opts = [{"decision": "pay", "label": "Pay now", "tone": "green"}]
            if deal.terms.release_against == "ACCEPTANCE":
                opts.append({"decision": "accept", "label": "Accept the bill (pay later)", "tone": "blue"})
            opts.append({"decision": "refuse", "label": "Refuse", "tone": "red"})
            acts.append({"item_id": deal.ref, "kind": "drawee",
                         "title": "Documents arrived for you - pay or accept to receive them",
                         "text": f"Amount: {deal.amount:,.2f} {deal.currency}.", "problems": [],
                         "deadline": deal.response_deadline.isoformat() if deal.response_deadline else None,
                         "options": opts})
    return acts


def counterparty(deal) -> Optional[Dict[str, Any]]:
    t = deal.terms
    party = getattr(t, "beneficiary", None) or getattr(t, "obligee", None) or getattr(t, "counterparty", None) \
        or getattr(t, "applicant", None)
    return party.model_dump(mode="json") if party is not None and hasattr(party, "model_dump") else None


def key_facts(deal) -> List[Dict[str, str]]:
    """The most important terms with human labels."""
    t = deal.terms
    facts: List[Dict[str, str]] = []

    def add(label, value):
        if value not in (None, "", [], False):
            facts.append({"label": label, "value": str(value)})

    cp = counterparty(deal) or {}
    role = {"LC": "Seller (beneficiary)", "GUARANTEE": "Beneficiary", "SURETY": "Obligee (protected party)",
            "COLLECTION": "Buyer" if getattr(t, "direction", "") == "EXPORT" else "Seller", "EXPORT_LC": "Buyer (applicant)"}
    add(role.get(deal.product, "Other party"), f"{cp.get('name', '')} ({cp.get('country', '')})" if cp else None)
    add("Amount", f"{deal.amount:,.2f} {deal.currency}")
    add("Still open", f"{deal.outstanding:,.2f} {deal.currency}")
    add("Issued on", deal.issue_date.isoformat() if deal.issue_date else None)
    add("Valid until", getattr(t, "expiry_date", None) and t.expiry_date.isoformat())
    if deal.product in ("LC", "EXPORT_LC"):
        add("Goods", t.goods_description)
        add("Shipment", f"{t.port_of_loading} to {t.port_of_discharge}, latest {t.latest_shipment_date.isoformat()}")
        add("Delivery terms", t.incoterms)
        add("Payment", {"SIGHT_PAYMENT": "At sight (immediately)", "DEFERRED_PAYMENT": f"Deferred ({t.usance_days} days)",
                        "ACCEPTANCE": f"Acceptance ({t.usance_days} days)", "NEGOTIATION": "Negotiation"}[t.available_by])
        add("Documents required", ", ".join(DOC_NAMES.get(r.doc_type, r.doc_type) for r in t.documents_required))
    if deal.product == "LC":
        add("Seller's bank (BIC)", t.advising_bank_bic)
    if deal.product == "GUARANTEE":
        add("Type", t.guarantee_type.replace("_", " ").capitalize())
        add("Underlying contract", t.underlying_transaction)
        add("Rules", t.rules)
    if deal.product == "SURETY":
        add("Type", t.bond_type.replace("_", " ").capitalize())
        add("On first demand", "Yes" if t.on_first_demand else "No")
        add("Underlying contract", t.underlying_contract)
    if deal.product == "COLLECTION":
        add("Direction", "Export (we collect for our client)" if t.direction == "EXPORT" else "Import (our client pays)")
        add("Documents released against", "Payment" if t.release_against == "PAYMENT" else f"Acceptance ({t.usance_days} days)")
        add("Buyer's bank (BIC)", t.counterparty_bank_bic)
    return facts


def progress(deal) -> List[Dict[str, Any]]:
    """A simple step tracker: Submitted - Checked - Issued/Sent - Documents/Claims - Completed."""
    st = deal.status
    done_steps = {e.step.split("_PRS")[0] for e in deal.history if e.outcome in ("DONE", "OK")}
    has_docs = bool(getattr(deal, "presentations", None) or getattr(deal, "demands", None) or getattr(deal, "claims", None))
    paid = any(getattr(x, "status", "") == "PAID" for x in (getattr(deal, "presentations", []) or [])
               + (getattr(deal, "demands", []) or []) + (getattr(deal, "claims", []) or [])) or st in ("PAID",)
    issued = deal.issue_date is not None or "ISSUANCE" in done_steps or st not in ("RECEIVED", "REJECTED") \
        and not st.startswith("ON_HOLD")
    finished = st in ("CLOSED", "EXPIRED", "PAID", "REJECTED", "BLOCKED_SANCTIONS", "UNPAID", "RETURNED_FOR_CORRECTION")
    second = {"LC": "Documents & payment", "EXPORT_LC": "Documents & payment", "COLLECTION": "Payment",
              "GUARANTEE": "Claims (if any)", "SURETY": "Claims (if any)"}[deal.product]
    issued_label = {"COLLECTION": "Documents sent"}.get(deal.product, "Issued")
    steps = [
        {"label": "Submitted", "state": "done"},
        {"label": "Checked", "state": "done" if issued or "VALIDATION" in done_steps else "current"},
        {"label": issued_label, "state": "done" if issued else ("stopped" if st.startswith("ON_HOLD") or st in
                                                                ("REJECTED", "BLOCKED_SANCTIONS", "RETURNED_FOR_CORRECTION") else "todo")},
        {"label": second, "state": "done" if paid or (finished and has_docs) else ("current" if has_docs else "todo")},
        {"label": "Completed", "state": "done" if finished and st not in ("REJECTED", "BLOCKED_SANCTIONS", "RETURNED_FOR_CORRECTION") else "todo"},
    ]
    if st.startswith("ON_HOLD") and issued:
        steps[3]["state"] = "stopped"
    return steps


AMENDABLE = {"LC": ("ISSUED", "PARTIALLY_DRAWN"), "GUARANTEE": ("ISSUED",), "SURETY": ("ISSUED",)}
AMENDMENT_STATUS = {"PENDING_APPROVAL": ("Waiting for bank approval", "amber"),
                    "PENDING_BENEFICIARY": ("Waiting for the other party to agree", "blue"),
                    "ACCEPTED": ("Agreed", "green"), "EFFECTIVE": ("In force", "green"), "REJECTED": ("Not agreed", "red")}


def change_options(deal) -> Dict[str, Any]:
    """What the client may change on an issued transaction (formal amendment) - or why not."""
    allowed = deal.status in AMENDABLE.get(deal.product, ())
    pending = any(a.status in ("PENDING_APPROVAL", "PENDING_BENEFICIARY") for a in deal.amendments)
    if deal.product not in AMENDABLE:
        why = "This type of transaction cannot be changed online. Please contact your relationship manager."
    elif deal.status.startswith("ON_HOLD") or deal.status == "RECEIVED":
        why = "It is being reviewed by the bank right now. Changes are possible once it is issued."
    elif pending:
        why = "A previous change is still open. You can request another change once it is completed."
    elif not allowed:
        why = "This transaction is finished and can no longer be changed."
    else:
        why = ""
    t = deal.terms
    return {"can_change": allowed and not pending, "why_not": why,
            "current": {"amount": str(deal.amount), "currency": deal.currency,
                        "expiry_date": t.expiry_date.isoformat() if getattr(t, "expiry_date", None) else None,
                        "latest_shipment_date": t.latest_shipment_date.isoformat() if deal.product == "LC" else None},
            "needs_other_party": deal.product == "LC",
            "history": [{"number": a.number, "date": a.date.isoformat(), "label": AMENDMENT_STATUS.get(a.status, (a.status, "gray"))[0],
                         "tone": AMENDMENT_STATUS.get(a.status, (a.status, "gray"))[1],
                         "changes": _describe_changes(a.changes, deal.currency)} for a in deal.amendments]}


def _describe_changes(ch: Dict[str, Any], ccy: str) -> str:
    parts = []
    if ch.get("amount_delta") not in (None, "", "0", 0):
        d = float(ch["amount_delta"])
        parts.append(f"amount {'increased' if d > 0 else 'reduced'} by {abs(d):,.2f} {ccy}")
    if ch.get("expiry_date"):
        parts.append(f"valid until {ch['expiry_date']}")
    if ch.get("latest_shipment_date"):
        parts.append(f"latest shipment {ch['latest_shipment_date']}")
    if ch.get("narrative"):
        parts.append(f'reason: "{ch["narrative"]}"')
    return "; ".join(parts) or "change"


# Mistakes in an application that only the client can put right (typing errors, missing data). They are sent back to
# the client instead of waiting for a bank employee. Policy questions (e.g. open-ended guarantees) still go to staff.
CLIENT_FIXABLE = {
    "AMOUNT_INVALID": ("amount", "The amount must be more than zero."),
    "ADVISING_BIC_INVALID": ("advising_bank_bic", "This is not a valid SWIFT code (BIC). It has 8 or 11 letters/digits, "
                                                  "e.g. BNCAITMMXXX - please check it with the seller."),
    "BENEFICIARY_BANK_BIC_INVALID": ("beneficiary_bank_bic", "This is not a valid SWIFT code (BIC): 8 or 11 letters/digits."),
    "EXPIRY_IN_PAST": ("expiry_date", "The 'valid until' date must be in the future."),
    "SHIPMENT_AFTER_EXPIRY": ("latest_shipment_date", "The latest shipment date must be before the 'valid until' date."),
    "SHIPMENT_DATE_IN_PAST": ("latest_shipment_date", "The latest shipment date is in the past - please check the date "
                                                      "(also the year)."),
    "EXPIRY_TOO_SOON": ("expiry_date", "The 'valid until' date leaves no time to ship and present the documents - it must "
                                       "be after the latest shipment date plus the days allowed for presenting documents."),
    "NO_DOCUMENTS": ("documents_required", "Choose at least one document the seller must present."),
    "USANCE_MISSING": ("usance_days", "For deferred payment or acceptance, enter the number of days until payment."),
    "TRANSPORT_DOCUMENT_MISSING": ("documents_required", "Add a transport document (bill of lading or air waybill) - "
                                                         "it proves the goods were shipped."),
    "BENEFICIARY_IBAN_INVALID": ("beneficiary.iban", "The seller's IBAN is not valid - please check it."),
    "UNDERLYING_MISSING": ("underlying_transaction", "Say which contract or project the guarantee secures."),
    "COUNTER_BANK_MISSING": ("beneficiary_bank_bic", "A counter-guarantee needs the SWIFT code of the bank abroad."),
    "ADVANCE_MISSING": ("advance_payment_amount", "Enter the amount of the advance payment this surety secures."),
    "RENT_MISSING": ("monthly_rent", "Enter the monthly rent for a rent deposit surety."),
}


def client_problems(errors) -> List[Dict[str, str]]:
    """Validation errors (Discrepancy objects or 'CODE: text' strings) the client can correct, as field messages."""
    out = []
    for e in errors:
        code = getattr(e, "code", None) or str(e).split(":", 1)[0].strip()
        if code in CLIENT_FIXABLE:
            field, msg = CLIENT_FIXABLE[code]
            out.append({"code": code, "field": field, "message": msg})
    return out
