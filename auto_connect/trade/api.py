"""
REST API, client-portal decision links, operations page and end-of-day scheduler
for the Trade Finance Operations Engine. Mounted into the main FastAPI app.
"""

import asyncio
import logging
import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .. import settings
from .engine import TradeFinanceEngine, WorkflowError
from .models import (CollectionTerms, Demand, ExportLCTerms, GuaranteeTerms, LCTerms, Party, Presentation,
                     SuretyClaim, SuretyTerms)
from .samples import (
    sample_demand, sample_guarantee, sample_lc, sample_presentation, sample_surety, sample_surety_claim,
)
from .state import StateStore, dump_state_summary, to_jsonable
from .systems import BusError

logger = logging.getLogger("auto_connect.trade.api")


def mount_trade_api(app: FastAPI, vault, mesh, executor) -> TradeFinanceEngine:
    from .database import open_store

    store = open_store()
    engine = TradeFinanceEngine(store, vault, mesh, executor)
    app.state.trade_engine = engine
    if engine.reference.enabled:  # load ECB rates + EU/OFAC lists in the background (cache first, download if stale)
        import threading

        threading.Thread(target=engine.reference.refresh, name="reference-data", daemon=True).start()
    scheduler: Dict[str, Any] = {"task": None, "last_run": None, "last_error": None}

    async def _scheduler_loop(minutes: float):
        while True:
            try:
                if date.today() >= engine.today:
                    res = await engine.run_daily_batch(date.today())
                    scheduler["last_run"] = res
                await engine.retry_waiting_applications()  # applications saved while a bank system was down
            except Exception as e:  # keep the loop alive; error visible in /summary
                scheduler["last_error"] = str(e)
                logger.exception("End-of-day batch failed")
            await asyncio.sleep(minutes * 60)

    @app.middleware("http")
    async def _start_scheduler(request: Request, call_next):
        if request.method == "GET" and request.url.path.startswith(("/api/v1/tf/", "/ready")):
            engine.sync()  # shared database: show what other application servers committed
        minutes = float(os.environ.get("TF_AUTO_BATCH_MINUTES", "60") or 0)
        if minutes > 0 and scheduler["task"] is None:
            scheduler["task"] = asyncio.create_task(_scheduler_loop(minutes))
        return await call_next(request)

    def ok(obj):
        return to_jsonable(obj)

    def maker(request: Request) -> Optional[str]:
        user = getattr(request.state, "user", None)
        return user["user_id"] if user else None

    async def guard(coro):
        try:
            return ok(await coro)
        except WorkflowError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except BusError as e:  # a bank system failed mid-request: say which one instead of a bare 500
            raise HTTPException(status_code=502, detail=f"{e.system or 'bank system'} failed ({e.code}): {e.detail}")

    # ------------------------------------------------------------------ read
    @app.get("/api/v1/tf/summary")
    async def tf_summary():
        st = engine.state
        by_status: Dict[str, int] = {}
        for d in st.deals.values():
            by_status[f"{d.product}:{d.status}"] = by_status.get(f"{d.product}:{d.status}", 0) + 1
        return {**dump_state_summary(st), "deals_by_status": by_status,
                "scheduler": {"running": scheduler["task"] is not None, "last_run": scheduler["last_run"],
                              "last_error": scheduler["last_error"]}}

    @app.get("/api/v1/tf/deals")
    async def tf_deals(product: Optional[str] = None, status: Optional[str] = None):
        deals = [d for d in engine.state.deals.values()
                 if (not product or d.product == product) and (not status or d.status == status)]
        return ok([{"ref": d.ref, "product": d.product, "customer_id": d.customer_id, "status": d.status,
                    "currency": d.currency, "amount": d.amount, "outstanding": d.outstanding,
                    "issue_date": d.issue_date, "open_exceptions": d.open_exceptions} for d in deals])

    @app.get("/api/v1/tf/deals/{ref}")
    async def tf_deal(ref: str):
        d = engine.state.deals.get(ref)
        if not d:
            raise HTTPException(404, f"Unknown deal {ref}")
        return ok(d)

    @app.get("/api/v1/tf/deals/{ref}/files")
    async def tf_deal_files(ref: str):
        d = engine.state.deals.get(ref)
        if not d:
            raise HTTPException(404, f"Unknown deal {ref}")
        return ok(d.attachments)

    @app.get("/api/v1/tf/files/{file_id}")
    async def tf_file(file_id: str):
        """The original document exactly as uploaded; its SHA-256 is checked before it is handed out."""
        import hashlib

        meta, data = engine.store.load_file(file_id)
        if meta is None:
            raise HTTPException(404, "Unknown file")
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise HTTPException(500, f"Integrity check failed for {file_id}: stored file does not match its fingerprint")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in meta["filename"])
        return Response(content=data, media_type=meta["mime"] or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{safe}"', "X-SHA256": meta["sha256"]})

    @app.get("/api/v1/tf/export/excel")
    async def tf_export_excel():
        """Read-only Excel report for staff: deals, bookings, fees, decisions, SWIFT messages and stored documents.
        The database stays the official record."""
        from .excel_report import build_report

        data = build_report(engine.state, settings.bank_name())
        name = f"trade_finance_report_{engine.today.isoformat()}.xlsx"
        return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/v1/tf/exceptions")
    async def tf_exceptions(status: str = "OPEN"):
        return ok([e for e in engine.state.exceptions.values() if status == "ALL" or e.status == status])

    @app.get("/api/v1/tf/customers")
    async def tf_customers():
        return ok(list(engine.state.customers.values()))

    @app.get("/api/v1/tf/facilities")
    async def tf_facilities():
        return ok([{**f.model_dump(mode="json"), "available": str(f.available)} for f in engine.state.facilities.values()])

    @app.get("/api/v1/tf/accounts")
    async def tf_accounts():
        return ok(list(engine.state.accounts.values()))

    @app.get("/api/v1/tf/ledger")
    async def tf_ledger(deal_ref: Optional[str] = None, limit: int = 500):
        entries = [e for e in engine.state.ledger if not deal_ref or e.deal_ref == deal_ref]
        return ok(entries[-limit:])

    @app.get("/api/v1/tf/swift")
    async def tf_swift(deal_ref: Optional[str] = None):
        msgs = [m for m in engine.state.swift_messages.values() if not deal_ref or m.deal_ref == deal_ref]
        return ok([{k: v for k, v in m.model_dump(mode="json").items() if k != "text"} for m in msgs])

    @app.get("/api/v1/tf/swift/{message_id}", response_class=PlainTextResponse)
    async def tf_swift_text(message_id: str):
        m = engine.state.swift_messages.get(message_id)
        if not m:
            raise HTTPException(404, "Unknown message")
        return m.text

    @app.get("/api/v1/tf/notifications")
    async def tf_notifications(limit: int = 100):
        return ok(engine.state.notifications[-limit:])

    @app.get("/api/v1/tf/reference")
    async def tf_reference():
        """Status of the live reference data (ECB rates, EU and OFAC sanctions lists) and the rates in use."""
        md = engine.systems["tf_market_data"]
        return ok({**engine.reference.summary(), "fx_eur_per_unit": md.rates(), "fx_as_of": md.as_of()})

    @app.post("/api/v1/tf/reference/refresh")
    async def tf_reference_refresh():
        return ok(await engine.refresh_reference_data(force=True))

    @app.get("/api/v1/tf/systems")
    async def tf_systems():
        out = []
        for sy in engine.systems.values():
            cred = vault._credentials.get(sy.agent_id)
            bound = engine.integration.active_for(sy.agent_id)
            if bound is not None:
                mode = f"APP:{bound.application_name}"
            elif executor.has_in_process_handler(sy.agent_id):
                mode = "SIMULATOR"
            else:
                mode = f"LIVE {getattr(cred, 'transport', 'HTTP') or 'HTTP'}"
            out.append({"agent_id": sy.agent_id, "name": sy.name, "department": sy.department, "mode": mode,
                        "operations": list(sy.operations)})
        return out

    @app.get("/api/v1/tf/systems/{agent_id}/openapi")
    async def tf_system_openapi(agent_id: str):
        s = engine.systems.get(agent_id)
        if not s:
            raise HTTPException(404, "Unknown system")
        return s.openapi()

    # ------------------------------------------------------------------ LC
    @app.post("/api/v1/tf/lc")
    async def tf_apply_lc(terms: LCTerms, request: Request):
        return await guard(engine.apply_lc(terms, created_by=maker(request)))

    @app.post("/api/v1/tf/lc/{ref}/amend")
    async def tf_amend_lc(ref: str, changes: Dict[str, Any] = Body(...)):
        return await guard(engine.amend_lc(ref, changes))

    @app.post("/api/v1/tf/lc/{ref}/amendments/{number}/beneficiary-response")
    async def tf_amend_response(ref: str, number: int, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.beneficiary_amendment_response(ref, number, bool(payload.get("accepted"))))

    @app.post("/api/v1/tf/lc/{ref}/presentations")
    async def tf_present(ref: str, presentation: Presentation):
        return await guard(engine.present_documents(ref, presentation))

    @app.post("/api/v1/tf/lc/{ref}/presentations/{presentation_id}/waiver")
    async def tf_waiver(ref: str, presentation_id: str, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.applicant_waiver_decision(ref, presentation_id, bool(payload.get("waive"))))

    # ------------------------------------------------------------------ document reading (IDR)
    async def _read(request: Request, files):
        from ..document_reader import DocumentReader

        brain = getattr(request.app.state, "brain", None)
        decoded = DocumentReader.decode(files or [])
        if not decoded:
            raise HTTPException(422, "files: give [{filename, content_base64}] or [{filename, text}]")
        limit = float(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024
        too_big = [n for n, data in decoded if len(data) > limit]
        if too_big:
            raise HTTPException(413, f"File(s) larger than {limit / 1024 / 1024:.0f} MB: {too_big}")
        request.state.uploaded_files = decoded  # the originals, kept with the deal
        return await asyncio.to_thread(DocumentReader(brain).read, decoded)

    def uploaded(request: Request):
        return getattr(request.state, "uploaded_files", None)

    @app.post("/api/v1/tf/documents/read")
    async def tf_read_documents(request: Request, payload: Dict[str, Any] = Body(...)):
        """Reads trade documents (PDF / image / text) into structured data - nothing is examined or booked."""
        return ok(await _read(request, payload.get("files")))

    @app.post("/api/v1/tf/lc/{ref}/presentations/upload")
    async def tf_present_upload(ref: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """Presenting bank sends the documents as files: they are read (IDR), then examined - or held for a
        document checker when the reading is incomplete."""
        from ..document_reader import to_presented

        reading = await _read(request, payload.get("files"))
        docs = [d for d in reading["documents"]]
        invoice = next((d for d in docs if d["doc_type"] == "COMMERCIAL_INVOICE"), None)
        claimed = payload.get("claimed_amount") or (invoice or {}).get("fields", {}).get("amount")
        review = list(reading["needs_review"])
        if not claimed:
            review.append("claimed amount not given and not readable from an invoice")
            claimed = "0.01"
        try:
            presentation = Presentation(
                presenting_bank_bic=payload["presenting_bank_bic"], presenting_bank_ref=payload.get("presenting_bank_ref", ""),
                presentation_date=payload.get("presentation_date") or engine.today.isoformat(),
                claimed_amount=claimed, documents=[to_presented(d) for d in docs])
        except (KeyError, ValueError) as e:
            raise HTTPException(422, f"Invalid presentation: {e}")
        lc = await guard(engine.present_documents(ref, presentation, review_reasons=review, files=uploaded(request),
                                                  uploaded_by=maker(request) or "presenting bank"))
        return {"reading": ok(reading), "presentation_id": presentation.presentation_id, "deal": lc}

    @app.post("/api/v1/tf/lc/{ref}/presentations/{presentation_id}/documents")
    async def tf_correct_documents(ref: str, presentation_id: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """Document checker corrects the read data (then approves the DATA_QUALITY exception to examine)."""
        from .models import PresentedDocument

        user = getattr(request.state, "user", None)
        if user:
            payload["corrected_by"] = user["user_id"]
        if not payload.get("corrected_by"):
            raise HTTPException(400, "corrected_by is required (four-eyes audit trail)")
        try:
            docs = [PresentedDocument(**d) for d in payload.get("documents") or []]
        except Exception as e:
            raise HTTPException(422, f"Invalid documents: {e}")
        return await guard(engine.correct_presentation_documents(ref, presentation_id, docs, payload["corrected_by"]))

    # ------------------------------------------------------------------ export LCs (advising / confirming bank)
    def _opt_date(v):
        return date.fromisoformat(v) if v else None

    @app.post("/api/v1/tf/export-lc")
    async def tf_receive_export_lc(terms: ExportLCTerms, request: Request):
        """Incoming MT700 in favour of our client: screened, advised (MT730), confirmed if requested and covered."""
        return await guard(engine.receive_export_lc(terms, created_by=maker(request)))

    @app.post("/api/v1/tf/export-lc/{ref}/presentations")
    async def tf_export_present(ref: str, presentation: Presentation):
        return await guard(engine.present_export_documents(ref, presentation))

    @app.post("/api/v1/tf/export-lc/{ref}/presentations/{presentation_id}/instruction")
    async def tf_export_instruction(ref: str, presentation_id: str, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.beneficiary_instruction(ref, presentation_id, bool(payload.get("send_on_approval"))))

    @app.post("/api/v1/tf/export-lc/{ref}/presentations/{presentation_id}/issuing-bank-response")
    async def tf_export_issuer(ref: str, presentation_id: str, payload: Dict[str, Any] = Body(...)):
        try:
            amount = Decimal(str(payload["amount"])) if payload.get("amount") is not None else None
            maturity = _opt_date(payload.get("maturity_date"))
        except (ValueError, ArithmeticError) as e:
            raise HTTPException(422, f"Invalid amount / date: {e}")
        return await guard(engine.issuing_bank_response(ref, presentation_id, str(payload.get("outcome", "")).upper(),
                                                        amount, maturity, payload.get("reason", "")))

    @app.post("/api/v1/tf/export-lc/{ref}/transfer")
    async def tf_export_transfer(ref: str, payload: Dict[str, Any] = Body(...)):
        try:
            second = Party(**payload["second_beneficiary"])
            amount = Decimal(str(payload["amount"]))
            ship, exp = _opt_date(payload.get("latest_shipment_date")), _opt_date(payload.get("expiry_date"))
        except (KeyError, ValueError, TypeError, ArithmeticError) as e:
            raise HTTPException(422, f"Invalid transfer request: {e}")
        return await guard(engine.transfer_export_lc(ref, second, payload.get("second_beneficiary_bank_bic", ""), amount, ship, exp))

    # ------------------------------------------------------------------ documentary collections (URC 522)
    @app.post("/api/v1/tf/collections")
    async def tf_new_collection(terms: CollectionTerms, request: Request):
        return await guard(engine.new_collection(terms, created_by=maker(request)))

    @app.post("/api/v1/tf/collections/{ref}/advice")
    async def tf_collection_advice(ref: str, payload: Dict[str, Any] = Body(...)):
        try:
            amount = Decimal(str(payload["amount"])) if payload.get("amount") is not None else None
            maturity = _opt_date(payload.get("maturity_date"))
        except (ValueError, ArithmeticError) as e:
            raise HTTPException(422, f"Invalid amount / date: {e}")
        return await guard(engine.collection_advice(ref, str(payload.get("advice", "")).upper(), amount, maturity,
                                                    payload.get("reason", "")))

    @app.post("/api/v1/tf/collections/{ref}/drawee-decision")
    async def tf_drawee_decision(ref: str, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.drawee_decision(ref, str(payload.get("decision", "")).lower()))

    # ------------------------------------------------------------------ outbox (emails / SWIFT after commit)
    @app.get("/api/v1/tf/outbox")
    async def tf_outbox(status: Optional[str] = None):
        items = [i for i in engine.state.outbox.values() if not status or i["status"] == status.upper()]
        return ok([{k: v for k, v in i.items() if k != "payload"} for i in items])

    @app.post("/api/v1/tf/outbox/dispatch")
    async def tf_outbox_dispatch():
        async with engine.lock:
            return await engine.dispatch_outbox()

    # ------------------------------------------------------------------ guarantees
    @app.post("/api/v1/tf/guarantees")
    async def tf_apply_guarantee(terms: GuaranteeTerms, request: Request):
        return await guard(engine.apply_guarantee(terms, created_by=maker(request)))

    @app.post("/api/v1/tf/guarantees/{ref}/amend")
    async def tf_amend_guarantee(ref: str, changes: Dict[str, Any] = Body(...)):
        return await guard(engine.amend_guarantee(ref, changes))

    @app.post("/api/v1/tf/guarantees/{ref}/demands")
    async def tf_demand(ref: str, demand: Demand):
        return await guard(engine.present_demand(ref, demand))

    async def _claim_from_upload(request: Request, payload: Dict[str, Any], what: str):
        """Reads the uploaded demand / claim documents; amount and currency come from the letter unless given."""
        from ..document_reader import to_presented

        reading = await _read(request, payload.get("files"))
        letter = next((d for d in reading["documents"] if d["doc_type"] == "DEMAND"), None)
        amount = payload.get("amount") or (letter or {}).get("fields", {}).get("amount")
        currency = payload.get("currency") or (letter or {}).get("fields", {}).get("currency")
        if not amount or not currency:
            raise HTTPException(422, {"detail": f"The {what} amount / currency could not be read from the documents - "
                                                f"enter them (amount, currency) and upload again", "reading": ok(reading)})
        docs = [to_presented(d) for d in reading["documents"]]
        return reading, docs, amount, currency

    @app.post("/api/v1/tf/guarantees/{ref}/demands/upload")
    async def tf_demand_upload(ref: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """The beneficiary's demand letter (and statement of breach etc.) as files: read, then examined (URDG 758)."""
        reading, docs, amount, currency = await _claim_from_upload(request, payload, "demand")
        try:
            demand = Demand(presentation_date=payload.get("presentation_date") or engine.today.isoformat(), amount=amount,
                            currency=currency, documents=docs, extend_or_pay=bool(payload.get("extend_or_pay")),
                            requested_new_expiry=payload.get("requested_new_expiry"))
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"Invalid demand: {e}")
        deal = await guard(engine.present_demand(ref, demand, files=uploaded(request),
                                                 uploaded_by=maker(request) or "beneficiary"))
        return {"reading": ok(reading), "demand_id": demand.demand_id, "deal": deal}

    @app.post("/api/v1/tf/sureties/{ref}/claims/upload")
    async def tf_claim_upload(ref: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """The obligee's claim letter, notice of default and evidence as files: read, then examined."""
        reading, docs, amount, currency = await _claim_from_upload(request, payload, "claim")
        try:
            claim = SuretyClaim(presentation_date=payload.get("presentation_date") or engine.today.isoformat(),
                                amount=amount, currency=currency, documents=docs)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"Invalid claim: {e}")
        deal = await guard(engine.present_surety_claim(ref, claim, files=uploaded(request),
                                                       uploaded_by=maker(request) or "obligee"))
        return {"reading": ok(reading), "claim_id": claim.claim_id, "deal": deal}

    @app.post("/api/v1/tf/guarantees/{ref}/demands/{demand_id}/extend-or-pay")
    async def tf_extend_or_pay(ref: str, demand_id: str, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.extend_or_pay_decision(ref, demand_id, bool(payload.get("extend"))))

    @app.post("/api/v1/tf/undertakings/{ref}/release")
    async def tf_release(ref: str, payload: Dict[str, Any] = Body(default={})):
        return await guard(engine.release_undertaking(ref, payload.get("reason") or "Released by beneficiary / original returned"))

    # ------------------------------------------------------------------ sureties
    @app.post("/api/v1/tf/sureties")
    async def tf_apply_surety(terms: SuretyTerms, request: Request):
        return await guard(engine.apply_surety(terms, created_by=maker(request)))

    @app.post("/api/v1/tf/sureties/{ref}/amend")
    async def tf_amend_surety(ref: str, changes: Dict[str, Any] = Body(...)):
        return await guard(engine.amend_surety(ref, changes))

    @app.post("/api/v1/tf/sureties/{ref}/claims")
    async def tf_claim(ref: str, claim: SuretyClaim):
        return await guard(engine.present_surety_claim(ref, claim))

    @app.post("/api/v1/tf/sureties/{ref}/claims/{claim_id}/principal-response")
    async def tf_principal_response(ref: str, claim_id: str, payload: Dict[str, Any] = Body(...)):
        return await guard(engine.principal_claim_response(ref, claim_id, bool(payload.get("dispute")), payload.get("reason", "")))

    # ------------------------------------------------------------------ exceptions & batch
    @app.post("/api/v1/tf/exceptions/{exception_id}/resolve")
    async def tf_resolve(exception_id: str, request: Request, payload: Dict[str, Any] = Body(...)):
        from ..security import APPROVALS_REQUIRED, EXCEPTION_ROLE

        user = getattr(request.state, "user", None)
        approve = bool(payload.get("approve"))
        exc = engine.state.exceptions.get(exception_id)
        if user:  # named users: the decision is taken by the responsible role, four-eyes for high-risk approvals
            if not exc:
                raise HTTPException(404, "Unknown exception")
            need = EXCEPTION_ROLE.get(exc.category, "OPERATIONS")
            if need not in user["roles"]:
                raise HTTPException(403, f"{exc.category} decisions are taken by role {need}")
            return await guard(engine.resolve_exception(exception_id, approve, user["user_id"], payload.get("note", ""),
                                                        APPROVALS_REQUIRED.get(exc.category, 1) if approve else 1))
        if not payload.get("resolved_by"):
            raise HTTPException(400, "resolved_by is required (four-eyes audit trail)")
        return await guard(engine.resolve_exception(exception_id, approve, payload["resolved_by"], payload.get("note", "")))

    @app.post("/api/v1/tf/batch")
    async def tf_batch(payload: Dict[str, Any] = Body(default={})):
        as_of = date.fromisoformat(payload["as_of"]) if payload.get("as_of") else date.today()
        return await guard(engine.run_daily_batch(max(as_of, engine.today)))

    def _not_in_production(what: str):
        from ..security import production

        if production():
            raise HTTPException(403, f"{what} is disabled in production (AUTO_CONNECT_ENV=production)")

    @app.post("/api/v1/tf/reset")
    async def tf_reset():
        _not_in_production("Resetting the bank's books")
        async with engine.lock:
            store.reset()
        return {"status": "RESET", **dump_state_summary(engine.state)}

    @app.post("/api/v1/tf/demo")
    async def tf_demo():
        """Runs representative transactions through the full lifecycle on the current business date."""
        _not_in_production("The demo run")
        today = engine.today
        out: Dict[str, Any] = {}
        lc = await engine.apply_lc(sample_lc(today))
        cust = engine.state.customers[lc.customer_id]
        out["lc_shortfall_reallocated_paid"] = lc.ref
        if lc.status == "ISSUED":
            await engine.present_documents(lc.ref, sample_presentation(lc.terms, today, today, cust.name))
        lc2 = await engine.apply_lc(sample_lc(today, "DE-CORP-40551", "300000"))
        out["lc_discrepant_awaiting_client_waiver"] = lc2.ref
        if lc2.status == "ISSUED":
            await engine.present_documents(lc2.ref, sample_presentation(
                lc2.terms, today, today, engine.state.customers[lc2.customer_id].name, with_insurance=False))
        g = await engine.apply_guarantee(sample_guarantee(today))
        out["guarantee_demand_paid"] = g.ref
        if g.status == "ISSUED":
            await engine.present_demand(g.ref, sample_demand(today))
        b = await engine.apply_surety(sample_surety(today))
        out["surety_claim_awaiting_principal"] = b.ref
        if b.status == "ISSUED":
            await engine.present_surety_claim(b.ref, sample_surety_claim(today))
        out["summary"] = dump_state_summary(engine.state)
        return ok({**out, "deals": {r: engine.state.deals[r].status for r in out.values() if isinstance(r, str)}})

    # ------------------------------------------------------------------ client portal decision links (HMAC-signed)
    @app.get("/portal/decision", response_class=HTMLResponse)
    async def portal_decision(ref: str, item: str, decision: str, sig: str):
        if not engine.verify(ref, item, decision, sig):
            raise HTTPException(403, "Invalid or tampered link")
        try:
            if decision in ("waive", "refuse"):
                await engine.applicant_waiver_decision(ref, item, decision == "waive")
            elif decision in ("extend", "pay"):
                await engine.extend_or_pay_decision(ref, item, decision == "extend")
            elif decision in ("send_on_approval", "correct"):
                await engine.beneficiary_instruction(ref, item, decision == "send_on_approval")
            elif decision in ("collection_pay", "collection_accept", "collection_refuse"):
                await engine.drawee_decision(ref, decision.split("_", 1)[1])
            elif decision in ("acknowledge", "dispute"):
                await engine.principal_claim_response(ref, item, decision == "dispute",
                                                      "Disputed via client portal" if decision == "dispute" else "")
            else:
                raise HTTPException(400, "Unknown decision")
            msg = f"Thank you. Your decision '{decision}' for {ref} has been recorded and processed."
        except WorkflowError as e:
            msg = f"This request was already processed or is no longer open ({e})."
        return HTMLResponse(f"<html><body style='font-family:sans-serif;padding:40px'><h2>{settings.bank_name()}</h2>"
                            f"<p>{msg}</p></body></html>")

    from .portal_api import mount_portals

    mount_portals(app, engine)

    @app.get("/trade", response_class=HTMLResponse)
    async def trade_page():
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "trade.html")
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())

    return engine
