"""Client portal and bank workbench: applications are saved before processing, survive outages and restarts,
clients only ever see their own company, client decisions work, and the plain-language views hide internal checks."""
import base64
import os
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

URLS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_POSTGRES_URL") else [])
HANSE, BMA, RHEIN = "DE-CORP-99214", "DE-CORP-40551", "DE-CORP-77310"


@pytest.fixture(params=URLS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'bank.db'}"
    import psycopg

    base = os.environ["TEST_POSTGRES_URL"]
    name = "p_" + uuid.uuid4().hex[:10]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    return base.rsplit("/", 1)[0] + "/" + name


def lc(today, amount="85000"):
    return {"beneficiary": {"name": "Milano Meccanica S.p.A.", "country": "IT"}, "advising_bank_bic": "BNCAITMMXXX",
            "amount": amount, "currency": "EUR", "incoterms": "FOB", "expiry_date": (today + timedelta(days=60)).isoformat(),
            "latest_shipment_date": (today + timedelta(days=30)).isoformat(), "port_of_loading": "Genoa",
            "port_of_discharge": "Hamburg", "goods_description": "CNC milling machines",
            "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]}


def client(cid):
    return {"X-Customer-Id": cid}


def test_application_saved_processed_and_private(db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", db_url)
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    proforma = b"PRO-FORMA INVOICE PF-77 total EUR 85,000.00"
    r = c.post("/api/v1/portal/applications", headers=client(BMA), json={
        "product": "LC", "details": {**lc(today), "applicant_customer_id": HANSE},  # cannot apply for another company
        "files": [{"filename": "proforma.txt", "content_base64": base64.b64encode(proforma).decode()}]})
    assert r.status_code == 200, r.text
    app = r.json()
    assert app["status"] == "PROCESSED" and app["customer_id"] == BMA and app["deal_ref"].startswith("ILC")
    deal = c.app.state.trade_engine.state.deals[app["deal_ref"]]
    assert deal.customer_id == BMA and deal.application_id == app["application_id"] and deal.created_by == f"client:{BMA}"

    # the client sees its transaction in plain words and can download its own document - another company cannot
    d = c.get(f"/api/v1/portal/deals/{app['deal_ref']}", headers=client(BMA)).json()
    assert d["status_label"] == "Active" and [s["title"] for s in d["story"]] == ["Application received", "Issued"]
    fid = d["documents"][0]["file_id"]
    assert c.get(f"/api/v1/portal/files/{fid}", headers=client(BMA)).content == proforma
    assert c.get(f"/api/v1/portal/files/{fid}", headers=client(HANSE)).status_code == 404
    assert c.get(f"/api/v1/portal/deals/{app['deal_ref']}", headers=client(HANSE)).status_code == 404
    assert all(x["customer_id"] == HANSE for x in c.get("/api/v1/portal/deals", headers=client(HANSE)).json())
    assert c.get("/api/v1/portal/me").status_code == 401  # no company chosen

    # invalid form: field errors, nothing stored
    bad = c.post("/api/v1/portal/applications", headers=client(BMA), json={"product": "LC", "details": {"amount": "abc"}})
    assert bad.status_code == 422
    fields = {e["field"] for e in bad.json()["detail"]["errors"]}
    assert {"beneficiary", "advising_bank_bic", "amount"} <= fields
    assert len(c.app.state.trade_engine.state.applications) == 1

    # restart: applications and their link to the deal are still there
    c2 = TestClient(create_app())
    st = c2.app.state.trade_engine.state
    assert st.applications[app["application_id"]].deal_ref == app["deal_ref"]
    assert c2.get(f"/api/v1/portal/files/{fid}", headers=client(BMA)).content == proforma


def test_application_kept_when_a_system_is_down_then_retried(db_url, monkeypatch):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", db_url)
    from auto_connect.dashboard import create_app
    from auto_connect.trade.systems import CustomerMasterSystem

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    original = CustomerMasterSystem.op_get_customer
    monkeypatch.setattr(CustomerMasterSystem, "op_get_customer",
                        lambda self, customer_id, as_of=None: (_ for _ in ()).throw(ConnectionError("customer master down")))
    r = c.post("/api/v1/portal/applications", headers=client(BMA), json={"product": "LC", "details": lc(today)})
    assert r.status_code == 200 and r.json()["status"] == "WAITING_FOR_SYSTEM" and r.json()["deal_ref"] is None
    app_id = r.json()["application_id"]
    assert not c.app.state.trade_engine.state.deals  # nothing half-created
    assert c.get("/api/v1/staff/overview").json()["counts"]["applications_waiting"] == 1

    monkeypatch.setattr(CustomerMasterSystem, "op_get_customer", original)
    c2 = TestClient(create_app())  # also proves the waiting application survived a restart
    again = c2.post(f"/api/v1/staff/applications/{app_id}/retry", headers={"X-Staff-Name": "Anna Weber"}).json()
    assert again["status"] == "PROCESSED" and again["deal_ref"]
    assert len(again["history"]) == 3 and "retried by Anna Weber" in again["history"][-1]["text"]
    assert c2.post(f"/api/v1/staff/applications/{app_id}/retry").status_code == 409  # never twice


def test_client_decision_and_sanctions_review_hidden_from_client():
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    demo = c.post("/api/v1/tf/demo").json()
    ref = demo["lc_discrepant_awaiting_client_waiver"]
    cid = c.app.state.trade_engine.state.deals[ref].customer_id
    d = c.get(f"/api/v1/portal/deals/{ref}", headers=client(cid)).json()
    act = d["actions"][0]
    assert act["kind"] == "waiver" and act["problems"]
    other = HANSE if cid != HANSE else BMA
    assert c.post(f"/api/v1/portal/deals/{ref}/decisions", headers=client(other),
                  json={"item_id": act["item_id"], "decision": "waive"}).status_code == 404
    done = c.post(f"/api/v1/portal/deals/{ref}/decisions", headers=client(cid),
                  json={"item_id": act["item_id"], "decision": "waive"})
    assert done.status_code == 200 and not done.json()["actions"]
    assert c.post(f"/api/v1/portal/deals/{ref}/decisions", headers=client(cid),
                  json={"item_id": act["item_id"], "decision": "waive"}).status_code == 409

    # a possible sanctions match: staff see why, the client only "Under review" (no tipping-off)
    g = {"beneficiary": {"name": "Black Sea Shadow Logistic FZE", "country": "AE"}, "guarantee_type": "PERFORMANCE",
         "amount": "60000", "currency": "EUR", "expiry_date": (today + timedelta(days=300)).isoformat(),
         "underlying_transaction": "Logistics contract BS-4", "underlying_contract_value": "600000"}
    r = c.post("/api/v1/portal/applications", headers=client(HANSE), json={"product": "GUARANTEE", "details": g}).json()
    assert r["deal_status_label"] == "Under review"
    view = c.get(f"/api/v1/portal/deals/{r['deal_ref']}", headers=client(HANSE)).json()
    text = str(view).lower()
    assert "sanction" not in text and "black sea shadow logistics" not in text.replace("logistic fze", "")
    tasks = c.get("/api/v1/staff/tasks").json()["decisions"]
    task = next(t for t in tasks if t["deal_ref"] == r["deal_ref"])
    assert task["title"] == "Possible sanctions match" and task["who"] == "Compliance" and task["approvals_needed"] == 2
    staff = c.get(f"/api/v1/staff/deals/{r['deal_ref']}").json()
    assert staff["decisions"] and any(s["tone"] == "amber" for s in staff["story"])


def test_roles_keep_clients_and_staff_apart(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CONNECT_USERS_FILE", str(tmp_path / "users.json"))
    from auto_connect.dashboard import create_app
    from auto_connect.security import UserDirectory

    users = UserDirectory()
    with pytest.raises(ValueError):
        users.add("bad", "Bad", ["CLIENT", "OPERATIONS"], customer_id=BMA)  # clients never get bank roles
    k_client = users.add("bma.cfo", "CFO BMA", ["CLIENT"], customer_id=BMA)
    k_ops = users.add("ops1", "Anna Weber", ["OPERATIONS"])
    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    ch, oh = {"X-API-Key": k_client}, {"X-API-Key": k_ops}
    assert c.get("/portal/login-info").json()["demo"] is False  # demo company picker is off with named users
    assert c.get("/api/v1/portal/me", headers={"X-Customer-Id": BMA}).status_code == 401  # no key: no access
    me = c.get("/api/v1/portal/me", headers={**ch, "X-Customer-Id": HANSE}).json()
    assert me["customer_id"] == BMA  # the header cannot switch company
    assert c.get("/api/v1/tf/deals", headers=ch).status_code == 403  # clients never see bank-wide data
    assert c.get("/api/v1/staff/overview", headers=ch).status_code == 403
    assert c.get("/api/v1/portal/me", headers=oh).status_code == 403  # staff do not use the client portal
    r = c.post("/api/v1/portal/applications", headers=ch, json={"product": "LC", "details": lc(today)})
    assert r.status_code == 200 and r.json()["submitted_by"] == "bma.cfo"
    assert c.get("/api/v1/staff/me", headers=oh).json()["name"] == "Anna Weber"
    s = c.post("/api/v1/staff/applications", headers=oh, json={"customer_id": RHEIN, "product": "LC", "details": lc(today, "50000")})
    assert s.status_code == 200 and s.json()["channel"] == "BANK_STAFF" and s.json()["submitted_by"] == "ops1"


def test_pages_and_assets_served():
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    for path, marker in [("/portal", "Trade Finance Online"), ("/bank", "Trade Finance Workbench"),
                         ("/assets/portal.js", "P.renderForm"), ("/assets/portal.css", ".timeline")]:
        r = c.get(path)
        assert r.status_code == 200 and marker in r.text
    assert "cdn." not in c.get("/portal").text and "cdn." not in c.get("/bank").text  # works inside a closed bank network


def test_client_edits_resubmits_withdraws_and_requests_changes(db_url, monkeypatch):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", db_url)
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    # an application the bank cannot process (unsupported currency) -> the client corrects it and sends it again
    r = c.post("/api/v1/portal/applications", headers=client(BMA), json={"product": "LC", "details": {**lc(today), "currency": "XYZ"}}).json()
    assert r["status"] == "REJECTED" and r["can_edit"] and "XYZ" in r["message"]
    app_id = r["application_id"]
    assert c.post(f"/api/v1/portal/applications/{app_id}/resubmit", headers=client(HANSE),
                  json={"details": lc(today)}).status_code == 404  # not your application
    bad = c.post(f"/api/v1/portal/applications/{app_id}/resubmit", headers=client(BMA), json={"details": {"amount": "x"}})
    assert bad.status_code == 422
    fixed = c.post(f"/api/v1/portal/applications/{app_id}/resubmit", headers=client(BMA), json={
        "details": lc(today, "90000"), "files": [{"filename": "po.txt", "content_base64": base64.b64encode(b"PO 4711").decode()}]}).json()
    assert fixed["status"] == "PROCESSED" and fixed["deal_ref"] and fixed["details"]["currency"] == "EUR" and not fixed["can_edit"]
    stored = c.app.state.trade_engine.state.applications[app_id]
    assert stored.history[-2]["previous"]["currency"] == "XYZ"  # the earlier version is kept
    assert len(stored.attachments) == 1 and c.app.state.trade_engine.state.deals[fixed["deal_ref"]].attachments
    assert c.post(f"/api/v1/portal/applications/{app_id}/resubmit", headers=client(BMA),
                  json={"details": lc(today)}).status_code == 409  # already a transaction: use a change request
    assert c.post(f"/api/v1/portal/applications/{app_id}/withdraw", headers=client(BMA)).status_code == 409

    # withdraw an application that could not be processed
    w = c.post("/api/v1/portal/applications", headers=client(BMA), json={"product": "LC", "details": {**lc(today), "currency": "XYZ"}}).json()
    wd = c.post(f"/api/v1/portal/applications/{w['application_id']}/withdraw", headers=client(BMA)).json()
    assert wd["status"] == "WITHDRAWN" and not wd["can_edit"]

    # change request on the issued LC: more money and more time - waits for the seller's consent
    ref = fixed["deal_ref"]
    d = c.get(f"/api/v1/portal/deals/{ref}", headers=client(BMA)).json()
    assert d["change"]["can_change"] and d["change"]["needs_other_party"]
    assert c.post(f"/api/v1/portal/deals/{ref}/changes", headers=client(BMA), json={"amount": "90000"}).status_code == 422
    assert c.post(f"/api/v1/portal/deals/{ref}/changes", headers=client(HANSE), json={"amount": "95000"}).status_code == 404
    new_exp = (today + timedelta(days=90)).isoformat()
    ch = c.post(f"/api/v1/portal/deals/{ref}/changes", headers=client(BMA),
                json={"amount": "100000", "expiry_date": new_exp, "reason": "Second machine"})
    assert ch.status_code == 200, ch.text
    h = ch.json()["change"]["history"][0]
    assert "increased by 10,000.00 EUR" in h["changes"] and new_exp in h["changes"] and "Second machine" in h["changes"]
    assert h["label"] == "Waiting for the other party to agree" and not ch.json()["change"]["can_change"]

    # a guarantee change applies straight away
    g = {"beneficiary": {"name": "Nordwind Energie GmbH", "country": "DE"}, "guarantee_type": "PERFORMANCE",
         "amount": "45000", "currency": "EUR", "expiry_date": (today + timedelta(days=365)).isoformat(),
         "underlying_transaction": "Supply contract NW-118", "underlying_contract_value": "450000"}
    gr = c.post("/api/v1/portal/applications", headers=client(RHEIN), json={"product": "GUARANTEE", "details": g}).json()
    gch = c.post(f"/api/v1/portal/deals/{gr['deal_ref']}/changes", headers=client(RHEIN), json={"amount": "40000"}).json()
    assert gch["amount"] == 40000.0 and gch["change"]["history"][0]["label"] == "In force"


def test_wrong_bic_goes_back_to_the_client(db_url, monkeypatch):
    monkeypatch.setenv("AUTO_CONNECT_DATABASE_URL", db_url)
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    eng = c.app.state.trade_engine
    today = eng.today
    # 1) caught at the door: no transaction, the client sees exactly which field is wrong
    r = c.post("/api/v1/portal/applications", headers=client(HANSE),
               json={"product": "LC", "details": {**lc(today), "advising_bank_bic": "hefdskfhidshf"}}).json()
    assert r["status"] == "NEEDS_CORRECTION" and r["can_edit"] and r["deal_ref"] is None
    assert r["problems"][0]["field"] == "advising_bank_bic" and "SWIFT code" in r["problems"][0]["message"]
    assert not eng.state.deals and not c.get("/api/v1/staff/tasks").json()["decisions"]  # no employee needed
    fixed = c.post(f"/api/v1/portal/applications/{r['application_id']}/resubmit", headers=client(HANSE),
                   json={"details": lc(today)}).json()
    assert fixed["status"] == "PROCESSED" and fixed["problems"] == []

    # 2) a case that was already stopped for a document checker (as before this check existed)
    monkeypatch.setattr(type(eng), "_client_mistakes", lambda self, product, terms: [])
    r = c.post("/api/v1/portal/applications", headers=client(HANSE),
               json={"product": "LC", "details": {**lc(today), "advising_bank_bic": "hefdskfhidshf"}}).json()
    stopped = r["deal_ref"]
    assert eng.state.deals[stopped].status == "ON_HOLD_DATA_QUALITY"
    task = next(t for t in c.get("/api/v1/staff/tasks").json()["decisions"] if t["deal_ref"] == stopped)
    assert task["can_return"] and task["return_label"] == "Send back to client to correct"
    back = c.post(f"/api/v1/staff/exceptions/{task['exception_id']}/return-to-client", headers={"X-Staff-Name": "Markus Huber"},
                  json={"note": "The seller's bank code has letters only - please ask the seller for the BIC."})
    assert back.status_code == 200, back.text
    a = back.json()
    assert a["status"] == "NEEDS_CORRECTION" and a["problems"][0]["field"] == "advising_bank_bic" and a["deal_ref"] is None
    assert "ask the seller" in a["message"]
    deal = eng.state.deals[stopped]
    assert deal.status == "RETURNED_FOR_CORRECTION" and not deal.open_exceptions
    assert not [e for e in eng.state.ledger if e.deal_ref == stopped]  # nothing was booked
    assert eng.state.exceptions[task["exception_id"]].status == "REJECTED"
    assert any(n.get("subject") == "Please correct your application" for n in eng.state.notifications)
    assert c.post(f"/api/v1/staff/exceptions/{task['exception_id']}/return-to-client", json={}).status_code == 409
    view = c.get(f"/api/v1/portal/deals/{stopped}", headers=client(HANSE)).json()
    assert view["status"] == "RETURNED_FOR_CORRECTION" and view["status_label"] == "Returned to client"
    again = c.post(f"/api/v1/portal/applications/{a['application_id']}/resubmit", headers=client(HANSE),
                   json={"details": lc(today)}).json()
    assert again["status"] == "PROCESSED" and again["deal_ref"] != stopped
    assert eng.state.deals[again["deal_ref"]].status == "ISSUED"

    # only before anything is booked, and only for data problems: a credit decision cannot be "sent back"
    big = c.post("/api/v1/portal/applications", headers=client(RHEIN), json={"product": "SURETY", "details": {
        "obligee": {"name": "Wohnbau Mitte GmbH", "country": "DE"}, "amount": "3000000", "currency": "EUR",
        "expiry_date": (today + timedelta(days=400)).isoformat(), "underlying_contract": "WM-77", "contract_value": "30000000"}}).json()
    credit = next(t for t in c.get("/api/v1/staff/tasks").json()["decisions"] if t["deal_ref"] == big["deal_ref"])
    assert not credit["can_return"]
    assert c.post(f"/api/v1/staff/exceptions/{credit['exception_id']}/return-to-client", json={}).status_code == 409


def test_impossible_lc_dates_go_back_to_the_client():
    from auto_connect.dashboard import create_app

    c = TestClient(create_app())
    today = c.app.state.trade_engine.today
    r = c.post("/api/v1/portal/applications", headers=client(HANSE),
               json={"product": "LC", "details": {**lc(today), "latest_shipment_date": "0026-01-21"}}).json()
    assert r["status"] == "NEEDS_CORRECTION" and r["problems"][0]["field"] == "latest_shipment_date"
    r = c.post("/api/v1/portal/applications", headers=client(HANSE), json={"product": "LC", "details": {
        **lc(today), "latest_shipment_date": (today + timedelta(days=30)).isoformat(),
        "expiry_date": (today + timedelta(days=31)).isoformat()}}).json()
    assert r["status"] == "NEEDS_CORRECTION" and r["problems"][0]["field"] == "expiry_date"


def test_bank_can_check_documents_again_before_the_client_decides(monkeypatch):
    from auto_connect.dashboard import create_app
    from auto_connect.trade import rules

    c = TestClient(create_app())
    eng = c.app.state.trade_engine
    today = eng.today
    ref = c.post("/api/v1/portal/applications", headers=client(BMA), json={"product": "LC", "details": lc(today)}).json()["deal_ref"]
    real = rules.examine_ucp600
    monkeypatch.setattr(rules, "examine_ucp600", lambda *a, **k: [rules._d("FAKE", "test", "wrong finding")])
    bl = BL_TEXT.replace("{d}", today.strftime("%d.%m.%Y"))
    files = [{"filename": "invoice.txt", "content_base64": base64.b64encode(INV_TEXT.encode()).decode()},
             {"filename": "bl.txt", "content_base64": base64.b64encode(bl.encode()).decode()}]
    up = c.post(f"/api/v1/tf/lc/{ref}/presentations/upload", json={"presenting_bank_bic": "BNCAITMMXXX",
                "presentation_date": today.isoformat(), "files": files}).json()
    pid = up["presentation_id"]
    assert eng.state.deals[ref].presentations[-1].status == "DISCREPANT_AWAITING_APPLICANT"
    monkeypatch.setattr(rules, "examine_ucp600", real)  # the rule is corrected
    r = c.post(f"/api/v1/staff/deals/{ref}/presentations/{pid}/re-examine", headers={"X-Staff-Name": "Markus Huber"})
    assert r.status_code == 200, r.text
    p = eng.state.deals[ref].presentations[-1]
    assert p.status == "PAID" and p.discrepancy_fee == 0 and not p.discrepancies
    assert c.post(f"/api/v1/staff/deals/{ref}/presentations/{pid}/re-examine").status_code == 409


INV_TEXT = ("COMMERCIAL INVOICE\nSeller: Milano Meccanica S.p.A., Via Roma 12, Genova\n"
            "Buyer: Bayerische Maschinenbau AG, Leopoldstrasse 210, Muenchen\nDescription of goods: CNC milling machines\n"
            "Total amount: EUR 85,000.00\n")
BL_TEXT = ("BILL OF LADING\nPort of loading: Genoa\nPort of discharge: Hamburg\nShipped on board: {d}\n"
           "Number of original B/L: THREE (3)\nSigned by: MSC as carrier\n")
