"""Message-based connections: shared hot folder (incl. SWIFT raw files through the outbox), SFTP and IBM MQ.
SFTP / IBM MQ run against real servers when TEST_SFTP_URL / TEST_IBMMQ_URL are set (e.g. docker containers)."""
import asyncio
import glob
import json
import os
import threading
import time
from datetime import timedelta
from decimal import Decimal

import pytest

from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.models import VaultCredential
from auto_connect.trade.engine import TradeFinanceEngine, WorkflowError
from auto_connect.trade.models import DocumentRequirement, LCTerms, Party
from auto_connect.trade.state import StateStore
from auto_connect.vault import ZeroTrustVault


def lc_terms(today):
    return LCTerms(applicant_customer_id="DE-CORP-40551", beneficiary=Party(name="Milano Meccanica S.p.A.", country="IT"),
                   advising_bank_bic="BNCAITMMXXX", amount=Decimal("100000"), expiry_date=today + timedelta(days=60),
                   latest_shipment_date=today + timedelta(days=30), port_of_loading="Genoa", port_of_discharge="Hamburg",
                   incoterms="FOB", goods_description="machines",
                   documents_required=[DocumentRequirement(doc_type="COMMERCIAL_INVOICE"),
                                       DocumentRequirement(doc_type="BILL_OF_LADING", originals=3)])


def test_swift_hot_folder_through_the_outbox(tmp_path, monkeypatch):
    hot = tmp_path / "saa_hotfolder"
    monkeypatch.setenv("TF_SYSTEM_TRANSPORT_TF_SWIFT_GATEWAY", "FILE")
    monkeypatch.setenv("TF_SYSTEM_TRANSPORT_TARGET_TF_SWIFT_GATEWAY", str(hot))
    monkeypatch.setenv("TF_SYSTEM_TRANSPORT_FORMAT_TF_SWIFT_GATEWAY", "RAW:fin_text")
    vault = ZeroTrustVault()
    eng = TradeFinanceEngine(StateStore(path=""), vault, A2AEcosystemMesh(), UniversalAPIExecutor(vault))
    lc = asyncio.run(eng.apply_lc(lc_terms(eng.today)))
    files = glob.glob(str(hot / f"{lc.ref}_release_message_*.fin"))
    assert lc.status == "ISSUED" and len(files) == 1
    assert open(files[0]).read().startswith("{1:F01") and ":20:" + lc.ref in open(files[0]).read()
    assert all(i["status"] == "SENT" for i in eng.state.outbox.values())
    # a refused action never reaches the SWIFT folder
    with pytest.raises(WorkflowError):
        asyncio.run(eng.amend_lc(lc.ref, {"amount_delta": "-500000"}))
    assert len(os.listdir(hot)) == 1


def test_request_reply_over_shared_folders(tmp_path):
    out, rep = tmp_path / "out", tmp_path / "in"
    rep.mkdir()
    stop = threading.Event()

    def core_system():  # the bank system on the other side: answers each request file
        while not stop.is_set():
            for f in glob.glob(str(out / "*.json")):
                req = json.load(open(f))
                os.remove(f)
                answer = {"status_code": 200, "data": {"account_id": req["params"]["account_id"], "free_balance": "750000.00"}}
                with open(rep / f"{req['request_id']}.json", "w") as w:
                    json.dump(answer, w)
            time.sleep(0.05)
    threading.Thread(target=core_system, daemon=True).start()
    try:
        vault = ZeroTrustVault()
        vault.store_credential(VaultCredential(app_id="core", base_url="file://", auth_type="None", transport="FILE",
                                               transport_target=str(out), transport_reply=str(rep), reply_timeout_seconds=5))
        r = asyncio.run(UniversalAPIExecutor(vault).execute_dynamic_call(
            "core", "/api/v1/tf_core_ledger/account_balance", "POST", {"account_id": "DE89"}, "TRADE_FINANCE"))
        assert r["status_code"] == 200 and r["data"]["free_balance"] == "750000.00"
        vault.store_credential(VaultCredential(app_id="slow", base_url="file://", auth_type="None", transport="FILE",
                                               transport_target=str(tmp_path / "nobody"), transport_reply=str(rep),
                                               reply_timeout_seconds=0.5))
        r = asyncio.run(UniversalAPIExecutor(vault).execute_dynamic_call("slow", "/x/op", "POST", {}, "TRADE_FINANCE"))
        assert r["status_code"] == 504 and r.get("network_error")
    finally:
        stop.set()


