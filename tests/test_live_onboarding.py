"""
Live onboarding tests against real HTTP servers (vendor APIs run as local ASGI apps behind an
httpx transport, so every call goes through real HTTP request/response handling):

* connect by URL only -> API description discovered automatically (FastAPI /openapi.json with $refs)
* wrong credentials rejected by the live read-only probe
* sanctions vendor auto-plugged into the trade engine after contract tests; real hits block deals
* vendor outage -> failover to the incumbent screening + ops alert + circuit breaker + auto-recovery
* FX vendor with a non-standard YAML spec URL and inverse quotation -> detected and inverted
* write-type systems are connected as agents but NOT switched automatically
* no API description -> clear error listing every location tried
* generated operator console, Gemini analysis (stubbed) and its failure reporting
"""

import asyncio
import json
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from auto_connect.dashboard import create_app
from auto_connect.trade.models import Party, PresentedDocument, SuretyClaim
from auto_connect.trade import samples

TOKEN = "sg-live-key-2026"


# ---------------------------------------------------------------------------
# vendor applications
# ---------------------------------------------------------------------------

def screenguard_app(state):
    app = FastAPI(title="ScreenGuard Watchlist API", version="3.2",
                  description="Name screening against global watchlists (OFAC, EU, UN, HMT)")
    WATCHLIST = {"ocean star shipping": "OFAC SDN", "northwind arms trading": "EU Consolidated List"}

    class CheckRequest(BaseModel):
        full_name: str
        country_code: str = ""

    def auth(authorization):
        if authorization != f"Bearer {TOKEN}":
            raise HTTPException(401, "invalid token")
        if state.get("down"):
            raise HTTPException(503, "maintenance")

    @app.get("/health")
    def health(authorization: str = Header(default="")):
        auth(authorization)
        return {"status": "UP"}

    @app.post("/v2/screening/check", operation_id="checkEntity")
    def check(req: CheckRequest, authorization: str = Header(default="")):
        auth(authorization)
        state["calls"] = state.get("calls", 0) + 1
        key = " ".join(w for w in req.full_name.lower().replace(".", "").split() if w not in {"ltd", "llc", "gmbh"})
        if key in WATCHLIST:
            return {"outcome": "MATCH", "score": 97, "matches": [{"list_name": WATCHLIST[key], "name": req.full_name}]}
        return {"outcome": "NO_MATCH", "score": 0, "matches": []}

    return app


FX_SPEC_YAML = """
openapi: 3.0.3
info:
  title: Euro FX Reference Rates
  version: "1.4"
  description: Daily exchange rate fixings (quoted as units of currency per 1 EUR)
paths:
  /fx/{currency}:
    get:
      operationId: getRate
      summary: Latest exchange rate for a currency against EUR
      parameters:
        - name: currency
          in: path
          required: true
          schema: {type: string}
  /ping:
    get:
      summary: Ping
"""


def fx_app():
    app = FastAPI(openapi_url=None)
    per_eur = {"USD": 1.087, "GBP": 0.855, "CHF": 0.952, "JPY": 163.9, "CNY": 7.81}

    @app.get("/docs/spec.yaml", response_class=PlainTextResponse)
    def spec():
        return FX_SPEC_YAML

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/fx/{currency}")
    def rate(currency: str):
        return {"pair": f"EUR{currency}", "rate": per_eur[currency.upper()]}

    return app


def ledger_app():
    app = FastAPI(title="Core Ledger Service", version="9.1", description="General ledger postings for core banking")

    class Posting(BaseModel):
        debit_account: str
        credit_account: str
        amount: float

    @app.get("/status")
    def status():
        return {"status": "OK"}

    @app.post("/postings")
    def post(p: Posting):
        return {"posted": True}

    return app


def nospec_app():
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/")
    def root():
        return {"hello": "world"}

    return app


class HostRouter(httpx.AsyncBaseTransport):
    """Routes requests by host name to local ASGI apps (real HTTP semantics, no sockets)."""

    def __init__(self, apps):
        self.apps = {h: httpx.ASGITransport(app=a) for h, a in apps.items()}

    async def handle_async_request(self, request):
        t = self.apps.get(request.url.host)
        if not t:
            raise httpx.ConnectError(f"cannot resolve {request.url.host}", request=request)
        return await t.handle_async_request(request)


