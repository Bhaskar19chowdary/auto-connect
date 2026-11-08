"""Connected applications doing engine work: a certified external SWIFT interface with a different API (field
mapping), outbox + correlation id, restart persistence, four-eyes certification, and a customer-master app that
binds itself after contract tests."""
import os
import socket
import tempfile
import threading
import time
from datetime import timedelta

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def serve(app):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return server, f"http://127.0.0.1:{port}"


def swift_interface():
    """A bank's SWIFT interface with its own API: POST /messages {messageType, receiver, payload, reference}."""
    app = FastAPI(title="SWIFT Interface Service", version="7.2")
    app.state.received = []

    @app.post("/messages", summary="Submit a FIN message for release", operation_id="submitMessage")
    async def submit(request: Request):
        body = await request.json()
        app.state.received.append({**body, "correlation": request.headers.get("x-correlation-id")})
        return {"id": f"SAA-{len(app.state.received):05d}", "state": "QUEUED_FOR_NETWORK"}

    @app.get("/health", summary="Service health")
    async def health():
        return {"status": "UP"}
    return app


def lc(today):
    return {"applicant_customer_id": "DE-CORP-40551",
            "beneficiary": {"name": "Milano Meccanica S.p.A.", "address": "Milano", "country": "IT"},
            "advising_bank_bic": "BNCAITMMXXX", "currency": "EUR", "amount": "100000", "incoterms": "FOB",
            "expiry_date": (today + timedelta(days=60)).isoformat(), "latest_shipment_date": (today + timedelta(days=30)).isoformat(),
            "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "goods_description": "machines",
            "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]}


MAPPING = {"release_message": {"method": "POST", "path": "/messages",
                               "request": {"mt_type": "messageType", "receiver_bic": "receiver", "fin_text": "payload",
                                           "deal_ref": "reference"},
                               "response": {"message_id": "id", "status": "state"}}}


def test_certified_swift_interface_with_mapping_outbox_and_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", f"sqlite:///{tmp_path / 'bank.db'}")
    monkeypatch.setenv("AUTO_CONNECT_STATE_FILE", str(tmp_path / "unused.json"))
    vendor = swift_interface()
    server, url = serve(vendor)
    try:
        from auto_connect.dashboard import create_app

        c = TestClient(create_app())
        today = c.app.state.trade_engine.today
        r = c.post("/api/v1/apps/connect", json={"app_name": "SWIFT Interface Service", "base_url": url, "auth_type": "None"}).json()
        assert r["status"] == "SUCCESS" and r["binding"]["status"] != "ACTIVE"  # writes are never switched automatically
        c.post("/api/v1/tf/lc", json=lc(today))
        assert vendor.state.received == []  # not certified -> engine does not use it

        bad = c.post("/api/v1/apps/agent_swift_interface_service/certify",
                     json={"role": "tf_swift_gateway", "certified_by": "it.admin", "evidence": "x"})
        assert bad.status_code == 422  # evidence required
        nomap = c.post("/api/v1/apps/agent_swift_interface_service/certify",
                       json={"role": "tf_swift_gateway", "certified_by": "it.admin", "evidence": "UAT run 2026-11-08 #4411"})
        assert nomap.status_code == 422 and "mapping" in nomap.json()["detail"]  # its API differs from the contract
        b = c.post("/api/v1/apps/agent_swift_interface_service/certify",
                   json={"role": "tf_swift_gateway", "certified_by": "it.admin", "evidence": "UAT run 2026-11-08 #4411",
                         "mapping": MAPPING}).json()
        assert b["status"] == "ACTIVE" and b["mode"] == "MAPPED"

        deal = c.post("/api/v1/tf/lc", json=lc(today)).json()
        got = vendor.state.received
        assert len(got) == 1 and got[0]["messageType"] == "700" and got[0]["reference"] == deal["ref"]
        assert got[0]["payload"].startswith("{1:F01") and got[0]["correlation"] == deal["ref"]
        assert c.get("/api/v1/tf/outbox").json()[-1]["status"] == "SENT"

        assert c.post(f"/api/v1/tf/lc/{deal['ref']}/amend", json={"amount_delta": "-500000"}).status_code == 409
        assert len(vendor.state.received) == 1  # refused action: nothing reached the SWIFT interface

        # restart: certification is part of the bank's saved configuration
        c2 = TestClient(create_app())
        assert c2.app.state.trade_engine.integration.active_for("tf_swift_gateway").agent_id == "agent_swift_interface_service"
        c2.post("/api/v1/tf/lc", json=lc(today))
        assert len(vendor.state.received) == 2
    finally:
        server.should_exit = True


def test_four_eyes_certification_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_ENV", "production")
    monkeypatch.setenv("AUTO_CONNECT_USERS_FILE", str(tmp_path / "users.json"))
    from auto_connect.dashboard import create_app
    from auto_connect.security import UserDirectory

    users = UserDirectory()
    k1, k2 = users.add("admin1", "Admin One", ["ADMIN"]), users.add("admin2", "Admin Two", ["ADMIN"])
    ops = users.add("ops1", "Ops", ["OPERATIONS"])
    vendor = swift_interface()
    server, url = serve(vendor)
    try:
        c = TestClient(create_app())
        h1, h2 = {"X-API-Key": k1}, {"X-API-Key": k2}
        assert c.post("/api/v1/apps/connect", headers=h1, json={"app_name": "SWIFT Interface Service", "base_url": url,
                                                                "auth_type": "None"}).json()["status"] == "SUCCESS"
        body = {"role": "tf_swift_gateway", "evidence": "UAT run 2026-11-08 #4411", "mapping": MAPPING}
        assert c.post("/api/v1/apps/agent_swift_interface_service/certify", headers={"X-API-Key": ops}, json=body).status_code == 403
        b = c.post("/api/v1/apps/agent_swift_interface_service/certify", headers=h1, json=body).json()
        assert b["status"] == "PENDING_APPROVAL" and b["settings"]["certified_by"] == "admin1"
        assert c.post("/api/v1/apps/agent_swift_interface_service/certify/approve", headers=h1).status_code == 409
        b = c.post("/api/v1/apps/agent_swift_interface_service/certify/approve", headers=h2).json()
        assert b["status"] == "ACTIVE" and b["settings"]["approved_by"] == "admin2"
    finally:
        server.should_exit = True


def test_customer_master_app_binds_after_contract_test(monkeypatch):
    """A KYC platform implementing the published contract is plugged in automatically (read-only role)."""
    kyc = FastAPI(title="Corporate KYC Platform", version="5.2")

    @kyc.post("/api/v1/tf_customer_master/get_customer", summary="Customer and KYC verdict", operation_id="get_customer")
    async def get_customer(request: Request):
        body = await request.json()
        return {"customer": {"customer_id": body["customer_id"], "name": "from KYC platform"},
                "kyc_ok": body["customer_id"] != "DE-CORP-40551", "kyc_issue": "UBO declaration expired"}

    @kyc.post("/api/v1/tf_customer_master/periodic_reviews_due", summary="Reviews due", operation_id="periodic_reviews_due")
    async def due():
        return {"due": []}
    server, url = serve(kyc)
    try:
        from auto_connect.dashboard import create_app

        c = TestClient(create_app())
        r = c.post("/api/v1/apps/connect", json={"app_name": "Corporate KYC Platform", "base_url": url, "auth_type": "None"}).json()
        assert r["binding"]["role"] == "tf_customer_master" and r["binding"]["status"] == "ACTIVE", r["binding"]
        deal = c.post("/api/v1/tf/lc", json=lc(c.app.state.trade_engine.today)).json()
        assert deal["status"] == "ON_HOLD_KYC"  # the engine used the KYC platform's verdict
        exc = c.get("/api/v1/tf/exceptions").json()[0]
        assert "UBO declaration expired" in exc["reason"]
    finally:
        server.should_exit = True
