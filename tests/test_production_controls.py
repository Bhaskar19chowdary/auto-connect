"""Production controls: named users, role permissions, four-eyes, maker-checker, audit trail, readiness."""
import json
import os
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def bank(monkeypatch):
    d = tempfile.mkdtemp(prefix="auto_connect_prod_")
    monkeypatch.setenv("AUTO_CONNECT_ENV", "production")
    monkeypatch.setenv("AUTO_CONNECT_USERS_FILE", os.path.join(d, "users.json"))
    monkeypatch.setenv("AUTO_CONNECT_AUDIT_FILE", os.path.join(d, "audit.jsonl"))
    from auto_connect.dashboard import create_app
    from auto_connect.security import UserDirectory

    with pytest.raises(RuntimeError):  # production refuses to start without named users
        create_app()
    users = UserDirectory()
    keys = {uid: users.add(uid, uid.title(), roles) for uid, roles in [
        ("ops1", ["OPERATIONS"]), ("comp1", ["COMPLIANCE"]), ("comp2", ["COMPLIANCE"]), ("viewer", ["VIEWER"]),
        ("hybrid", ["OPERATIONS", "COMPLIANCE"]), ("admin", ["ADMIN"])]}
    app = create_app()
    client = TestClient(app)

    def call(user, method, path, body=None):
        return client.request(method, path, json=body, headers={"X-API-Key": keys[user]} if user else {})
    return app, call, os.path.join(d, "audit.jsonl")


def lc(today, beneficiary="Volkov Maritime Trading LLC", country="RU"):
    return {"applicant_customer_id": "DE-CORP-40551",
            "beneficiary": {"name": beneficiary, "address": "Novorossiysk", "country": country},
            "advising_bank_bic": "BNCAITMMXXX", "currency": "EUR", "amount": "100000",
            "expiry_date": (today + timedelta(days=90)).isoformat(), "latest_shipment_date": (today + timedelta(days=60)).isoformat(),
            "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "goods_description": "machines",
            "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3},
                                   {"doc_type": "INSURANCE_DOCUMENT"}]}


def test_roles_four_eyes_maker_checker_and_audit(bank):
    app, call, audit_file = bank
    today = app.state.trade_engine.today
    assert call(None, "GET", "/api/v1/tf/summary").status_code == 401
    assert call("viewer", "GET", "/api/v1/tf/summary").status_code == 200
    assert call("viewer", "POST", "/api/v1/tf/lc", lc(today)).status_code == 403
    assert call("ops1", "POST", "/api/v1/apps/connect", {"app_name": "x"}).status_code == 403  # admin only

    deal = call("ops1", "POST", "/api/v1/tf/lc", lc(today)).json()
    assert deal["status"] == "ON_HOLD_SANCTIONS" and deal["created_by"] == "ops1"
    exc = call("comp1", "GET", "/api/v1/tf/exceptions").json()[0]["exception_id"]
    resolve = f"/api/v1/tf/exceptions/{exc}/resolve"
    assert call("ops1", "POST", resolve, {"approve": True}).status_code == 403  # sanctions are compliance's call
    r = call("comp1", "POST", resolve, {"approve": True, "resolved_by": "someone-else", "note": "false positive"}).json()
    assert r["pending_approvals"] == 1 and r["exception"]["approvers"] == ["comp1"]  # body cannot fake the decider
    assert call("comp1", "POST", resolve, {"approve": True}).status_code == 409  # same person twice
    r = call("comp2", "POST", resolve, {"approve": True, "note": "confirmed"}).json()
    assert r["exception"]["status"] == "APPROVED" and r["deal"]["status"] == "ISSUED"

    deal2 = call("hybrid", "POST", "/api/v1/tf/lc", lc(today)).json()
    exc2 = [e for e in call("comp1", "GET", "/api/v1/tf/exceptions").json() if e["deal_ref"] == deal2["ref"]][0]
    assert call("hybrid", "POST", f"/api/v1/tf/exceptions/{exc2['exception_id']}/resolve", {"approve": True}).status_code == 409

    assert call("admin", "POST", "/api/v1/tf/reset").status_code == 403  # never in production
    assert call(None, "GET", "/docs").status_code == 404
    ready = call(None, "GET", "/ready").json()
    assert ready["status"] == "READY" and ready["checks"]["audit_trail_intact"]

    audit = call("admin", "GET", "/api/v1/audit/verify").json()
    assert audit["valid"] and audit["records"] >= 6
    lines = open(audit_file).read().splitlines()
    first = json.loads(lines[0])
    first["user"] = "nobody"
    lines[0] = json.dumps(first)
    open(audit_file, "w").write("\n".join(lines) + "\n")
    assert call("admin", "GET", "/api/v1/audit/verify").json()["valid"] is False
    assert call(None, "GET", "/ready").status_code == 503


def test_iso20022_payment_and_munich_calendar():
    from auto_connect.trade.bankdays import add_banking_days, is_banking_day
    from auto_connect.trade.iso20022 import pacs008
    import xml.etree.ElementTree as ET

    assert not is_banking_day(date(2026, 6, 4))  # Corpus Christi (Bavaria)
    assert not is_banking_day(date(2026, 10, 3)) and not is_banking_day(date(2026, 12, 24))
    assert add_banking_days(date(2026, 5, 22), 2) == date(2026, 5, 27)  # Whit Monday skipped
    xml = pacs008("GTE1C1", "DMD-1", date(2026, 11, 9), "EUR", 50000, "Demo Bank AG", "DE89370400440532013000",
                  "Saudi Industrial Projects Co", "SA0380000000608010167519", "SA", "PAYMENT UNDER GTE1", "DEUTDEFFXXX",
                  "DEMODEMMXXX")
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert any(e.tag.endswith("IntrBkSttlmAmt") and e.text == "50000.00" for e in root.iter())