def setup():
    vendor_state = {}
    app = create_app()
    app.state.executor.transport = HostRouter({
        "screenguard.example": screenguard_app(vendor_state),
        "rates.example": fx_app(),
        "ledger.example": ledger_app(),
        "nospec.example": nospec_app(),
    })
    return app, vendor_state


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def connect_payload(name, url, token=TOKEN, **extra):
    return {"app_name": name, "base_url": url, "auth_type": "Bearer", "token": token, "preset_type": "custom", **extra}


# ---------------------------------------------------------------------------

def test_sanctions_vendor_connected_by_url_and_plugged_into_trade_engine():
    async def go():
        app, vendor = setup()
        engine = app.state.trade_engine
        async with await client_for(app) as c:
            await c.post("/api/v1/tf/reset")
            # wrong token -> live read-only probe rejects it, nothing stays connected
            bad = (await c.post("/api/v1/apps/connect", json=connect_payload("ScreenGuard", "https://screenguard.example",
                                                                                 token="sg-old-key-2025"))).json()
            assert bad["status"] == "ERROR" and "401" in bad["message"]
            assert bad["pipeline"][-1]["status"] == "REJECTED_401"
            assert app.state.mesh.get_agent("agent_screenguard") is None

            r = (await c.post("/api/v1/apps/connect", json=connect_payload("ScreenGuard", "https://screenguard.example"))).json()
            assert r["status"] == "SUCCESS", r
            assert r["mode"] == "LIVE"
            steps = {p["step"]: p for p in r["pipeline"]}
            assert "https://screenguard.example/openapi.json" in steps["API Discovery"]["detail"]
            assert "COMPLIANCE_AML" in steps["Semantic Mapping"]["detail"]
            assert "rules engine" in steps["Semantic Mapping"]["detail"]
            assert steps["Live Connectivity & Auth Probe"]["detail"].startswith("GET /health -> 200")
            assert r["binding"]["status"] == "ACTIVE" and r["binding"]["mode"] == "ADAPTER"
            assert r["binding"]["role"] == "tf_sanctions_screening"
            assert r["binding"]["settings"]["name_field"] == "full_name"   # resolved through $ref
            tools = r["agent_card"]["discovered_tools"]
            assert any(t["tool_name"] == "check_entity" and t["required_parameters"] == ["full_name"] for t in tools)

            # a real vendor hit now blocks a deal the bank's own sample lists would have passed
            t = samples.sample_lc(engine.today)
            t.beneficiary = Party(name="Ocean Star Shipping Ltd", address="Piraeus", country="GR")
            res = (await c.post("/api/v1/tf/lc", json=json.loads(t.model_dump_json()))).json()
            assert res["status"] == "ON_HOLD_SANCTIONS"
            exc = engine.state.exceptions[res["open_exceptions"][0]]
            assert "ScreenGuard" in exc.reason and "OFAC SDN" in exc.reason
            assert any(m.target_agent_id == "agent_screenguard" and m.intent == "SCREEN_NAME"
                       for m in app.state.mesh.message_ledger)
            # client is told only that the application is being processed (no tipping-off)
            ack = [n for n in engine.state.notifications if n.get("deal_ref") == res["ref"] and n.get("subject")]
            assert ack and "being processed" in ack[-1]["subject"] and "sanction" not in ack[-1]["subject"].lower()

            clean = (await c.post("/api/v1/tf/lc", json=json.loads(samples.sample_lc(engine.today).model_dump_json()))).json()
            assert clean["status"] == "ISSUED"
            assert any(c_["case_id"].startswith("VND-") and c_["deal_ref"] == clean["ref"] for c_ in engine.state.sanctions_cases)

            # vendor outage -> failover to incumbent screening, ops alert, circuit breaker after 3 failures
            vendor["down"] = True
            for _ in range(3):
                r2 = (await c.post("/api/v1/tf/lc", json=json.loads(samples.sample_lc(engine.today, amount="10000").model_dump_json()))).json()
                assert r2["status"] == "ISSUED"   # business continues
            b = engine.integration.bindings["agent_screenguard"]
            assert b.failovers == 3 and b.status == "SUSPENDED"
            out_dir = os.environ["AUTO_CONNECT_OUTPUT_DIR"]
            assert any(f.startswith("OPS_FAILOVER_agent_screenguard") for f in os.listdir(out_dir))
            # vendor back -> end-of-day health check re-activates the binding
            vendor["down"] = False
            batch = (await c.post("/api/v1/tf/batch", json={})).json()
            assert any("re-activated" in a for a in batch["actions"])
            assert b.status == "ACTIVE"

            # generated console + details + read-only invoke through the console
            assert (await c.get("/apps/agent_screenguard")).status_code == 200
            det = (await c.get("/api/v1/apps/agent_screenguard/details")).json()
            assert det["mode"] == "LIVE" and det["binding"]["status"] == "ACTIVE"
            inv = (await c.post("/api/v1/apps/agent_screenguard/invoke",
                                json={"tool_name": "check_entity", "parameters": {"full_name": "Northwind Arms Trading"}})).json()
            assert inv["status_code"] == 200 and inv["data"]["outcome"] == "MATCH"
            assert (await c.post("/api/v1/apps/agent_screenguard/test")).json()["status"] == "SUCCESS"

            # disconnect releases the binding; the incumbent takes over again
            await c.delete("/api/v1/apps/agent_screenguard")
            assert engine.integration.active_for("tf_sanctions_screening") is None
    asyncio.run(go())


