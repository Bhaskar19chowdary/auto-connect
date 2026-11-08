"""HTTP-level test of the trade finance REST API, portal decision links and operations page."""
import asyncio

import httpx

from auto_connect.dashboard import create_app


def test_trade_api_full_flow():
    async def go():
        app = create_app()
        engine = app.state.trade_engine
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/tf/reset")
            assert r.status_code == 200
            demo = (await c.post("/api/v1/tf/demo")).json()
            assert demo["deals"][demo["lc_shortfall_reallocated_paid"]] == "CLOSED"
            assert demo["deals"][demo["lc_discrepant_awaiting_client_waiver"]] == "DOCUMENTS_DISCREPANT"
            assert demo["deals"][demo["guarantee_demand_paid"]] == "ISSUED"
            assert demo["deals"][demo["surety_claim_awaiting_principal"]] == "ISSUED"

            # client clicks the signed waiver link from the email
            ref = demo["lc_discrepant_awaiting_client_waiver"]
            pid = engine.state.deals[ref].presentations[0].presentation_id
            link = engine.decision_link(ref, pid, "waive").split("8088", 1)[1]
            bad = await c.get(link[:-3] + "xyz")
            assert bad.status_code == 403
            ok = await c.get(link)
            assert ok.status_code == 200 and "recorded" in ok.text
            assert (await c.get(f"/api/v1/tf/deals/{ref}")).json()["status"] == "CLOSED"

            deals = (await c.get("/api/v1/tf/deals")).json()
            assert len(deals) == 4
            swift = (await c.get("/api/v1/tf/swift")).json()
            text = (await c.get(f"/api/v1/tf/swift/{swift[0]['message_id']}")).text
            assert text.startswith("{1:F01")
            summary = (await c.get("/api/v1/tf/summary")).json()
            assert summary["ledger_balanced"] is True
            assert len((await c.get("/api/v1/tf/systems")).json()) == 9
            assert (await c.get("/trade")).status_code == 200
            # new LC via REST with validation error -> 422 from pydantic
            assert (await c.post("/api/v1/tf/lc", json={"amount": "1"})).status_code == 422
            # wrong state -> 409
            assert (await c.post(f"/api/v1/tf/lc/{ref}/amend", json={"amount_delta": "1"})).status_code == 409
            # A2A directory now includes the trade systems
            agents = (await c.get("/.well-known/agent.json")).json()["agents"]
            assert any(a["agent_id"] == "tf_operations_engine" for a in agents)
            # batch
            r = await c.post("/api/v1/tf/batch", json={})
            assert r.status_code == 200
    asyncio.run(go())


def test_api_key_protection(monkeypatch):
    monkeypatch.setenv("AUTO_CONNECT_API_KEY", "s3cret-key")

    async def go():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            assert (await c.get("/api/v1/tf/summary")).status_code == 401
            assert (await c.get("/api/v1/tf/summary", headers={"X-API-Key": "s3cret-key"})).status_code == 200
            assert (await c.get("/")).status_code == 200
    asyncio.run(go())
