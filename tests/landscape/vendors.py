"""
Independent external bank systems for the landscape test. Each one runs in its OWN process with its OWN books
(a JSON state file) and its OWN API style, like products from different vendors:

  cif_app        Corporate Customer Hub   nested data.party, KYC codes V/E/P, risk class 1/2/3, dates DD.MM.YYYY
  screening_app  WatchGuard Screening     outcome NO_HIT/POTENTIAL_HIT/HIT, findings TRUE_MATCH/POSSIBLE
  rates_app      Treasury Rates Service   rate per path, sell/buy conversion
  ledger_app     CoreBank GL              journals in minor units (cents), dr/cr lines, refuses overdraft (NSF)
  limits_app     LimitGuard               product codes IMP_LC/GTEE/AVAL, amount objects, LINE_EXCEEDED / NO_LINE
  dms_app        DocuVault DMS            base64 documents in dossiers
  portal_app     CorporateConnect         client messages with action lists

Every system logs each request with its X-Correlation-ID and can be told to fail (POST /_admin/fail).
Run:  python -m uvicorn tests.landscape.vendors:ledger_app --factory --port 9101   (VENDOR_STATE=/path/state.json)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_lock = threading.Lock()


def _seed():
    from auto_connect.trade.state import seed_state

    return seed_state(date.today())


class Store:
    def __init__(self, init):
        self.path = os.environ.get("VENDOR_STATE", "")
        if self.path and os.path.exists(self.path):
            with open(self.path) as f:
                self.data = json.load(f)
        else:
            self.data = init()
            self.data.setdefault("_log", [])
            self.data.setdefault("_fail", {})
            self.save()

    def save(self):
        if self.path:
            with open(self.path + ".tmp", "w") as f:
                json.dump(self.data, f, default=str)
            os.replace(self.path + ".tmp", self.path)


def _base(title: str, init) -> (FastAPI, Store):
    app = FastAPI(title=title, version="1.0")
    st = Store(init)
    app.state.store = st

    @app.middleware("http")
    async def log(request: Request, call_next):
        if not request.url.path.startswith(("/_admin", "/openapi", "/docs")):
            body = (await request.body()).decode("utf-8", "replace")
            with _lock:
                st.data["_log"].append({"method": request.method, "path": request.url.path,
                                        "correlation": request.headers.get("x-correlation-id"), "body": body[:4000],
                                        "query": str(request.query_params)})
                fail = st.data["_fail"]
                key = next((k for k, f in fail.items() if f["path"] in request.url.path and f["times"] > 0
                            and (not f.get("contains") or f["contains"] in body)), None)
                if key:
                    fail[key]["times"] -= 1
                    st.save()
                    return JSONResponse({"error": {"code": "SYSTEM_UNAVAILABLE", "message": "injected failure"}},
                                        status_code=fail[key].get("status", 503))
                st.save()
        return await call_next(request)

    @app.get("/_admin/state", include_in_schema=False)
    async def state():
        return st.data

    @app.post("/_admin/fail", include_in_schema=False)
    async def arm(req: Request):
        b = await req.json()
        with _lock:
            st.data["_fail"][f"{b['path']}|{b.get('contains', '')}"] = {
                "path": b["path"], "times": int(b.get("times", 1)), "status": int(b.get("status", 503)),
                "contains": b.get("contains", "")}
            st.save()
        return {"armed": b}
    return app, st


def _err(code: str, msg: str, status: int):
    return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status)


def _de(d: date) -> str:
    return d.strftime("%d.%m.%Y")


# ----------------------------------------------------------------------------------------------- customer hub
def cif_app():
    def init():
        s = _seed()
        parties = {}
        for c in s.customers.values():
            parties[c.customer_id] = {
                "partyId": c.customer_id, "legalName": c.name,
                "postalAddress": {"line": c.address, "countryIso": c.country},
                "contact": {"email": c.email, "rmEmail": c.relationship_manager_email},
                "kyc": {"state": "V", "nextReview": _de(c.kyc_next_review)},
                "settlementAccount": {"iban": c.current_account_iban},
                "riskClass": {"LOW": "1", "MEDIUM": "2", "HIGH": "3"}[c.risk_rating]}
        return {"parties": parties}
    app, st = _base("Corporate Customer Hub", init)

    @app.get("/v3/parties/{partyId}", summary="Get a corporate party with its KYC state", operation_id="getParty")
    async def party(partyId: str):
        p = st.data["parties"].get(partyId)
        if not p:
            return _err("PARTY_NOT_FOUND", f"party {partyId} unknown", 404)
        return {"data": {"party": p}, "meta": {"source": "CCH", "ts": datetime.utcnow().isoformat()}}

    @app.get("/v3/kyc/reviews", summary="Parties whose KYC review falls due", operation_id="listReviews")
    async def reviews(horizonDays: int = 30):
        lim = date.today() + timedelta(days=horizonDays)
        items = [{"partyId": p["partyId"], "legalName": p["legalName"], "nextReview": p["kyc"]["nextReview"],
                  "rmEmail": p["contact"]["rmEmail"]} for p in st.data["parties"].values()
                 if datetime.strptime(p["kyc"]["nextReview"], "%d.%m.%Y").date() <= lim]
        return {"items": items, "count": len(items)}

    @app.post("/_admin/kyc/{partyId}", include_in_schema=False)
    async def set_kyc(partyId: str, req: Request):
        st.data["parties"][partyId]["kyc"]["state"] = (await req.json())["state"]
        st.save()
        return st.data["parties"][partyId]
    return app


# ----------------------------------------------------------------------------------------------- screening
def screening_app():
    app, st = _base("WatchGuard Screening", lambda: {"cases": [], "watchlist": [
        {"name": "volkov maritime trading", "list": "EU 833/2014"}, {"name": "black sea shadow logistics", "list": "OFAC SDN"}],
        "embargo": {"IR": "EU/US measures", "KP": "UN embargo", "SY": "EU measures"}, "vessels": ["IMO9123456"]})

    @app.post("/screening/v2/cases", summary="Screen a transaction (parties, places, vessel, goods)", operation_id="createCase")
    async def case(req: Request):
        b = await req.json()
        findings = []
        for sub in b.get("subjects", []):
            n = (sub.get("name") or "").lower()
            for w in st.data["watchlist"]:
                if w["name"] in n:
                    findings.append({"subjectName": sub["name"], "listName": w["list"], "score": 98, "decision": "TRUE_MATCH"})
            if (sub.get("countryIso") or "").upper() in st.data["embargo"]:
                findings.append({"subjectName": sub["name"], "listName": st.data["embargo"][sub["countryIso"].upper()],
                                 "score": 100, "decision": "TRUE_MATCH"})
        if (b.get("vesselImo") or "").replace(" ", "").upper() in st.data["vessels"]:
            findings.append({"subjectName": b["vesselImo"], "listName": "Sanctioned vessels", "score": 100, "decision": "TRUE_MATCH"})
        outcome = "HIT" if any(f["decision"] == "TRUE_MATCH" for f in findings) else ("POTENTIAL_HIT" if findings else "NO_HIT")
        cid = f"WG-{len(st.data['cases']) + 1:06d}"
        st.data["cases"].append({"caseId": cid, "ref": b.get("caseRef"), "outcome": outcome, "subjects": b.get("subjects")})
        st.save()
        return {"caseId": cid, "outcome": outcome, "findings": findings}
    return app


# ----------------------------------------------------------------------------------------------- rates
def rates_app():
    per_eur = {"USD": "1.1165", "GBP": "0.83415", "CHF": "0.9143", "JPY": "176.77", "CNY": "7.9500", "EUR": "1"}
    app, st = _base("Treasury Rates Service", lambda: {"per_eur": per_eur})

    @app.get("/rates/{ccy}", summary="Mid rate of a currency against EUR", operation_id="getRate")
    async def rate(ccy: str, quote: str = "EUR"):
        if ccy not in st.data["per_eur"]:
            return _err("UNKNOWN_CCY", ccy, 404)
        return {"pair": f"{ccy}{quote}", "mid": str((Decimal(1) / Decimal(st.data["per_eur"][ccy])).quantize(Decimal("0.0000001"))),
                "asOf": _de(date.today())}

    @app.post("/fx/convert", summary="Convert an amount at mid rate", operation_id="convert")
    async def convert(req: Request):
        b = await req.json()
        pe = st.data["per_eur"]
        if b["sellCcy"] not in pe or b["buyCcy"] not in pe:
            return _err("UNKNOWN_CCY", "currency", 404)
        eur = Decimal(str(b["sellAmount"])) / Decimal(pe[b["sellCcy"]])
        return {"buyAmount": str((eur * Decimal(pe[b["buyCcy"]])).quantize(Decimal("0.01"))), "buyCcy": b["buyCcy"]}
    return app


# ----------------------------------------------------------------------------------------------- core ledger
def ledger_app():
    def init():
        s = _seed()
        accts = {a.account_id: {"ccy": a.currency, "minor": int(a.balance * 100), "odMinor": int(a.overdraft_limit * 100),
                                "holds": {}} for a in s.accounts.values()}
        for ccy in s.reference.fx_eur_per_unit:
            accts.setdefault(f"GL_DUE_FROM_BANKS_{ccy}", {"ccy": ccy, "minor": 0, "odMinor": 0, "holds": {}})
        for acc in ("OB_CONF_CUSTOMER_LIABILITY", "OB_CONF_BANK_COMMITMENT"):
            accts.setdefault(acc, {"ccy": "EUR", "minor": 0, "odMinor": 0, "holds": {}})
        return {"accounts": accts, "journals": []}
    app, st = _base("CoreBank GL", init)

    def available(a):
        return a["minor"] + a["odMinor"] - sum(a["holds"].values())

    @app.post("/gl/v1/journals", summary="Post a balanced journal", operation_id="postJournal")
    async def journal(req: Request):
        b = await req.json()
        A = st.data["accounts"]
        lines = b.get("lines", [])
        for ln in lines:
            for k in ("drAcct", "crAcct"):
                if ln[k] not in A:
                    return _err("UNKNOWN_ACCOUNT", ln[k], 422)
            ln.setdefault("ccy", A[ln["drAcct"]]["ccy"])
            if A[ln["drAcct"]]["ccy"] != A[ln["crAcct"]]["ccy"] or A[ln["drAcct"]]["ccy"] != ln["ccy"]:
                return _err("CCY_MISMATCH", str(ln), 422)
            if int(ln["amountMinor"]) <= 0:
                return _err("INVALID_AMOUNT", str(ln), 422)
        for ln in lines:  # customer accounts must stay within overdraft unless overridden
            dr = A[ln["drAcct"]]
            if ln["drAcct"].startswith("DE") and not b.get("overdraftOverride") and available(dr) - int(ln["amountMinor"]) < 0:
                return _err("NSF", f"insufficient funds on {ln['drAcct']}", 422)
        with _lock:
            for ln in lines:
                A[ln["drAcct"]]["minor"] -= int(ln["amountMinor"])
                A[ln["crAcct"]]["minor"] += int(ln["amountMinor"])
            jid = f"J{len(st.data['journals']) + 1:07d}"
            st.data["journals"].append({"journalId": jid, "ref": b.get("journalRef"), "valueDate": b.get("valueDate"),
                                        "lines": lines})
            st.save()
        return {"journalId": jid, "status": "POSTED"}

    @app.get("/gl/v1/accounts/{acct}/balance", summary="Booked and available balance", operation_id="getBalance")
    async def balance(acct: str):
        a = st.data["accounts"].get(acct)
        if not a:
            return _err("UNKNOWN_ACCOUNT", acct, 404)
        return {"account": acct, "ccy": a["ccy"], "balances": {"booked": {"amountMinor": a["minor"]},
                                                               "available": {"amountMinor": available(a)}}}

    @app.post("/gl/v1/holds", summary="Place a hold (blocked funds)", operation_id="placeHold")
    async def hold(req: Request):
        b = await req.json()
        a = st.data["accounts"][b["acct"]]
        with _lock:
            a["holds"][b["holdRef"]] = a["holds"].get(b["holdRef"], 0) + int(b["amountMinor"])
            st.save()
        return {"holdRef": b["holdRef"], "heldMinor": a["holds"][b["holdRef"]]}

    @app.post("/gl/v1/holds/release", summary="Release a hold", operation_id="releaseHold")
    async def release(req: Request):
        b = await req.json()
        a = st.data["accounts"][b["acct"]]
        with _lock:
            held = a["holds"].get(b["holdRef"], 0)
            rel = held if b.get("amountMinor") in (None, "") else min(held, int(b["amountMinor"]))
            a["holds"][b["holdRef"]] = held - rel
            if a["holds"][b["holdRef"]] <= 0:
                a["holds"].pop(b["holdRef"], None)
            st.save()
        return {"releasedMinor": rel}
    return app


# ----------------------------------------------------------------------------------------------- limits
def limits_app():
    code = {"LC": "IMP_LC", "GUARANTEE": "GTEE", "SURETY": "AVAL", "WORKING_CAPITAL": "WCAP"}

    def init():
        s = _seed()
        lines = {}
        for f in s.facilities.values():
            if f.customer_id.startswith("BANK:"):
                lines[f"CPTY-{f.customer_id[5:]}"] = {"party": f.customer_id, "product": "CONF", "limit": str(f.limit), "used": "0"}
            else:
                lines[f"LN-{f.facility_id[4:]}"] = {"party": f.customer_id, "product": code[f.product], "limit": str(f.limit), "used": "0"}
        return {"lines": lines, "reservations": {}}
    app, st = _base("LimitGuard", init)

    def find(party, product):
        for lid, ln in st.data["lines"].items():
            if ln["party"] == party and ln["product"] == product:
                return lid, ln
        if product == "AVAL":
            return find(party, "GTEE")
        return None, None

    def reserve(lid, ln, ref, value, override):
        avail = Decimal(ln["limit"]) - Decimal(ln["used"])
        if value > avail and not override:
            return _err("LINE_EXCEEDED", f"{lid}: requested {value}, available {avail}", 409)
        with _lock:
            if value > avail:
                ln["limit"] = str(Decimal(ln["limit"]) + value - avail)  # approved excess
            ln["used"] = str(Decimal(ln["used"]) + value)
            r = st.data["reservations"].setdefault(ref, {"line": lid, "amount": "0"})
            r["amount"] = str(Decimal(r["amount"]) + value)
            st.save()
        return JSONResponse({"reservationId": f"RS-{ref}", "lineId": lid, "reserved": {"value": str(value), "ccy": "EUR"},
                             "lineAvailable": {"value": str(Decimal(ln["limit"]) - Decimal(ln["used"]))}}, status_code=201)

    @app.post("/limits/reservations", summary="Reserve a credit line for a deal", operation_id="reserve")
    async def reservations(req: Request):
        b = await req.json()
        lid, ln = find(b["partyId"], b["productCode"])
        if not ln:
            return _err("NO_LINE", f"no {b['productCode']} line for {b['partyId']}", 404)
        return reserve(lid, ln, b["externalRef"], Decimal(str(b["amount"]["value"])), b.get("approvedOverride"))

    @app.post("/limits/counterparty-reservations", summary="Reserve a bank counterparty line", operation_id="reserveCpty")
    async def cpty(req: Request):
        b = await req.json()
        lid = f"CPTY-{b['bankBic'][:8]}"
        ln = st.data["lines"].get(lid)
        if not ln:
            if not b.get("approvedOverride"):
                return _err("NO_LINE", f"no counterparty line for {b['bankBic']}", 404)
            ln = st.data["lines"].setdefault(lid, {"party": f"BANK:{b['bankBic'][:8]}", "product": "CONF", "limit": "0", "used": "0"})
        return reserve(lid, ln, b["externalRef"], Decimal(str(b["amount"]["value"])), b.get("approvedOverride"))

    @app.post("/limits/reservations/release", summary="Release (part of) a deal's reservation", operation_id="release")
    async def release(req: Request):
        b = await req.json()
        r = st.data["reservations"].get(b["externalRef"])
        if not r:
            return {"released": {"value": "0"}}
        amt = Decimal(r["amount"]) if not b.get("amount") else min(Decimal(r["amount"]), Decimal(str(b["amount"]["value"])))
        with _lock:
            ln = st.data["lines"][r["line"]]
            ln["used"] = str(Decimal(ln["used"]) - amt)
            r["amount"] = str(Decimal(r["amount"]) - amt)
            if Decimal(r["amount"]) <= 0:
                st.data["reservations"].pop(b["externalRef"])
            st.save()
        return {"released": {"value": str(amt), "ccy": "EUR"}}

    @app.get("/limits/parties/{partyId}", summary="Lines of a party", operation_id="partyLines")
    async def party(partyId: str):
        return {"lines": [{"lineId": k, **v} for k, v in st.data["lines"].items() if v["party"] == partyId]}
    return app


# ----------------------------------------------------------------------------------------------- DMS
def dms_app():
    app, st = _base("DocuVault DMS", lambda: {"documents": []})

    @app.post("/dms/api/documents", summary="Store a document in a dossier", operation_id="storeDocument")
    async def store(req: Request):
        import base64

        b = await req.json()
        content = base64.b64decode(b["contentBase64"]).decode("utf-8")
        did = f"DOC{len(st.data['documents']) + 1:06d}"
        st.data["documents"].append({"documentId": did, "dossier": b["dossier"], "docClass": b["docClass"],
                                     "title": b["title"], "size": len(content), "head": content[:80]})
        st.save()
        return {"documentId": did}
    return app


# ----------------------------------------------------------------------------------------------- portal
def portal_app():
    app, st = _base("CorporateConnect", lambda: {"messages": []})

    @app.post("/cc/v1/messages", summary="Send a message to a corporate client", operation_id="sendMessage")
    async def send(req: Request):
        b = await req.json()
        mid = f"MSG{len(st.data['messages']) + 1:06d}"
        st.data["messages"].append({"messageId": mid, **b})
        st.save()
        return {"messageId": mid, "delivered": True}
    return app