def test_fx_vendor_with_yaml_spec_and_inverse_quotes():
    async def go():
        app, _ = setup()
        engine = app.state.trade_engine
        async with await client_for(app) as c:
            await c.post("/api/v1/tf/reset")
            miss = (await c.post("/api/v1/apps/connect", json=connect_payload("ECB style rates", "https://rates.example"))).json()
            assert miss["status"] == "ERROR" and len(miss["discovery_attempts"]) >= 10
            r = (await c.post("/api/v1/apps/connect", json=connect_payload(
                "ECB style rates", "https://rates.example", spec_url="https://rates.example/docs/spec.yaml"))).json()
            assert r["status"] == "SUCCESS", r
            assert r["binding"]["role"] == "tf_market_data" and r["binding"]["status"] == "ACTIVE"
            assert r["binding"]["settings"]["invert"] is True   # vendor quotes USD per EUR
            t = samples.sample_lc(engine.today, customer_id="DE-CORP-40551", amount="100000")
            t.currency = "USD"
            lc = (await c.post("/api/v1/tf/lc", json=json.loads(t.model_dump_json()))).json()
            assert lc["status"] == "ISSUED"
            assert Decimal(lc["limit_utilisation_eur"]) == (Decimal("100000") / Decimal("1.087")).quantize(Decimal("0.01"))
    asyncio.run(go())


def test_write_system_is_connected_but_not_switched_and_missing_spec_reported():
    async def go():
        app, _ = setup()
        async with await client_for(app) as c:
            r = (await c.post("/api/v1/apps/connect", json=connect_payload("Core Ledger", "https://ledger.example"))).json()
            assert r["status"] == "SUCCESS"
            assert r["binding"]["status"] == "NOT_BOUND" and "certify" in r["binding"]["reason"]
            assert any(t["http_method"] == "POST" for t in r["agent_card"]["discovered_tools"])
            # the probe never called the POST endpoint
            assert "GET /status" in [p for p in r["pipeline"] if p["step"].startswith("Live")][0]["detail"]

            n = (await c.post("/api/v1/apps/connect", json=connect_payload("Mystery", "https://nospec.example"))).json()
            assert n["status"] == "ERROR" and "No OpenAPI" in n["message"]
            u = (await c.post("/api/v1/apps/connect", json=connect_payload("Offline", "https://offline.example"))).json()
            assert u["status"] == "ERROR" and "unreachable" in u["message"]
    asyncio.run(go())


