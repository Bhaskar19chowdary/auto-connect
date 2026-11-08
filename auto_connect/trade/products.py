"""
Export letters of credit and documentary collections, end to end.

Export LC (UCP 600) - the bank as advising / confirming / nominated bank for its exporting client:
  incoming MT700 -> KYC + sanctions -> advice to the beneficiary + MT730 -> optional confirmation against the
  issuing bank's counterparty limit (credit decision if not covered) -> transfer to a second beneficiary (MT720)
  -> examination of the beneficiary's documents -> confirmed: honour + MT754 / MT742 reimbursement claim;
  unconfirmed: documents sent for honour, beneficiary paid when the issuing bank pays; discrepant: beneficiary
  corrects or documents go on approval (MT750) -> reimbursement / refusal handling -> expiry.

Documentary collections (URC 522) - documents against payment (D/P) or acceptance (D/A), no bank undertaking:
  EXPORT: drawer's documents checked against the list -> sent to the collecting bank -> its advice of payment
  (MT400) / acceptance (MT412) / non-payment (MT416) -> drawer credited or informed.
  IMPORT: collection received from the remitting bank (MT410 acknowledgement) -> drawee asked to pay / accept /
  refuse -> debit and proceeds remitted (pacs.009 + MT400), acceptance advised (MT412) and paid at maturity,
  or non-payment advised (MT416).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .. import settings
from . import rules, swift
from .bankdays import add_banking_days, next_banking_day
from .models import (Account, Collection, CollectionTerms, ExportLC, ExportLCTerms, LCTerms, LetterOfCredit, Party,
                     Presentation, new_id)

D0 = Decimal("0")


class ProductsMixin:
    """Mixed into TradeFinanceEngine; uses its building blocks (_call, _post, _swift, _notify_client ...)."""

    # ------------------------------------------------------------------ helpers
    def _tariff(self, key: str) -> Decimal:
        return Decimal(str(self.state.reference.tariff.get(key, rules.DEFAULT_TARIFF[key])))

    def _ensure_accounts(self, currency: str):
        """Accounts introduced with export LCs / collections (created on first use in existing books)."""
        for acc_id, ccy, on_bs in [(f"GL_DUE_FROM_BANKS_{currency}", currency, True),
                                   ("OB_CONF_CUSTOMER_LIABILITY", "EUR", False), ("OB_CONF_BANK_COMMITMENT", "EUR", False)]:
            if acc_id not in self.state.accounts:
                self.state.accounts[acc_id] = Account(account_id=acc_id, owner="BANK", currency=ccy, on_balance_sheet=on_bs)

    async def _credit_customer_from(self, deal, currency: str, amount: Decimal, source_account: str, narrative: str,
                                    step: str) -> Decimal:
        """Credits our client's EUR account with `amount` in `currency` taken from `source_account` (nostro or
        receivable), converting at the market rate. Returns the EUR amount credited."""
        iban = (await self._customer(deal.customer_id)).current_account_iban
        if currency == "EUR":
            await self._post(deal, step, [{"debit": source_account, "credit": iban, "amount": amount, "narrative": narrative}])
            return rules.money(amount)
        eur = await self._eur(deal, amount, currency)
        await self._post(deal, step, [
            {"debit": source_account, "credit": f"GL_FX_POSITION_{currency}", "amount": amount, "narrative": narrative},
            {"debit": f"GL_FX_POSITION_{currency}", "credit": f"GL_EQUITY_{currency}", "amount": amount,
             "narrative": f"FX deal {currency} leg"},
            {"debit": "GL_EQUITY_EUR", "credit": "GL_FX_POSITION_EUR", "amount": eur, "narrative": "FX deal EUR leg"},
            {"debit": "GL_FX_POSITION_EUR", "credit": iban, "amount": eur, "narrative": f"{narrative} (EUR equivalent)"}])
        return eur

    def _nostro(self, currency: str) -> str:
        return self.state.reference.nostro_by_currency[currency]

    # ================================================================== EXPORT LC
    async def receive_export_lc(self, terms: ExportLCTerms, created_by: Optional[str] = None) -> ExportLC:
        async with self._unit_of_work():
            await self._customer(terms.beneficiary_customer_id)
            self._check_currency(terms.currency)
            ref = self.state.next_ref("ELC")
            d = ExportLC(ref=ref, customer_id=terms.beneficiary_customer_id, status="RECEIVED", contingent_booked_eur=D0, currency=terms.currency,
                         amount=rules.money(terms.amount), outstanding=rules.money(terms.amount), terms=terms,
                         created_by=created_by)
            self.state.deals[ref] = d
            self._event(d, "RECEIVED", terms.issuing_bank_bic, "DONE",
                        f"MT700 {terms.issuing_bank_lc_ref} received from {terms.issuing_bank_bic} (authenticated via SWIFT RMA)")
            await self._run_issuance(d, self._export_lc_pipeline)
            await self._acknowledge(d, "export documentary credit")
            return d

    def _export_errors(self, t: ExportLCTerms) -> List[str]:
        errors = []
        if t.amount <= 0:
            errors.append("Credit amount must be positive")
        if not swift.is_valid_bic(t.issuing_bank_bic or ""):
            errors.append(f"Issuing bank BIC '{t.issuing_bank_bic}' is invalid")
        if t.expiry_date <= self.today:
            errors.append("Credit has already expired (UCP 600 Art. 6(d))")
        if t.latest_shipment_date > t.expiry_date:
            errors.append("Latest shipment date is after the expiry date")
        if not t.documents_required:
            errors.append("Credit calls for no documents (UCP 600 Art. 5)")
        if t.available_by in ("DEFERRED_PAYMENT", "ACCEPTANCE") and t.usance_days <= 0:
            errors.append("Deferred payment / acceptance credit without usance period")
        return errors

    async def _export_lc_pipeline(self, d: ExportLC):
        t = d.terms
        if not self._done(d, "VALIDATION"):
            errors = self._export_errors(t)
            if errors and not self._use_approval(d, "DATA_QUALITY"):
                self._raise_exception(d, "DATA_QUALITY", " | ".join(errors), "ISSUE")
                return
            self._event(d, "VALIDATION", self.agent_id, "DONE", "Credit terms checked for apparent authenticity and "
                        "consistency (UCP 600 Art. 9(a))")
        if not await self._step_kyc(d):
            return
        cust = await self._customer(d.customer_id)
        parties = [{"name": t.applicant.name, "country": t.applicant.country, "address": t.applicant.address, "role": "applicant"},
                   {"name": f"Issuing bank {t.issuing_bank_bic}", "country": t.issuing_bank_bic[4:6], "role": "issuing bank"},
                   {"name": cust.name, "country": cust.country, "role": "beneficiary"}]
        if t.reimbursing_bank_bic:
            parties.append({"name": f"Reimbursing bank {t.reimbursing_bank_bic}", "country": t.reimbursing_bank_bic[4:6],
                            "role": "reimbursing bank"})
        if not await self._step_sanctions(d, parties, [t.port_of_loading, t.port_of_discharge], t.goods_description):
            return
        wants = t.confirmation_instructions == "CONFIRM" or (t.confirmation_instructions == "MAY ADD"
                                                             and t.beneficiary_requests_confirmation)
        if wants and not self._done(d, "CONFIRMATION"):
            exposure = await self._eur(d, rules.credit_ceiling(t), t.currency)
            if not self._done(d, "CONFIRMATION_LIMIT"):
                try:
                    await self._call(d, "CONFIRMATION", "tf_credit_limits", "earmark_bank_limit",
                                     {"bank_bic": t.issuing_bank_bic, "amount_eur": exposure, "deal_ref": d.ref,
                                      "credit_approved": "CREDIT" in d.approvals}, "RESERVE_COUNTERPARTY_LIMIT")
                except self.BusError as e:
                    if e.code not in ("CREDIT_LIMIT_INSUFFICIENT", "NO_FACILITY"):
                        raise
                    self._raise_exception(d, "CREDIT", f"Confirmation of {t.issuing_bank_bic} credit: {e.detail}", "ISSUE")
                    await self._notify_internal(d, "Confirmation needs credit approval", e.detail)
                    return
                self._use_approval(d, "CREDIT")
                d.limit_utilisation_eur = exposure
                self._event(d, "CONFIRMATION_LIMIT", "tf_credit_limits", "DONE", f"{t.issuing_bank_bic} line reserved EUR {exposure}")
            self._ensure_accounts(t.currency)
            d.confirmed = True
            if self._contingent_open(d, "CONF") == 0:
                await self._book_contingent(d, "CONF", exposure, step="CONFIRMATION")
            fee = max(self._tariff("confirmation_min"), rules.money(
                exposure * self._tariff("confirmation_rate_per_quarter_pct") / 100 * rules.quarters_started(self.today, t.expiry_date)))
            await self._charge_fee(d, "CONFIRMATION_COMMISSION", fee, period_end=t.expiry_date, once=True)
            self._event(d, "CONFIRMATION", self.agent_id, "DONE",
                        f"Confirmation added: we are bound as confirming bank (UCP 600 Art. 8); "
                        f"{t.issuing_bank_bic} limit reserved EUR {exposure}")
        await self._advise_export_lc(d)

    async def _advise_export_lc(self, d: ExportLC):
        if self._done(d, "ADVICE"):
            return
        t = d.terms
        await self._charge_fee(d, "ADVISING_COMMISSION", self._tariff("export_advising_flat"), once=True)
        if not self._did(d, "ADVICE", "tf_swift_gateway"):
            await self._swift(d, "ADVICE", "730", t.issuing_bank_bic, swift.mt730(d, settings.bank_bic(), t.issuing_bank_bic,
                                                                                self.today, d.confirmed))
        d.issue_date = self.today
        d.status = "CONFIRMED" if d.confirmed else "ADVISED"
        self._event(d, "ADVICE", self.agent_id, "DONE", f"Credit advised to the beneficiary ({d.status.lower()}); MT730 sent")
        declined = t.confirmation_instructions != "WITHOUT" and not d.confirmed and (
            t.confirmation_instructions == "CONFIRM" or t.beneficiary_requests_confirmation)
        await self._notify_client(
            d, "Documentary credit advised in your favour",
            f"{t.issuing_bank_bic} has issued credit {t.issuing_bank_lc_ref} in your favour for {t.currency} {d.amount:,} "
            f"(our ref. {d.ref}), applicant {t.applicant.name}.\n"
            f"Expiry {t.expiry_date} in {t.expiry_place}; latest shipment {t.latest_shipment_date}; "
            f"{t.port_of_loading} -> {t.port_of_discharge}; available by {t.available_by.replace('_', ' ').lower()}.\n"
            f"Documents: {', '.join(r.doc_type.replace('_', ' ').lower() for r in t.documents_required)}.\n" +
            ("We have ADDED OUR CONFIRMATION: you are paid by us for complying documents regardless of the issuing bank."
             if d.confirmed else ("Our confirmation could not be added; the credit is advised without our engagement."
                                  if declined else "The credit is advised without our confirmation.")) +
            "\nPlease check the terms now: request amendments from your buyer before shipping if anything cannot be met.")

    def _examination_view(self, d: ExportLC, cust) -> LetterOfCredit:
        """The credit as an LC object for the shared UCP 600 examination rules."""
        t = d.terms
        lt = LCTerms(applicant_customer_id=d.customer_id, beneficiary=Party(name=cust.name, address=cust.address,
                                                                             country=cust.country),
                     advising_bank_bic=settings.bank_bic(), currency=t.currency, amount=d.amount,
                     tolerance_plus_pct=t.tolerance_plus_pct, tolerance_minus_pct=t.tolerance_minus_pct,
                     available_by=t.available_by, usance_days=t.usance_days, expiry_date=t.expiry_date,
                     expiry_place=t.expiry_place, latest_shipment_date=t.latest_shipment_date,
                     port_of_loading=t.port_of_loading, port_of_discharge=t.port_of_discharge, incoterms=t.incoterms,
                     goods_description=t.goods_description, partial_shipments_allowed=t.partial_shipments_allowed,
                     transhipment_allowed=t.transhipment_allowed, documents_required=t.documents_required,
                     presentation_period_days=t.presentation_period_days, additional_conditions=t.additional_conditions)
        return LetterOfCredit(ref=d.ref, customer_id=d.customer_id, status="ISSUED", currency=d.currency, amount=d.amount,
                              outstanding=d.outstanding, terms=lt, presentations=d.presentations)

    def _export_drawn(self, d: ExportLC, exclude: Optional[str] = None) -> Decimal:
        return sum((p.claimed_amount for p in d.presentations if p.presentation_id != exclude and p.status in
                    ("PAID", "ACCEPTED", "COMPLYING", "DISCREPANT_WAIVED")), D0)

    async def present_export_documents(self, ref: str, p: Presentation) -> ExportLC:
        async with self._unit_of_work():
            d = self.get_deal(ref)
            if not isinstance(d, ExportLC) or d.status not in ("ADVISED", "CONFIRMED", "PARTIALLY_DRAWN", "TRANSFERRED"):
                raise self.WorkflowError(f"{ref} cannot receive documents in status {getattr(d, 'status', '?')}")
            p.examination_deadline = add_banking_days(p.presentation_date, 5)
            d.presentations.append(p)
            step = f"EXAMINATION_{p.presentation_id}"
            self._event(d, f"PRESENTATION_{p.presentation_id}", "beneficiary", "DONE",
                        f"Beneficiary presented documents for {d.currency} {p.claimed_amount}; examination by "
                        f"{p.examination_deadline} (UCP 600 Art. 14(b))")
            t = d.terms
            disc = rules.examine_ucp600(self._examination_view(d, await self._customer(d.customer_id)), p, t.applicant.name, self._export_drawn(d, p.presentation_id))
            bl = next((x for x in p.documents if x.doc_type == "BILL_OF_LADING"), None)
            places = [str(bl.fields.get("port_of_loading", "")), str(bl.fields.get("port_of_discharge", ""))] if bl else []
            if not await self._step_sanctions(d, [{"name": t.applicant.name, "country": t.applicant.country, "role": "applicant"}],
                                              places, "", bl.fields.get("vessel_imo") if bl else None, None,
                                              step=f"SANCTIONS_{p.presentation_id}", resume=f"PRESENTATION:{p.presentation_id}"):
                return d
            p.discrepancies = disc
            if disc:
                p.status = "DISCREPANT_AWAITING_APPLICANT"  # here: awaiting the beneficiary's instruction
                p.applicant_decision_deadline = p.examination_deadline
                self._event(d, step, self.agent_id, "DONE", f"{len(disc)} discrepancies - beneficiary informed",
                            {"discrepancies": [x.model_dump() for x in disc]})
                await self._notify_client(
                    d, "Your documents are discrepant",
                    f"We examined your documents under {d.ref} ({t.currency} {p.claimed_amount:,}) and found:\n" +
                    "\n".join(f"- {x.description} [{x.rule}]" for x in disc) +
                    f"\n\nYou can present corrected documents (credit valid until {t.expiry_date}), or ask us to send the "
                    f"documents to {t.issuing_bank_bic} on approval (MT750) - payment then depends on the applicant's waiver.",
                    {"Send on approval": self.decision_link(d.ref, p.presentation_id, "send_on_approval"),
                     "I will correct the documents": self.decision_link(d.ref, p.presentation_id, "correct")})
                return d
            p.status = "COMPLYING"
            self._event(d, step, self.agent_id, "DONE", "Complying presentation (UCP 600 Art. 15(b)/(c))")
            if d.confirmed:
                await self._honour_export(d, p)
            else:
                await self._forward_for_honour(d, p, on_approval=False)
            return d

    async def _honour_export(self, d: ExportLC, p: Presentation):
        """Confirming bank pays the beneficiary and claims reimbursement from the issuing bank (UCP 600 Art. 8(a)/(c))."""
        t = d.terms
        step = f"HONOUR_{p.presentation_id}"
        if t.available_by in ("DEFERRED_PAYMENT", "ACCEPTANCE"):
            ship = rules.shipment_date(p) or p.presentation_date
            p.maturity_date = next_banking_day(ship + timedelta(days=t.usance_days))
            p.status = "ACCEPTED"
            await self._swift(d, step, "754", t.issuing_bank_bic,
                              swift.mt754(d, p, p.claimed_amount, p.maturity_date, settings.bank_bic(), t.issuing_bank_bic, True))
            d.status = "ACCEPTED_AWAITING_MATURITY"
            self._event(d, step, self.agent_id, "DONE", f"Deferred payment undertaking incurred, maturity {p.maturity_date}")
            await self._notify_client(d, "Documents accepted - we pay at maturity",
                                      f"Your documents under {d.ref} comply. As confirming bank we will pay {t.currency} "
                                      f"{p.claimed_amount:,} on {p.maturity_date}.")
            return
        await self._pay_beneficiary_confirmed(d, p)

    async def _pay_beneficiary_confirmed(self, d: ExportLC, p: Presentation):
        t = d.terms
        step = f"PAYMENT_{p.presentation_id}"
        value_date = next_banking_day(self.today)
        self._ensure_accounts(t.currency)
        eur = await self._credit_customer_from(d, t.currency, p.claimed_amount, f"GL_DUE_FROM_BANKS_{t.currency}",
                                               f"Proceeds export credit {d.ref}", step)
        await self._charge_fee(d, "PAYMENT_COMMISSION", max(self._tariff("export_payment_min"),
                                                           rules.money(eur * self._tariff("export_payment_rate_pct") / 100)))
        await self._book_contingent(d, "CONF", min(eur, self._contingent_open(d, "CONF")), reverse=True, step=step)
        p.status, p.paid_amount = "PAID", p.claimed_amount
        d.receivable_outstanding += p.claimed_amount
        d.outstanding = max(D0, d.outstanding - p.claimed_amount)
        await self._swift(d, step, "754", t.issuing_bank_bic,
                          swift.mt754(d, p, p.claimed_amount, value_date, settings.bank_bic(), t.issuing_bank_bic, True))
        if t.reimbursing_bank_bic:
            await self._swift(d, step, "742", t.reimbursing_bank_bic,
                              swift.mt742(d, p.claimed_amount, value_date, settings.bank_bic(), t.reimbursing_bank_bic))
        d.status = "PAID_AWAITING_REIMBURSEMENT"
        self._event(d, step, self.agent_id, "DONE", f"Beneficiary paid {t.currency} {p.claimed_amount} (EUR {eur}); "
                    f"reimbursement claimed from {t.reimbursing_bank_bic or t.issuing_bank_bic}")
        await self._notify_client(d, "Credit advice - export credit paid",
                                  f"We paid {t.currency} {p.claimed_amount:,} under {d.ref}: EUR {eur:,} credited to your "
                                  f"account, value {value_date}. Paid without recourse as confirming bank.")

    async def _forward_for_honour(self, d: ExportLC, p: Presentation, on_approval: bool):
        t = d.terms
        step = f"FORWARD_{p.presentation_id}"
        if on_approval:
            await self._swift(d, step, "750", t.issuing_bank_bic,
                              swift.mt750(d, p, p.discrepancies, settings.bank_bic(), t.issuing_bank_bic))
            p.status = "SENT_ON_APPROVAL"
        else:
            await self._swift(d, step, "754", t.issuing_bank_bic,
                              swift.mt754(d, p, p.claimed_amount, self.today, settings.bank_bic(), t.issuing_bank_bic, False))
            p.status = "SENT_FOR_HONOUR"
        d.status = "DOCUMENTS_WITH_ISSUING_BANK"
        await self._archive(d, "COVER_SCHEDULE", f"{d.ref}_{p.presentation_id}_cover.txt",
                            f"Documents under {t.issuing_bank_lc_ref} sent by courier to {t.issuing_bank_bic}"
                            + (" on approval" if on_approval else " for honour"))
        self._event(d, step, self.agent_id, "DONE", "Documents sent to the issuing bank by courier " +
                    ("on approval (MT750)" if on_approval else "for honour"))
        await self._notify_client(d, "Documents sent to the issuing bank",
                                  f"Your documents under {d.ref} were sent to {t.issuing_bank_bic}" +
                                  (" on approval. Payment depends on the applicant waiving the discrepancies."
                                   if on_approval else ". We credit you as soon as the issuing bank pays."))

    async def beneficiary_instruction(self, ref: str, presentation_id: str, send_on_approval: bool) -> ExportLC:
        async with self._unit_of_work():
            d = self.get_deal(ref)
            p = next((x for x in getattr(d, "presentations", []) if x.presentation_id == presentation_id), None)
            if not isinstance(d, ExportLC) or not p or p.status != "DISCREPANT_AWAITING_APPLICANT":
                raise self.WorkflowError("No discrepant presentation awaiting the beneficiary's instruction")
            if send_on_approval:
                await self._forward_for_honour(d, p, on_approval=True)
            else:
                p.status = "REFUSED"
                self._event(d, f"PRESENTATION_{presentation_id}", "beneficiary", "DONE",
                            "Beneficiary takes the documents back to correct them")
            return d

    async def issuing_bank_response(self, ref: str, presentation_id: str, outcome: str, amount: Optional[Decimal] = None,
                                    maturity_date: Optional[date] = None, reason: str = "") -> ExportLC:
        """Incoming MT756 / pacs.009 (paid), MT752 (authorised), MT732 (accepted) or MT734 (refused)."""
        async with self._unit_of_work():
            d = self.get_deal(ref)
            p = next((x for x in getattr(d, "presentations", []) if x.presentation_id == presentation_id), None)
            if not isinstance(d, ExportLC) or not p:
                raise self.WorkflowError("Unknown export credit presentation")
            t = d.terms
            step = f"ISSUER_{outcome}_{presentation_id}"
            if outcome == "PAID":
                amount = rules.money(amount if amount is not None else p.claimed_amount)
                await self._receive_issuer_funds(d, p, amount, step)
            elif outcome == "ACCEPTED":
                if p.status not in ("SENT_FOR_HONOUR", "SENT_ON_APPROVAL"):
                    raise self.WorkflowError("Presentation is not with the issuing bank")
                p.status, p.maturity_date = "ACCEPTED", maturity_date
                d.status = "ACCEPTED_AWAITING_MATURITY"
                self._event(d, step, t.issuing_bank_bic, "DONE", f"Issuing bank accepted / incurred deferred payment, maturity {maturity_date}")
                await self._notify_client(d, "Issuing bank accepted your documents",
                                          f"{t.issuing_bank_bic} will pay {t.currency} {p.claimed_amount:,} under {d.ref} on {maturity_date}.")
            elif outcome == "REFUSED":
                if p.status == "PAID":  # we paid as confirming bank - the issuing bank must reimburse complying documents
                    self._raise_exception(d, "LEGAL", f"{t.issuing_bank_bic} refused documents we honoured as confirming "
                                          f"bank: {reason or 'no reason'} - pursue reimbursement (UCP 600 Art. 7(c))",
                                          f"REIMBURSEMENT:{presentation_id}")
                    await self._notify_internal(d, "Reimbursement refused", reason)
                else:
                    p.status = "REFUSED"
                    d.status = "CONFIRMED" if d.confirmed else "ADVISED"
                    self._event(d, step, t.issuing_bank_bic, "DONE", f"Issuing bank refused the documents (MT734): {reason}")
                    await self._notify_client(d, "Documents refused by the issuing bank",
                                              f"{t.issuing_bank_bic} refused your documents under {d.ref}: {reason}. "
                                              f"They are held at your disposal; you may re-present within the credit's validity.")
            else:
                raise self.WorkflowError("outcome must be PAID, ACCEPTED or REFUSED")
            return d

    async def _receive_issuer_funds(self, d: ExportLC, p: Presentation, amount: Decimal, step: str):
        t = d.terms
        nostro = self._nostro(t.currency)
        if p.status == "PAID":  # reimbursement of what we already paid as confirming bank
            if amount > d.receivable_outstanding:
                raise self.WorkflowError(f"Reimbursement {amount} exceeds the claim outstanding {d.receivable_outstanding}")
            await self._post(d, step, [{"debit": nostro, "credit": f"GL_DUE_FROM_BANKS_{t.currency}", "amount": amount,
                                        "narrative": f"Reimbursement {t.issuing_bank_lc_ref} from {t.issuing_bank_bic}"}])
            d.receivable_outstanding -= amount
            release = await self._eur(d, amount * (1 + t.tolerance_plus_pct / 100), t.currency)
            await self._call(d, step, "tf_credit_limits", "release_limit", {"deal_ref": d.ref, "amount_eur": release},
                             "RELEASE_COUNTERPARTY_LIMIT")
            d.limit_utilisation_eur = max(D0, d.limit_utilisation_eur - release)
            self._event(d, step, t.issuing_bank_bic, "DONE", f"Reimbursement {t.currency} {amount} received")
        elif p.status in ("SENT_FOR_HONOUR", "SENT_ON_APPROVAL", "ACCEPTED"):
            self._ensure_accounts(t.currency)
            eur = await self._credit_customer_from(d, t.currency, amount, nostro, f"Proceeds export credit {d.ref}", step)
            await self._charge_fee(d, "PAYMENT_COMMISSION", max(self._tariff("export_payment_min"),
                                                               rules.money(eur * self._tariff("export_payment_rate_pct") / 100)))
            p.status, p.paid_amount = "PAID", amount
            d.outstanding = max(D0, d.outstanding - p.claimed_amount)
            if d.confirmed:  # confirmed but documents went on approval: our engagement for this drawing ends
                await self._book_contingent(d, "CONF", min(eur, self._contingent_open(d, "CONF")), reverse=True, step=step)
            self._event(d, step, t.issuing_bank_bic, "DONE", f"Issuing bank paid {t.currency} {amount}; beneficiary credited EUR {eur}")
            await self._notify_client(d, "Credit advice - export proceeds",
                                      f"{t.issuing_bank_bic} paid {t.currency} {amount:,} under {d.ref}: EUR {eur:,} credited to your account.")
        else:
            raise self.WorkflowError(f"No payment expected for presentation in status {p.status}")
        await self._settle_export_status(d)

    async def _settle_export_status(self, d: ExportLC):
        open_items = [x for x in d.presentations if x.status in ("SENT_FOR_HONOUR", "SENT_ON_APPROVAL", "ACCEPTED",
                                                                  "DISCREPANT_AWAITING_APPLICANT")]
        if d.receivable_outstanding > 0:
            d.status = "PAID_AWAITING_REIMBURSEMENT"
        elif open_items:
            d.status = "DOCUMENTS_WITH_ISSUING_BANK"
        elif d.outstanding <= 0 or not d.terms.partial_shipments_allowed:
            await self._release_all(d, "CONF", "Export credit fully utilised and settled - closed")
            d.status = "CLOSED"
        else:
            d.status = "PARTIALLY_DRAWN"

    async def transfer_export_lc(self, ref: str, second_beneficiary: Party, second_beneficiary_bank_bic: str,
                                 amount: Decimal, latest_shipment_date: Optional[date] = None,
                                 expiry_date: Optional[date] = None) -> ExportLC:
        async with self._unit_of_work():
            d = self.get_deal(ref)
            if not isinstance(d, ExportLC) or d.status not in ("ADVISED", "CONFIRMED", "PARTIALLY_DRAWN", "TRANSFERRED"):
                raise self.WorkflowError(f"{ref} cannot be transferred in status {getattr(d, 'status', '?')}")
            t = d.terms
            if not t.transferable:
                raise self.WorkflowError("The credit is not designated 'transferable' (UCP 600 Art. 38(b))")
            already = sum((Decimal(str(x["amount"])) for x in d.transfers), D0)
            amount = rules.money(amount)
            if amount <= 0 or already + amount > d.outstanding:
                raise self.WorkflowError(f"Transfer amount must be positive and within the available {d.outstanding - already}")
            exp = expiry_date or t.expiry_date
            ship = latest_shipment_date or t.latest_shipment_date
            if exp > t.expiry_date or ship > t.latest_shipment_date:
                raise self.WorkflowError("Transferred credit may only shorten expiry / shipment dates (UCP 600 Art. 38(g))")
            screen = [{"name": second_beneficiary.name, "country": second_beneficiary.country, "role": "second beneficiary"},
                      {"name": f"Bank {second_beneficiary_bank_bic}", "country": second_beneficiary_bank_bic[4:6], "role": "bank"}]
            n = len(d.transfers) + 1
            if not await self._step_sanctions(d, screen, [], t.goods_description, step=f"SANCTIONS_TRANSFER_{n}",
                                              resume="NONE"):
                return d
            tr = {"transfer_ref": f"{d.ref}T{n}", "second_beneficiary": second_beneficiary.model_dump(mode="json"),
                  "bank_bic": second_beneficiary_bank_bic, "amount": str(amount), "expiry_date": exp.isoformat(),
                  "latest_shipment_date": ship.isoformat(), "date": self.today.isoformat()}
            d.transfers.append(tr)
            await self._charge_fee(d, "TRANSFER_COMMISSION", self._tariff("transfer_flat"))
            await self._swift(d, f"TRANSFER_{n}", "720", second_beneficiary_bank_bic,
                              swift.mt720(d, {**tr, "expiry_date": exp, "latest_shipment_date": ship}, settings.bank_bic(),
                                          second_beneficiary_bank_bic, self.today))
            d.status = "TRANSFERRED"
            self._event(d, f"TRANSFER_{n}", self.agent_id, "DONE",
                        f"{t.currency} {amount} transferred to {second_beneficiary.name} (MT720); first beneficiary may "
                        f"substitute its invoice (UCP 600 Art. 38(h))")
            await self._notify_client(d, "Credit transferred", f"{t.currency} {amount:,} of {d.ref} was transferred to "
                                      f"{second_beneficiary.name}. You keep the right to substitute your own invoice.")
            return d

    # ================================================================== COLLECTIONS
    async def new_collection(self, terms: CollectionTerms, created_by: Optional[str] = None) -> Collection:
        async with self._unit_of_work():
            await self._customer(terms.customer_id)
            self._check_currency(terms.currency)
            if terms.amount <= 0:
                raise self.WorkflowError("Collection amount must be positive")
            if terms.release_against == "ACCEPTANCE" and terms.usance_days <= 0:
                raise self.WorkflowError("D/A collection needs a usance period for the bill of exchange")
            if not swift.is_valid_bic(terms.counterparty_bank_bic or ""):
                raise self.WorkflowError(f"Invalid bank BIC {terms.counterparty_bank_bic}")
            ref = self.state.next_ref("ODC" if terms.direction == "EXPORT" else "IDC")
            c = Collection(ref=ref, customer_id=terms.customer_id, status="RECEIVED", contingent_booked_eur=D0, currency=terms.currency,
                           amount=rules.money(terms.amount), outstanding=rules.money(terms.amount), terms=terms,
                           created_by=created_by)
            self.state.deals[ref] = c
            self._event(c, "RECEIVED", self.agent_id, "DONE",
                        f"{terms.direction.title()} collection {terms.release_against.lower()} received (URC 522)")
            await self._run_issuance(c, self._collection_pipeline)
            await self._acknowledge(c, "documentary collection")
            return c

    def _collection_fee(self, amount_eur: Decimal) -> Decimal:
        fee = rules.money(amount_eur * self._tariff("collection_rate_pct") / 100)
        return min(max(fee, self._tariff("collection_min")), self._tariff("collection_max"))

    async def _collection_pipeline(self, c: Collection):
        t = c.terms
        if not await self._step_kyc(c):
            return
        cust = await self._customer(c.customer_id)
        parties = [{"name": t.counterparty.name, "country": t.counterparty.country, "address": t.counterparty.address,
                    "role": "drawee" if t.direction == "EXPORT" else "drawer"},
                   {"name": f"Bank {t.counterparty_bank_bic}", "country": t.counterparty_bank_bic[4:6],
                    "role": "collecting bank" if t.direction == "EXPORT" else "remitting bank"},
                   {"name": cust.name, "country": cust.country, "role": "customer"}]
        if not await self._step_sanctions(c, parties, [t.counterparty.address], t.goods_description):
            return
        if self._done(c, "DISPATCH"):
            return
        docs = ", ".join(f"{r.originals}x {r.doc_type.replace('_', ' ').lower()}" for r in t.documents)
        if t.direction == "EXPORT":
            await self._charge_fee(c, "COLLECTION_COMMISSION", self._collection_fee(await self._eur(c, c.amount, t.currency)),
                                   once=True)
            instruction = (f"COLLECTION INSTRUCTION (URC 522) {c.ref}: please present the enclosed documents ({docs}) to "
                           f"{t.counterparty.name} and release them only against "
                           f"{'payment' if t.release_against == 'PAYMENT' else f'acceptance of a bill at {t.usance_days} days'} "
                           f"of {t.currency} {c.amount:,}. Charges for {t.charges_for.lower()}'s account. "
                           f"{'Protest if unpaid.' if t.protest_if_unpaid else 'Do not protest.'} Advise fate by SWIFT.")
            await self._archive(c, "COLLECTION_INSTRUCTION", f"{c.ref}_instruction.txt", instruction)
            c.status = "SENT_TO_COLLECTING_BANK"
            c.issue_date = self.today
            self._event(c, "DISPATCH", self.agent_id, "DONE",
                        f"Documents checked against the list (URC 522 Art. 12) and sent by courier to {t.counterparty_bank_bic}")
            await self._notify_client(c, "Your documents are on their way",
                                      f"Collection {c.ref} for {t.currency} {c.amount:,} on {t.counterparty.name} was sent to "
                                      f"{t.counterparty_bank_bic}. We credit you as soon as the proceeds arrive.")
        else:
            await self._swift(c, "DISPATCH", "410", t.counterparty_bank_bic,
                              swift.mt410(c, settings.bank_bic(), t.counterparty_bank_bic))
            c.response_deadline = add_banking_days(self.today, t.drawee_response_days)
            c.status = "PRESENTED_TO_DRAWEE"
            c.issue_date = self.today
            self._event(c, "DISPATCH", self.agent_id, "DONE",
                        f"Collection acknowledged (MT410); documents presented to the drawee, answer by {c.response_deadline}")
            what = "pay" if t.release_against == "PAYMENT" else f"accept a bill of exchange at {t.usance_days} days"
            actions = {"Pay now and receive the documents": self.decision_link(c.ref, c.ref, "collection_pay"),
                       "Refuse": self.decision_link(c.ref, c.ref, "collection_refuse")}
            if t.release_against == "ACCEPTANCE":
                actions = {"Accept the bill of exchange": self.decision_link(c.ref, c.ref, "collection_accept"),
                           "Refuse": self.decision_link(c.ref, c.ref, "collection_refuse")}
            await self._notify_client(
                c, "Documents arrived for you (documentary collection)",
                f"{t.counterparty.name} sent documents for {t.goods_description or 'your purchase'} through "
                f"{t.counterparty_bank_bic}: {docs}. To receive them you need to {what} for {t.currency} {c.amount:,}. "
                f"Please answer by {c.response_deadline}.", actions)

    async def collection_advice(self, ref: str, advice: str, amount: Optional[Decimal] = None,
                                maturity_date: Optional[date] = None, reason: str = "") -> Collection:
        """EXPORT collection: the collecting bank reports PAID (MT400), ACCEPTED (MT412) or UNPAID (MT416)."""
        async with self._unit_of_work():
            c = self.get_deal(ref)
            if not isinstance(c, Collection) or c.terms.direction != "EXPORT" or c.status not in (
                    "SENT_TO_COLLECTING_BANK", "ACCEPTED", "UNPAID"):
                raise self.WorkflowError(f"{ref} does not expect an advice in status {getattr(c, 'status', '?')}")
            t = c.terms
            if advice == "PAID":
                amount = rules.money(amount if amount is not None else c.amount)
                eur = await self._credit_customer_from(c, t.currency, amount, self._nostro(t.currency),
                                                       f"Collection proceeds {c.ref}", "PROCEEDS")
                c.paid_amount, c.outstanding, c.status = amount, D0, "PAID"
                self._event(c, "PROCEEDS", t.counterparty_bank_bic, "DONE", f"Proceeds {t.currency} {amount} received (MT400)")
                await self._notify_client(c, "Collection paid", f"{t.counterparty.name} paid collection {c.ref}: "
                                          f"EUR {eur:,} credited to your account.")
            elif advice == "ACCEPTED":
                c.status, c.maturity_date, c.accepted_on = "ACCEPTED", maturity_date, self.today
                self._event(c, "ACCEPTANCE", t.counterparty_bank_bic, "DONE", f"Bill accepted by the drawee, maturity {maturity_date} (MT412)")
                await self._notify_client(c, "Bill of exchange accepted", f"{t.counterparty.name} accepted the bill under "
                                          f"{c.ref}; payment is due on {maturity_date}.")
            elif advice == "UNPAID":
                c.status = "UNPAID"
                self._event(c, "NON_PAYMENT", t.counterparty_bank_bic, "DONE", f"Advice of non-payment / non-acceptance (MT416): {reason}")
                await self._notify_client(c, "Collection not paid", f"{t.counterparty.name} did not pay / accept under {c.ref}: "
                                          f"{reason}. Please instruct us: re-present, protest or have the documents returned.")
            else:
                raise self.WorkflowError("advice must be PAID, ACCEPTED or UNPAID")
            return c

    async def drawee_decision(self, ref: str, decision: str) -> Collection:
        """IMPORT collection: our client pays, accepts or refuses."""
        async with self._unit_of_work():
            c = self.get_deal(ref)
            if not isinstance(c, Collection) or c.terms.direction != "IMPORT" or c.status != "PRESENTED_TO_DRAWEE":
                raise self.WorkflowError(f"{ref} is not awaiting the drawee's decision")
            t = c.terms
            if decision == "pay":
                await self._pay_import_collection(c, "Drawee paid")
            elif decision == "accept":
                if t.release_against != "ACCEPTANCE":
                    raise self.WorkflowError("This collection is documents against payment - it must be paid")
                c.accepted_on = self.today
                c.maturity_date = next_banking_day(self.today + timedelta(days=t.usance_days))
                c.status = "ACCEPTED"
                await self._swift(c, "ACCEPTANCE", "412", t.counterparty_bank_bic,
                                  swift.mt412(c, c.maturity_date, settings.bank_bic(), t.counterparty_bank_bic))
                self._event(c, "ACCEPTANCE", "drawee", "DONE", f"Bill accepted, documents released; maturity {c.maturity_date}")
                await self._notify_client(c, "Documents released against your acceptance",
                                          f"You accepted the bill under {c.ref}. {t.currency} {c.amount:,} will be debited "
                                          f"on {c.maturity_date}.")
            elif decision == "refuse":
                await self._advise_import_unpaid(c, "Drawee refused")
            else:
                raise self.WorkflowError("decision must be pay, accept or refuse")
            return c

    async def _pay_import_collection(self, c: Collection, why: str):
        t = c.terms
        step = "PAYMENT"
        value_date = next_banking_day(self.today)
        fee = self._collection_fee(await self._eur(c, c.amount, t.currency)) if t.charges_for == "DRAWEE" else D0
        info = await self._debit_customer_for_payment(
            c, c.amount, t.currency, f"Documentary collection {c.ref}",
            [{"credit": self._nostro(t.currency), "amount": c.amount, "narrative": f"Proceeds {c.ref} to {t.counterparty_bank_bic}"}],
            step, fees_eur=fee)
        if t.currency != "EUR":
            await self._post(c, step, [{"debit": "GL_FX_POSITION_EUR", "credit": "GL_EQUITY_EUR", "amount": info["amount_eur"],
                                        "narrative": "FX deal EUR leg"},
                                       {"debit": f"GL_EQUITY_{t.currency}", "credit": f"GL_FX_POSITION_{t.currency}",
                                        "amount": c.amount, "narrative": f"FX deal {t.currency} leg"}])
        if fee:
            await self._charge_fee(c, "COLLECTION_COMMISSION", fee)
        corr = self.state.reference.correspondent_bic_by_currency.get(t.currency, t.counterparty_bank_bic)
        await self._pay_bank(c, step, f"{c.ref}P", t.counterparty_bank_ref or c.ref, value_date, t.currency, c.amount,
                             t.counterparty_bank_bic, corr)
        await self._swift(c, step, "400", t.counterparty_bank_bic,
                          swift.mt400(c, c.amount, value_date, settings.bank_bic(), t.counterparty_bank_bic))
        c.paid_amount, c.outstanding, c.status = c.amount, D0, "PAID"
        self._event(c, step, self.agent_id, "DONE", f"{why}: {t.currency} {c.amount} remitted to {t.counterparty_bank_bic} "
                    f"(MT400 + cover payment), documents released")
        await self._notify_client(c, "Debit advice - documentary collection paid",
                                  f"{t.currency} {c.amount:,} (EUR {info['amount_eur']:,}) debited under {c.ref}; the "
                                  f"documents are released to you.")

    async def _advise_import_unpaid(self, c: Collection, reason: str):
        t = c.terms
        await self._swift(c, "NON_PAYMENT", "416", t.counterparty_bank_bic,
                          swift.mt416(c, reason, settings.bank_bic(), t.counterparty_bank_bic))
        c.status = "UNPAID"
        self._event(c, "NON_PAYMENT", self.agent_id, "DONE", f"Advice of non-payment / non-acceptance sent (MT416): {reason}; "
                    f"documents held at the remitting bank's disposal (URC 522 Art. 26)")

    # ================================================================== end of day / exception hooks
    async def _eod_product(self, deal) -> List[str]:
        out: List[str] = []
        if isinstance(deal, ExportLC):
            for p in deal.presentations:
                if deal.confirmed and p.status == "ACCEPTED" and p.maturity_date and p.maturity_date <= self.today \
                        and deal.status == "ACCEPTED_AWAITING_MATURITY" and not p.paid_amount:
                    await self._pay_beneficiary_confirmed(deal, p)
                    out.append(f"{deal.ref}: deferred payment {p.presentation_id} paid to the beneficiary at maturity")
            if deal.status in ("ADVISED", "CONFIRMED", "PARTIALLY_DRAWN", "TRANSFERRED") and \
                    self.today > deal.terms.expiry_date + timedelta(days=30):
                if deal.confirmed or deal.limit_utilisation_eur:
                    await self._release_all(deal, "CONF", f"Export credit expired on {deal.terms.expiry_date}")
                deal.status = "EXPIRED"
                out.append(f"{deal.ref}: expired")
            elif deal.status in ("ADVISED", "CONFIRMED", "TRANSFERRED") and 0 <= (deal.terms.expiry_date - self.today).days <= 14:
                if await self._notify_client_once(deal, "EXPIRY_14D", "Your export credit expires soon",
                                                  f"Credit {deal.ref} expires on {deal.terms.expiry_date}. Present your "
                                                  f"documents in time or ask your buyer for an extension."):
                    out.append(f"{deal.ref}: expiry reminder sent")
        elif isinstance(deal, Collection):
            t = deal.terms
            if t.direction == "IMPORT" and deal.status == "ACCEPTED" and deal.maturity_date and deal.maturity_date <= self.today:
                await self._pay_import_collection(deal, "Accepted bill paid at maturity")
                out.append(f"{deal.ref}: accepted bill paid at maturity")
            elif t.direction == "IMPORT" and deal.status == "PRESENTED_TO_DRAWEE" and deal.response_deadline:
                if self.today > deal.response_deadline:
                    await self._advise_import_unpaid(deal, "No answer from the drawee")
                    await self._notify_client(deal, "Documents returned unpaid",
                                              f"Without your decision we advised non-payment under {deal.ref}.")
                    out.append(f"{deal.ref}: non-payment advised (no answer)")
                elif (deal.response_deadline - self.today).days <= 2:
                    if await self._notify_client_once(deal, "DRAWEE_REMINDER", "Reminder: documents waiting for you",
                                                      f"Please pay or accept under {deal.ref} by {deal.response_deadline}."):
                        out.append(f"{deal.ref}: drawee reminder sent")
            elif t.direction == "EXPORT" and deal.status == "ACCEPTED" and deal.maturity_date and \
                    self.today > deal.maturity_date + timedelta(days=5):
                if await self._notify_client_once(deal, "OVERDUE", "Accepted bill overdue",
                                                  f"The bill under {deal.ref} matured on {deal.maturity_date} and no proceeds "
                                                  f"have arrived yet. We are tracing the collecting bank."):
                    out.append(f"{deal.ref}: overdue tracer")
        return out

    async def _resume_product(self, deal, step: str) -> bool:
        if not isinstance(deal, (ExportLC, Collection)):
            return False
        if step == "ISSUE":
            deal.status = "RECEIVED"
            await self._run_issuance(deal, self._export_lc_pipeline if isinstance(deal, ExportLC) else self._collection_pipeline)
            return True
        if step.startswith("PRESENTATION:") and isinstance(deal, ExportLC):
            pid = step.split(":")[1]
            p = next(x for x in deal.presentations if x.presentation_id == pid)
            deal.status = "CONFIRMED" if deal.confirmed else "ADVISED"
            await self._reexamine_export(deal, p)  # compliance released the payment hold
            return True
        if step.startswith("REIMBURSEMENT:"):
            deal.status = "PAID_AWAITING_REIMBURSEMENT"
            self._event(deal, step, self.agent_id, "DONE", "Legal pursues reimbursement from the issuing bank")
            return True
        deal.status = "CONFIRMED" if getattr(deal, "confirmed", False) else ("ADVISED" if isinstance(deal, ExportLC) else deal.status)
        return True

    async def _reexamine_export(self, d: ExportLC, p: Presentation):
        t = d.terms
        disc = rules.examine_ucp600(self._examination_view(d, await self._customer(d.customer_id)), p, t.applicant.name, self._export_drawn(d, p.presentation_id))
        p.discrepancies = disc
        if disc:
            p.status = "DISCREPANT_AWAITING_APPLICANT"
            await self._notify_client(d, "Your documents are discrepant", "\n".join(f"- {x.description}" for x in disc),
                                      {"Send on approval": self.decision_link(d.ref, p.presentation_id, "send_on_approval"),
                                       "I will correct the documents": self.decision_link(d.ref, p.presentation_id, "correct")})
            return
        p.status = "COMPLYING"
        if d.confirmed:
            await self._honour_export(d, p)
        else:
            await self._forward_for_honour(d, p, on_approval=False)

    async def _reject_product(self, deal, step: str, exc) -> bool:
        if not isinstance(deal, (ExportLC, Collection)):
            return False
        if isinstance(deal, ExportLC) and step == "ISSUE" and exc.category == "CREDIT":
            # credit declines the confirmation: the credit is still advised, without our engagement
            self._event(deal, "CONFIRMATION", exc.resolved_by or self.agent_id, "REJECTED",
                        "Confirmation declined by credit - credit advised without our confirmation")
            await self._advise_export_lc(deal)
            return True
        if step == "ISSUE":
            deal.status = "REJECTED"
            if isinstance(deal, Collection) and deal.terms.direction == "IMPORT":
                await self._advise_import_unpaid(deal, "Collection cannot be handled by us (compliance)")
                deal.status = "REJECTED"
            await self._notify_client(deal, "We cannot handle this transaction",
                                      f"We are unable to proceed with {deal.ref}. Your relationship manager will contact you.")
            return True
        if step.startswith("PRESENTATION:") and isinstance(deal, ExportLC):
            pid = step.split(":")[1]
            p = next(x for x in deal.presentations if x.presentation_id == pid)
            p.status = "REFUSED"
            deal.status = "BLOCKED_SANCTIONS"
            self._event(deal, step, self.agent_id, "DONE", "Documents blocked for sanctions reasons")
            return True
        if step.startswith("REIMBURSEMENT:"):
            deal.status = "PAID_AWAITING_REIMBURSEMENT"
            self._event(deal, step, exc.resolved_by or self.agent_id, "DONE", "Legal: claim on the issuing bank written off / "
                        "pursued outside the system")
            return True
        return True