@pytest.mark.skipif(not os.environ.get("TEST_SFTP_URL"), reason="set TEST_SFTP_URL=sftp://user@host:port (password in TEST_SFTP_PASSWORD)")
def test_sftp_one_way_and_request_reply(monkeypatch):
    import paramiko
    from urllib.parse import urlparse

    url = os.environ["TEST_SFTP_URL"]
    vault = ZeroTrustVault()
    vault.store_credential(VaultCredential(app_id="saa", base_url=url, auth_type="Bearer", token="env:TEST_SFTP_PASSWORD",
                                           transport="SFTP", transport_target="/upload", transport_format="RAW:fin_text"))
    r = asyncio.run(UniversalAPIExecutor(vault).execute_dynamic_call(
        "saa", "/api/v1/tf_swift_gateway/release_message", "POST", {"fin_text": "{1:F01DEMODEMMXXX}{4:\n:20:TEST\n-}"}, "TRADE_FINANCE"))
    assert r["status_code"] == 202
    u = urlparse(url)
    t = paramiko.Transport((u.hostname, u.port))
    t.connect(username=u.username, password=os.environ["TEST_SFTP_PASSWORD"])
    sftp = paramiko.SFTPClient.from_transport(t)
    name = os.path.basename(r["data"]["destination"])
    assert name in sftp.listdir("/upload") and sftp.open(f"/upload/{name}").read().startswith(b"{1:F01")
    sftp.close()
    t.close()


@pytest.mark.skipif(not os.environ.get("TEST_IBMMQ_URL"), reason="set TEST_IBMMQ_URL, TEST_IBMMQ_USER, TEST_IBMMQ_CA")
def test_ibm_mq_request_reply():
    """The 'bank system' listens on DEV.QUEUE.1 and answers on DEV.QUEUE.2 with the request's correlation id."""
    import httpx

    base, user, ca = os.environ["TEST_IBMMQ_URL"], os.environ["TEST_IBMMQ_USER"], os.environ["TEST_IBMMQ_CA"]
    auth = tuple(user.split(":", 1))
    stop = threading.Event()

    def responder():
        with httpx.Client(verify=ca, auth=auth, headers={"ibm-mq-rest-csrf-token": "x"}, timeout=20) as c:
            while not stop.is_set():
                r = c.delete(f"{base}/ibmmq/rest/v2/messaging/qmgr/QM1/queue/DEV.QUEUE.1/message", params={"wait": 1000})
                if r.status_code != 200:
                    continue
                req = json.loads(r.text)
                c.post(f"{base}/ibmmq/rest/v2/messaging/qmgr/QM1/queue/DEV.QUEUE.2/message",
                       content=json.dumps({"status_code": 200, "data": {"customer_id": req["params"]["customer_id"],
                                                                        "kyc_ok": True}}),
                       headers={"Content-Type": "text/plain", "ibm-mq-md-correlationId": r.headers["ibm-mq-md-correlationId"]})
    threading.Thread(target=responder, daemon=True).start()
    try:
        vault = ZeroTrustVault()
        vault.store_credential(VaultCredential(app_id="cif", base_url=base, auth_type="Basic", token=user, ca_bundle_file=ca,
                                               transport="IBM_MQ", transport_target="QM1/DEV.QUEUE.1",
                                               transport_reply="QM1/DEV.QUEUE.2", reply_timeout_seconds=15))
        r = asyncio.run(UniversalAPIExecutor(vault).execute_dynamic_call(
            "cif", "/api/v1/tf_customer_master/get_customer", "POST", {"customer_id": "DE-CORP-40551"}, "TRADE_FINANCE"))
        assert r["status_code"] == 200 and r["data"] == {"customer_id": "DE-CORP-40551", "kyc_ok": True}
    finally:
        stop.set()
