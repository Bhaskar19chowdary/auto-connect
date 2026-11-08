"""
Two web portals on top of the trade finance engine:

* Client portal (/portal): a company applies for a letter of credit, guarantee, surety or collection, uploads supporting
  documents, follows its transactions in plain language and takes the decisions the bank asks it for.
* Bank workbench (/bank): the trade finance employee sees today's work, open decisions with a plain explanation of
  each choice, every application and transaction as a story, customers and their limits.

Every application is saved in the database before it is processed (see TradeFinanceEngine.submit_application).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import ValidationError

from .. import settings
from . import plain
from .engine import TradeFinanceEngine, WorkflowError
from .state import to_jsonable
from .systems import BusError

FINISHED = {"CLOSED", "EXPIRED", "REJECTED", "BLOCKED_SANCTIONS", "PAID", "UNPAID", "RETURNED_FOR_CORRECTION"}
DEMO_STAFF = [
    {"name": "Anna Weber", "roles": ["OPERATIONS"], "job": "Trade finance officer"},
    {"name": "Markus Huber", "roles": ["DOC_CHECKER"], "job": "Document checker"},
    {"name": "Julia Mueller", "roles": ["COMPLIANCE"], "job": "Compliance officer"},
    {"name": "Thomas Schmid", "roles": ["COMPLIANCE"], "job": "Compliance officer (second approver)"},
    {"name": "Sabine Koch", "roles": ["CREDIT"], "job": "Credit risk manager"},
    {"name": "Lukas Bauer", "roles": ["CREDIT"], "job": "Credit risk manager (second approver)"},
    {"name": "Dr. Eva Fischer", "roles": ["LEGAL"], "job": "Legal counsel"},
]


def _template(name: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _form_errors(e: ValidationError) -> List[Dict[str, str]]:
    out = []
    for err in e.errors():
        field = ".".join(str(x) for x in err.get("loc", ()) if x != "__root__")
        msg = err.get("msg", "invalid")
        if err.get("type") == "missing":
            msg = "This field is required"
        out.append({"field": field, "message": msg.replace("Value error, ", "")})
    return out


def _status_for(status: str, audience: str) -> str:
    """Internal hold reasons (sanctions, KYC, credit...) are never shown to a client: no tipping-off."""
    return "UNDER_REVIEW" if audience == "client" and status.startswith("ON_HOLD_") else status


def mount_portals(app: FastAPI, engine: TradeFinanceEngine) -> None:
    from ..security import production

    def users_enabled() -> bool:
        users = getattr(app.state, "users", None)
        return bool(users and users.enabled)

    def demo_allowed() -> bool:
        return not users_enabled() and not production()

    def eur(amount: Decimal, currency: str) -> Decimal:
        rate = engine.state.reference.fx_eur_per_unit.get(currency)
        return (Decimal(amount) * Decimal(rate)) if rate else Decimal(amount)

    def money(v) -> float:
        return float(Decimal(str(v)).quantize(Decimal("0.01")))

    def customer_name(cid: str) -> str:
        c = engine.state.customers.get(cid)
        return c.name if c else cid

    def currencies() -> List[str]:
        ref = engine.state.reference
        return sorted(c for c in ref.fx_eur_per_unit if c in ref.nostro_by_currency)

    async def decoded_files(request: Request, files) -> List[Tuple[str, bytes]]:
        from ..document_reader import DocumentReader

        if not files:
            return []
        decoded = DocumentReader.decode(files)
        limit = float(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024
        too_big = [n for n, data in decoded if len(data) > limit]
        if too_big:
            raise HTTPException(413, f"File(s) larger than {limit / 1024 / 1024:.0f} MB: {', '.join(too_big)}")
        return decoded

    async def submit(customer_id: str, payload: Dict[str, Any], submitted_by: str, channel: str, request: Request):
        product = (payload.get("product") or "").upper()
        if product not in ("LC", "GUARANTEE", "SURETY", "COLLECTION"):
            raise HTTPException(422, {"message": "Choose a product", "errors": [{"field": "product", "message": "required"}]})
        files = await decoded_files(request, payload.get("files"))
        try:
            app_rec = await engine.submit_application(customer_id, product, payload.get("details") or {}, files,
                                                      submitted_by=submitted_by, channel=channel)
        except ValidationError as e:
            raise HTTPException(422, {"message": "Please correct the highlighted fields", "errors": _form_errors(e)})
        except (TypeError, ValueError) as e:
            raise HTTPException(422, {"message": str(e), "errors": []})
        except WorkflowError as e:
            raise HTTPException(409, {"message": str(e), "errors": []})
        except BusError as e:
            raise HTTPException(502, {"message": f"{e.system or 'A bank system'} is not available - please try again",
                                      "errors": []})
        return application_view(app_rec, "client" if channel == "CLIENT_PORTAL" else "staff")

    def application_view(a, audience: str = "staff") -> Dict[str, Any]:
        label, tone, text = plain.APPLICATION_STATUS[a.status]
        deal = engine.state.deals.get(a.deal_ref) if a.deal_ref else None
        d = a.details
        party = d.get("beneficiary") or d.get("obligee") or d.get("counterparty") or {}
        out = {"application_id": a.application_id, "product": a.product,
               "product_name": plain.PRODUCT_NAMES[a.product], "customer_id": a.customer_id,
               "customer_name": customer_name(a.customer_id), "channel": a.channel, "submitted_by": a.submitted_by,
               "submitted_at": a.submitted_at, "status": a.status, "status_label": label, "tone": tone,
               "status_text": text, "message": a.message if (audience == "staff" or a.status != "REJECTED")
               else "We could not accept this application. Your relationship manager will contact you.",
               "deal_ref": a.deal_ref, "amount": d.get("amount"), "currency": d.get("currency"),
               "counterparty": party.get("name"), "attachments": a.attachments,
               "history": [{"at": h.get("at"), "text": h.get("text")} for h in a.history],
               "details": d, "can_edit": a.status in engine.EDITABLE_APPLICATION, "problems": a.problems}
        if audience == "client" and a.status == "REJECTED" and "Cannot be processed" in a.message:
            out["message"] = a.message  # validation problems the client can fix himself
        if deal is not None:
            sv = plain.status_view(deal.status, audience)
            out.update(deal_status=_status_for(deal.status, audience), deal_status_label=sv["label"], deal_tone=sv["tone"],
                       deal_status_text=sv["text"])
        return out

    def deal_row(d, audience: str = "staff") -> Dict[str, Any]:
        sv = plain.status_view(d.status, audience)
        cp = plain.counterparty(d) or {}
        acts = plain.client_actions(d)
        waiting = None
        if audience == "staff":
            if d.open_exceptions:
                ex = engine.state.exceptions.get(d.open_exceptions[0])
                waiting = f"Decision by {plain.DECISIONS.get(ex.category, {}).get('who', ex.category)}" if ex else None
            elif acts:
                waiting = "Waiting for the client"
        return {"ref": d.ref, "product": d.product, "product_name": plain.PRODUCT_NAMES.get(d.product, d.product),
                "customer_id": d.customer_id, "customer_name": customer_name(d.customer_id),
                "counterparty": cp.get("name"), "counterparty_country": cp.get("country"),
                "amount": money(d.amount), "outstanding": money(d.outstanding), "currency": d.currency,
                "status": _status_for(d.status, audience), "status_label": sv["label"], "tone": sv["tone"],
                "status_text": sv["text"],
                "created_at": d.created_at, "issue_date": d.issue_date.isoformat() if d.issue_date else None,
                "expiry_date": getattr(d.terms, "expiry_date", None) and d.terms.expiry_date.isoformat(),
                "actions_needed": len(acts), "waiting_for": waiting, "application_id": d.application_id,
                "finished": d.status in FINISHED}

    def deal_detail(d, audience: str) -> Dict[str, Any]:
        out = deal_row(d, audience)
        out.update(change=plain.change_options(d),
                   facts=plain.key_facts(d), progress=plain.progress(d), story=plain.story(d, audience),
                   actions=plain.client_actions(d), product_help=plain.PRODUCT_HELP.get(d.product, ""),
                   documents=[{**a, "item": a.get("item_id")} for a in d.attachments],
                   fees=[{"date": f.date.isoformat(), "fee": plain.FEE_NAMES.get(f.fee_type, f.fee_type.replace("_", " ").capitalize()),
                          "amount": money(f.amount), "currency": f.currency} for f in d.fees],
                   messages=[{"date": n.get("date"), "subject": n.get("subject"), "body": n.get("body", "")}
                             for n in engine.state.notifications if n.get("deal_ref") == d.ref and n.get("subject")])
        if audience == "staff":
            out["decisions"] = [exception_view(engine.state.exceptions[x]) for x in d.open_exceptions
                                if x in engine.state.exceptions]
            out["past_decisions"] = [exception_view(e) for e in engine.state.exceptions.values()
                                     if e.deal_ref == d.ref and e.status != "OPEN"]
            out["swift"] = [{"message_id": m.message_id, "type": m.mt_type, "meaning": plain.SWIFT_MEANING.get(m.mt_type, "Trade message"),
                             "to": m.receiver_bic, "status": m.status, "created_at": m.created_at}
                            for m in (engine.state.swift_messages.get(i) for i in d.swift_message_ids) if m]
            out["bookings"] = [{"date": e.booking_date.isoformat(), "debit": e.debit_account, "credit": e.credit_account,
                                "amount": money(e.amount), "currency": e.currency, "text": e.narrative}
                               for e in engine.state.ledger if e.deal_ref == d.ref]
            out["created_by"] = d.created_by
            c = engine.state.customers.get(d.customer_id)
            out["customer"] = {"name": c.name, "kyc": c.kyc_status, "risk": c.risk_rating, "email": c.email} if c else None
        return out

    def exception_view(e) -> Dict[str, Any]:
        from ..security import APPROVALS_REQUIRED, EXCEPTION_ROLE

        info = plain.DECISIONS.get(e.category, {})
        d = engine.state.deals.get(e.deal_ref)
        return {"exception_id": e.exception_id, "deal_ref": e.deal_ref, "category": e.category,
                "title": info.get("title", e.category), "who": info.get("who", e.category),
                "role": EXCEPTION_ROLE.get(e.category, "OPERATIONS"), "explain": info.get("explain", ""),
                "reason": e.reason, "approve_label": info.get("approve", "Approve"),
                "reject_label": info.get("reject", "Reject"), "if_approve": info.get("if_approve", ""),
                "if_reject": info.get("if_reject", ""), "approvals_needed": APPROVALS_REQUIRED.get(e.category, 1),
                "can_return": engine.can_return_to_client(e), "return_label": info.get("return", ""),
                "if_return": info.get("if_return", ""),
                "approvers": e.approvers, "status": e.status, "created_at": e.created_at, "resolved_by": e.resolved_by,
                "note": e.resolution_note,
                "customer_name": customer_name(d.customer_id) if d else None,
                "product_name": plain.PRODUCT_NAMES.get(d.product) if d else None,
                "amount": money(d.amount) if d else None, "currency": d.currency if d else None,
                "counterparty": (plain.counterparty(d) or {}).get("name") if d else None}

    # ================================================================== client portal
    def client(request: Request) -> Tuple[str, str]:
        user = getattr(request.state, "user", None)
        if user:
            if "CLIENT" not in user.get("roles", []) or not user.get("customer_id"):
                raise HTTPException(403, "This portal is for client users")
            return user["customer_id"], user["user_id"]
        if not demo_allowed():
            raise HTTPException(401, "Sign in with your personal access key")
        cid = request.headers.get("X-Customer-Id", "")
        if cid not in engine.state.customers:
            raise HTTPException(401, "Choose your company to sign in")
        return cid, f"client:{cid}"

    def own_deal(cid: str, ref: str):
        d = engine.state.deals.get(ref)
        if not d or d.customer_id != cid:
            raise HTTPException(404, "Transaction not found")
        return d

    @app.get("/portal/login-info")
    async def portal_login_info():
        return {"bank_name": settings.bank_name(), "demo": demo_allowed(),
                "customers": [{"customer_id": c.customer_id, "name": c.name, "city": c.address.split(",")[-1].strip()}
                              for c in engine.state.customers.values()] if demo_allowed() else []}

    @app.get("/api/v1/portal/me")
    async def portal_me(request: Request):
        cid, uid = client(request)
        c = engine.state.customers[cid]
        lines = []
        for f in engine.state.facilities.values():
            if f.customer_id == cid:
                lines.append({"product": f.product, "name": {"LC": "Letters of credit", "GUARANTEE": "Guarantees",
                                                             "SURETY": "Surety bonds",
                                                             "WORKING_CAPITAL": "General credit line"}.get(f.product, f.product),
                              "limit": money(f.limit), "used": money(f.utilised), "available": money(f.available),
                              "currency": f.currency})
        return {"customer_id": cid, "user": uid, "name": c.name, "address": c.address, "country": c.country,
                "email": c.email, "account": c.current_account_iban, "credit_lines": lines,
                "business_date": engine.today.isoformat(), "currencies": currencies(), "bank_name": settings.bank_name()}

    @app.get("/api/v1/portal/overview")
    async def portal_overview(request: Request):
        cid, _ = client(request)
        deals = [d for d in engine.state.deals.values() if d.customer_id == cid]
        actions = []
        for d in deals:
            for a in plain.client_actions(d):
                actions.append({**a, "ref": d.ref, "product_name": plain.PRODUCT_NAMES.get(d.product, d.product)})
        updates = []
        for d in deals:
            for s in plain.story(d, "client"):
                updates.append({**s, "ref": d.ref, "product_name": plain.PRODUCT_NAMES.get(d.product, d.product)})
        updates.sort(key=lambda x: x["at"], reverse=True)
        apps = sorted((a for a in engine.state.applications.values() if a.customer_id == cid),
                      key=lambda a: a.submitted_at, reverse=True)
        active = [d for d in deals if d.status not in FINISHED]
        return {"actions": actions, "updates": updates[:10],
                "counts": {"active": len(active), "under_review": sum(d.status.startswith("ON_HOLD") for d in deals),
                           "completed": len(deals) - len(active), "applications": len(apps)},
                "open_amount_eur": money(sum((eur(d.outstanding, d.currency) for d in active), Decimal("0"))),
                "applications": [application_view(a, "client") for a in apps[:5]]}

    @app.get("/api/v1/portal/deals")
    async def portal_deals(request: Request):
        cid, _ = client(request)
        rows = [deal_row(d, "client") for d in engine.state.deals.values() if d.customer_id == cid]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    @app.get("/api/v1/portal/deals/{ref}")
    async def portal_deal(ref: str, request: Request):
        cid, _ = client(request)
        return to_jsonable(deal_detail(own_deal(cid, ref), "client"))

    @app.post("/api/v1/portal/deals/{ref}/decisions")
    async def portal_decision(ref: str, request: Request, payload: Dict[str, Any] = Body(...)):
        cid, uid = client(request)
        d = own_deal(cid, ref)
        item, decision = payload.get("item_id", ""), payload.get("decision", "")
        allowed = {(a["item_id"], o["decision"]) for a in plain.client_actions(d) for o in a["options"]}
        if (item, decision) not in allowed:
            raise HTTPException(409, "This decision is no longer open")
        try:
            if decision in ("waive", "refuse"):
                await engine.applicant_waiver_decision(ref, item, decision == "waive")
            elif decision in ("extend", "pay") and d.product == "GUARANTEE":
                await engine.extend_or_pay_decision(ref, item, decision == "extend")
            elif decision in ("send_on_approval", "correct"):
                await engine.beneficiary_instruction(ref, item, decision == "send_on_approval")
            elif decision in ("acknowledge", "dispute"):
                reason = (payload.get("reason") or "").strip()
                if decision == "dispute" and not reason:
                    raise HTTPException(422, "Please give a reason for the dispute")
                await engine.principal_claim_response(ref, item, decision == "dispute", reason)
            else:
                await engine.drawee_decision(ref, decision)
        except WorkflowError as e:
            raise HTTPException(409, str(e))
        except BusError as e:
            raise HTTPException(502, f"{e.system or 'A bank system'} is not available - please try again")
        return to_jsonable(deal_detail(engine.state.deals[ref], "client"))

    @app.get("/api/v1/portal/applications")
    async def portal_applications(request: Request):
        cid, _ = client(request)
        apps = sorted((a for a in engine.state.applications.values() if a.customer_id == cid),
                      key=lambda a: a.submitted_at, reverse=True)
        return to_jsonable([application_view(a, "client") for a in apps])

    @app.post("/api/v1/portal/applications")
    async def portal_apply(request: Request, payload: Dict[str, Any] = Body(...)):
        cid, uid = client(request)
        return to_jsonable(await submit(cid, payload, uid, "CLIENT_PORTAL", request))

    @app.get("/api/v1/portal/applications/{application_id}")
    async def portal_application(application_id: str, request: Request):
        cid, _ = client(request)
        a = engine.state.applications.get(application_id)
        if not a or a.customer_id != cid:
            raise HTTPException(404, "Application not found")
        return to_jsonable(application_view(a, "client"))

    @app.post("/api/v1/portal/applications/{application_id}/resubmit")
    async def portal_resubmit(application_id: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """Corrects an application that did not become a transaction yet and sends it again."""
        cid, uid = client(request)
        a = engine.state.applications.get(application_id)
        if not a or a.customer_id != cid:
            raise HTTPException(404, "Application not found")
        files = await decoded_files(request, payload.get("files"))
        try:
            a = await engine.edit_application(application_id, cid, payload.get("details") or {}, files, by=uid)
        except ValidationError as e:
            raise HTTPException(422, {"message": "Please correct the highlighted fields", "errors": _form_errors(e)})
        except (TypeError, ValueError) as e:
            raise HTTPException(422, {"message": str(e), "errors": []})
        except WorkflowError as e:
            raise HTTPException(409, {"message": str(e), "errors": []})
        return to_jsonable(application_view(a, "client"))

    @app.post("/api/v1/portal/applications/{application_id}/withdraw")
    async def portal_withdraw(application_id: str, request: Request):
        cid, uid = client(request)
        try:
            return to_jsonable(application_view(await engine.withdraw_application(application_id, cid, by=uid), "client"))
        except WorkflowError as e:
            raise HTTPException(409 if application_id in engine.state.applications else 404, str(e))

    @app.post("/api/v1/portal/deals/{ref}/changes")
    async def portal_change(ref: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """The client asks to change an issued transaction (formal amendment): new total amount, new expiry date and
        - for a letter of credit - a new latest shipment date. Limits, fees and SWIFT are handled as usual."""
        from datetime import date as _date

        cid, uid = client(request)
        d = own_deal(cid, ref)
        opts = plain.change_options(d)
        if not opts["can_change"]:
            raise HTTPException(409, opts["why_not"])
        changes: Dict[str, Any] = {}
        try:
            if payload.get("amount") not in (None, ""):
                new_amount = Decimal(str(payload["amount"]).replace(",", "").strip())
                if new_amount <= 0:
                    raise ValueError("The new amount must be more than zero")
                if new_amount != d.amount:
                    changes["amount_delta"] = str(new_amount - d.amount)
            for k in ("expiry_date", "latest_shipment_date"):
                v = payload.get(k)
                if v and (k != "latest_shipment_date" or d.product == "LC"):
                    _date.fromisoformat(v)
                    if v != opts["current"].get(k):
                        changes[k] = v
        except (ArithmeticError, ValueError) as e:
            raise HTTPException(422, f"Please check the values: {e}")
        if not changes:
            raise HTTPException(422, "Nothing was changed - enter a new amount or date")
        if payload.get("reason"):
            changes["narrative"] = str(payload["reason"])[:300]
        try:
            if d.product == "LC":
                await engine.amend_lc(ref, changes)
            elif d.product == "GUARANTEE":
                await engine.amend_guarantee(ref, changes)
            else:
                await engine.amend_surety(ref, changes)
        except WorkflowError as e:
            raise HTTPException(409, str(e))
        except BusError as e:
            raise HTTPException(502, f"{e.system or 'A bank system'} is not available - please try again")
        return to_jsonable(deal_detail(engine.state.deals[ref], "client"))

    @app.get("/api/v1/portal/messages")
    async def portal_messages(request: Request):
        cid, _ = client(request)
        own = {d.ref for d in engine.state.deals.values() if d.customer_id == cid}
        msgs = [{"date": n.get("date"), "ref": n.get("deal_ref"), "subject": n.get("subject"), "body": n.get("body", "")}
                for n in engine.state.notifications
                if n.get("subject") and (n.get("customer_id") == cid or n.get("deal_ref") in own)]
        return list(reversed(msgs))[:100]

    def send_file(meta, data):
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise HTTPException(500, "Integrity check failed: the stored file does not match its fingerprint")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in meta["filename"])
        return Response(content=data, media_type=meta["mime"] or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{safe}"', "X-SHA256": meta["sha256"]})

    @app.get("/api/v1/portal/files/{file_id}")
    async def portal_file(file_id: str, request: Request):
        cid, _ = client(request)
        meta, data = engine.store.load_file(file_id)
        if meta is None:
            raise HTTPException(404, "Unknown file")
        owner = engine.state.deals.get(meta["deal_ref"]) or engine.state.applications.get(meta["deal_ref"])
        if owner is None or owner.customer_id != cid:
            raise HTTPException(404, "Unknown file")
        return send_file(meta, data)

    @app.get("/portal", response_class=HTMLResponse)
    async def portal_page():
        return HTMLResponse(_template("client_portal.html"))

    # ================================================================== bank workbench (employees)
    def staff(request: Request) -> Dict[str, Any]:
        user = getattr(request.state, "user", None)
        if user:
            if "CLIENT" in user.get("roles", []):
                raise HTTPException(403, "Client users cannot open the bank workbench")
            return {"id": user["user_id"], "name": user.get("name") or user["user_id"], "roles": user["roles"]}
        name = (request.headers.get("X-Staff-Name") or "").strip()
        return {"id": name or "bank employee", "name": name or "Bank employee", "roles": ["DEMO"]}

    @app.get("/bank/login-info")
    async def bank_login_info():
        return {"bank_name": settings.bank_name(), "demo": demo_allowed(), "demo_staff": DEMO_STAFF if demo_allowed() else []}

    @app.get("/api/v1/staff/me")
    async def staff_me(request: Request):
        return {**staff(request), "business_date": engine.today.isoformat(), "bank_name": settings.bank_name(),
                "currencies": currencies(), "demo": demo_allowed()}

    @app.get("/api/v1/staff/overview")
    async def staff_overview(request: Request):
        staff(request)
        st = engine.state
        deals = list(st.deals.values())
        active = [d for d in deals if d.status not in FINISHED]
        today = datetime.now(timezone.utc).date().isoformat()
        by_product: Dict[str, Dict[str, Any]] = {}
        for d in active:
            p = by_product.setdefault(d.product, {"product": d.product, "name": plain.PRODUCT_NAMES.get(d.product, d.product),
                                                  "count": 0, "open_eur": Decimal("0")})
            p["count"] += 1
            p["open_eur"] += eur(d.outstanding, d.currency)
        activity = []
        for d in deals:
            for s in plain.story(d, "staff"):
                if not s["technical"]:
                    activity.append({**s, "ref": d.ref, "customer_name": customer_name(d.customer_id)})
        activity.sort(key=lambda x: x["at"], reverse=True)
        failures = sum(1 for d in active for e in d.history[-30:] if plain.is_outage(e))
        return to_jsonable({
            "business_date": engine.today.isoformat(),
            "counts": {"decisions": sum(1 for e in st.exceptions.values() if e.status == "OPEN"),
                       "waiting_for_client": sum(len(plain.client_actions(d)) for d in active),
                       "applications_today": sum(1 for a in st.applications.values() if a.submitted_at[:10] == today),
                       "applications_waiting": sum(1 for a in st.applications.values() if a.status == "WAITING_FOR_SYSTEM"),
                       "active": len(active), "completed": len(deals) - len(active)},
            "open_eur": money(sum((p["open_eur"] for p in by_product.values()), Decimal("0"))),
            "by_product": [{**p, "open_eur": money(p["open_eur"])} for p in by_product.values()],
            "books_balanced": st.ledger_balanced(),
            "outbox_failed": sum(1 for i in st.outbox.values() if i.get("status") == "FAILED"),
            "system_problems": failures,
            "activity": activity[:15]})

    @app.get("/api/v1/staff/tasks")
    async def staff_tasks(request: Request):
        staff(request)
        st = engine.state
        decisions = [exception_view(e) for e in st.exceptions.values() if e.status == "OPEN"]
        decisions.sort(key=lambda x: x["created_at"])
        waiting = [application_view(a) for a in st.applications.values() if a.status == "WAITING_FOR_SYSTEM"]
        client_wait = []
        for d in st.deals.values():
            for a in plain.client_actions(d):
                client_wait.append({"ref": d.ref, "customer_name": customer_name(d.customer_id),
                                    "product_name": plain.PRODUCT_NAMES.get(d.product), "title": a["title"],
                                    "deadline": a["deadline"]})
        return to_jsonable({"decisions": decisions, "applications_waiting": waiting, "waiting_for_client": client_wait})

    @app.get("/api/v1/staff/applications")
    async def staff_applications(request: Request):
        staff(request)
        apps = sorted(engine.state.applications.values(), key=lambda a: a.submitted_at, reverse=True)
        return to_jsonable([application_view(a) for a in apps])

    @app.post("/api/v1/staff/applications")
    async def staff_apply(request: Request, payload: Dict[str, Any] = Body(...)):
        who = staff(request)
        cid = payload.get("customer_id") or ""
        if cid not in engine.state.customers:
            raise HTTPException(422, {"message": "Choose the customer", "errors": [{"field": "customer_id", "message": "required"}]})
        return to_jsonable(await submit(cid, payload, who["id"], "BANK_STAFF", request))

    @app.post("/api/v1/staff/applications/{application_id}/retry")
    async def staff_retry(application_id: str, request: Request):
        who = staff(request)
        try:
            return to_jsonable(application_view(await engine.process_application(application_id, by=who["id"])))
        except WorkflowError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/v1/staff/exceptions/{exception_id}/return-to-client")
    async def staff_return(exception_id: str, request: Request, payload: Dict[str, Any] = Body(default={})):
        """Sends an application with client mistakes (e.g. a wrong BIC) back to the client to correct."""
        who = staff(request)
        try:
            a = await engine.return_to_client(exception_id, who["id"], (payload.get("note") or "").strip())
        except WorkflowError as e:
            raise HTTPException(409, str(e))
        return to_jsonable(application_view(a))

    @app.post("/api/v1/staff/deals/{ref}/presentations/{presentation_id}/re-examine")
    async def staff_reexamine(ref: str, presentation_id: str, request: Request):
        who = staff(request)
        try:
            await engine.reexamine_presentation(ref, presentation_id, who["id"])
        except WorkflowError as e:
            raise HTTPException(409, str(e))
        except BusError as e:
            raise HTTPException(502, f"{e.system or 'A bank system'} is not available - please try again")
        d = engine.state.deals[ref]
        return to_jsonable(deal_detail(d, "staff"))

    @app.get("/api/v1/staff/deals")
    async def staff_deals(request: Request, q: str = "", view: str = "all"):
        staff(request)
        ql = q.strip().lower()
        rows = []
        for d in engine.state.deals.values():
            r = deal_row(d)
            if ql and ql not in " ".join(str(r.get(k) or "") for k in ("ref", "customer_name", "counterparty",
                                                                        "product_name", "status_label")).lower():
                continue
            if view == "active" and r["finished"] or view == "attention" and not (r["waiting_for"]) \
                    or view == "finished" and not r["finished"]:
                continue
            rows.append(r)
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    @app.get("/api/v1/staff/deals/{ref}")
    async def staff_deal(ref: str, request: Request):
        staff(request)
        d = engine.state.deals.get(ref)
        if not d:
            raise HTTPException(404, "Transaction not found")
        out = deal_detail(d, "staff")
        if d.application_id and d.application_id in engine.state.applications:
            out["application"] = application_view(engine.state.applications[d.application_id])
        return to_jsonable(out)

    @app.get("/api/v1/staff/swift/{message_id}", response_class=PlainTextResponse)
    async def staff_swift(message_id: str, request: Request):
        staff(request)
        m = engine.state.swift_messages.get(message_id)
        if not m:
            raise HTTPException(404, "Unknown message")
        return m.text

    @app.get("/api/v1/staff/customers")
    async def staff_customers(request: Request):
        staff(request)
        out = []
        for c in engine.state.customers.values():
            deals = [d for d in engine.state.deals.values() if d.customer_id == c.customer_id]
            out.append({"customer_id": c.customer_id, "name": c.name, "address": c.address, "country": c.country,
                        "kyc": c.kyc_status, "kyc_next_review": c.kyc_next_review.isoformat(), "risk": c.risk_rating,
                        "email": c.email, "active_deals": sum(d.status not in FINISHED for d in deals),
                        "all_deals": len(deals),
                        "credit_lines": [{"product": f.product, "limit": money(f.limit), "used": money(f.utilised),
                                          "available": money(f.available), "currency": f.currency}
                                         for f in engine.state.facilities.values() if f.customer_id == c.customer_id]})
        return out

    @app.get("/bank", response_class=HTMLResponse)
    async def bank_page():
        return HTMLResponse(_template("bank_portal.html"))

    @app.get("/assets/portal.css", response_class=PlainTextResponse)
    async def portal_css():
        return Response(_template("portal.css"), media_type="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/assets/portal.js", response_class=PlainTextResponse)
    async def portal_js():
        return Response(_template("portal.js"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})