def test_gemini_used_when_configured_and_failures_reported():
    async def go():
        app, _ = setup()
        brain = app.state.brain

        def fake_generate(model, contents, config):
            card = {"agent_id": "x", "application_name": "Core Ledger Service", "banking_department": "CORE_BANKING",
                    "system_instruction": "Ledger agent", "peer_departments_to_collaborate_with": ["TRADE_FINANCE"],
                    "discovered_tools": [{"tool_name": "health_status", "http_method": "GET", "endpoint_path": "/status",
                                          "description": "status", "required_parameters": [], "can_resolve_issues": []},
                                         {"tool_name": "create_posting", "http_method": "POST", "endpoint_path": "/postings",
                                          "description": "post", "required_parameters": ["debit_account"],
                                          "can_resolve_issues": []}]}
            return SimpleNamespace(text=json.dumps(card))

        brain.client = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate))
        async with await client_for(app) as c:
            r = (await c.post("/api/v1/apps/connect", json=connect_payload("Core Ledger", "https://ledger.example"))).json()
            assert r["status"] == "SUCCESS" and r["analysis"]["engine"] == "GEMINI"
            assert "Gemini (" in [p for p in r["pipeline"] if p["step"] == "Semantic Mapping"][0]["detail"]

            def broken(model, contents, config):
                raise RuntimeError("API key not valid. Please pass a valid API key.")

            brain.client = SimpleNamespace(models=SimpleNamespace(generate_content=broken))
            r2 = (await c.post("/api/v1/apps/connect", json=connect_payload("ScreenGuard", "https://screenguard.example"))).json()
            assert r2["status"] == "SUCCESS" and r2["analysis"]["engine"] == "RULES"
            assert "API key not valid" in r2["analysis"]["gemini_error"]
            eco = (await c.get("/api/v1/ecosystem")).json()
            assert eco["gemini"]["apps_analysed_by_gemini"] >= 1 and "API key not valid" in eco["gemini"]["last_gemini_error"]
    asyncio.run(go())


def test_client_and_counterparty_emails_and_reminders():
    async def go():
        app, _ = setup()
        engine = app.state.trade_engine
        async with await client_for(app) as c:
            await c.post("/api/v1/tf/reset")
            today = engine.today
            # wrong information in a guarantee application -> client email
            g = samples.sample_guarantee(today)
            g.beneficiary_bank_bic = "NOTABIC"
            res = (await c.post("/api/v1/tf/guarantees", json=json.loads(g.model_dump_json()))).json()
            assert res["status"] == "ON_HOLD_DATA_QUALITY"
            assert any(n.get("subject") == "Your guarantee application needs correction" for n in engine.state.notifications)

            # surety claim without evidence -> obligee gets the rejection by email
            st = samples.sample_surety(today)
            st.obligee.email = "vergabe@stadt-koeln.example"
            bond = await engine.apply_surety(st)
            claim = SuretyClaim(presentation_date=today, amount=Decimal("10000"), currency="EUR",
                                documents=[PresentedDocument(doc_type="DEMAND")])
            await engine.present_surety_claim(bond.ref, claim)
            out_dir = os.environ["AUTO_CONNECT_OUTPUT_DIR"]
            assert any(f.startswith(f"{bond.ref}_TO_StadtKoeln") for f in os.listdir(out_dir))

            # discrepant LC -> reminder on the deadline day, then automatic refusal the day after
            lc = await engine.apply_lc(samples.sample_lc(today, "DE-CORP-40551", "200000"))
            pres = samples.sample_presentation(lc.terms, today, today, "Bayerische Maschinenbau AG", with_insurance=False)
            await engine.present_documents(lc.ref, pres)
            deadline = lc.presentations[0].applicant_decision_deadline
            r = await engine.run_daily_batch(deadline)
            assert any("waiver reminder" in a for a in r["actions"])
            r = await engine.run_daily_batch(deadline + timedelta(days=1))
            assert lc.presentations[0].status == "REFUSED"
            # 30-day expiry reminder (sent once)
            r = await engine.run_daily_batch(lc.terms.expiry_date - timedelta(days=20))
            assert any(f"{lc.ref}: expiry reminder" in a for a in r["actions"])
            r = await engine.run_daily_batch(lc.terms.expiry_date - timedelta(days=19))
            assert not any(f"{lc.ref}: expiry reminder" in a for a in r["actions"])
    asyncio.run(go())
