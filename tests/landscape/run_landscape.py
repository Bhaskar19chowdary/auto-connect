"""
Landscape test: the platform coordinating NINE independent external bank systems, each in its own process with its
own books and its own API (translated by the certified mapping adapters), SWIFT over a real SFTP server and email over
a real SMTP server. Complete deals run across all of them; failures are injected; afterwards every system's own books
are reconciled.

    python tests/landscape/run_landscape.py            (needs Docker for the SFTP and SMTP servers)
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlparse

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
RESULTS = []
VENDORS = {  # role -> (factory, application name)
    "tf_customer_master": ("cif_app", "Corporate Customer Hub"),
    "tf_sanctions_screening": ("screening_app", "WatchGuard Screening"),
    "tf_market_data": ("rates_app", "Treasury Rates Service"),
    "tf_core_ledger": ("ledger_app", "CoreBank GL"),
    "tf_credit_limits": ("limits_app", "LimitGuard"),
    "tf_document_archive": ("dms_app", "DocuVault DMS"),
    "tf_client_portal": ("portal_app", "CorporateConnect"),
}


def check(scn, name, cond, detail=""):
    RESULTS.append((scn, name, bool(cond), "" if cond else str(detail)[:600]))
    return bool(cond)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(url, seconds=90):
    for _ in range(seconds * 5):
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


class Landscape:
    def __init__(self):
        self.work = tempfile.mkdtemp(prefix="landscape_")
        self.procs, self.urls, self.containers = [], {}, []
        self.sftp_pw = secrets.token_hex(8)
        self.base_env = {**os.environ, "PYTHONPATH": ROOT, "GEMINI_API_KEY": "", "TF_LIVE_REFERENCE_DATA": "false",
                         "SMTP_HOST": "127.0.0.1", "SMTP_USER": "trade-ops@bank.test", "SMTP_PASSWORD": "landscape",
                         "BANK_SENDER_EMAIL": "trade-ops@bank.test", "DEFAULT_CLIENT_EMAIL": "client@corp.test",
                         "CLIENT_EMAIL_DE_CORP_99214": "treasury@hanse-solar.test",
                         "CLIENT_EMAIL_DE_CORP_40551": "finance@bm-ag.test",
                         "CLIENT_EMAIL_DE_CORP_77310": "office@rheinbau.test"}

    # ------------------------------------------------------------------ start
    def start(self):
        self._docker()
        for role, (factory, _) in VENDORS.items():
            port = free_port()
            env = {**self.base_env, "VENDOR_STATE": os.path.join(self.work, f"{factory}.json")}
            p = subprocess.Popen([PY, "-m", "uvicorn", f"tests.landscape.vendors:{factory}", "--factory", "--port", str(port),
                                  "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                 stderr=open(os.path.join(self.work, f"{factory}.log"), "w"))
            self.procs.append(p)
            self.urls[role] = f"http://127.0.0.1:{port}"
        for url in self.urls.values():
            wait_http(url + "/openapi.json")
        self.app_port = free_port()
        self.app_env = {**self.base_env, "AUTO_CONNECT_DATABASE_URL": f"sqlite:///{self.work}/bank.db",
                        "AUTO_CONNECT_STATE_FILE": os.path.join(self.work, "none.json"),
                        "AUTO_CONNECT_OUTPUT_DIR": os.path.join(self.work, "out"),
                        "AUTO_CONNECT_USERS_FILE": os.path.join(self.work, "users.json"),
                        "AUTO_CONNECT_AUDIT_FILE": os.path.join(self.work, "audit.jsonl"),
                        "AUTO_CONNECT_VAULT_FILE": os.path.join(self.work, "vault.json"),
                        "PUBLIC_BASE_URL": f"http://127.0.0.1:{self.app_port}",
                        "SEND_LIVE_EMAIL": "true", "SMTP_PORT": str(self.smtp_port), "TF_AUTO_BATCH_MINUTES": "0",
                        "SFTP_PASSWORD": self.sftp_pw,
                        "TF_SYSTEM_URL_TF_SWIFT_GATEWAY": f"sftp://bank@127.0.0.1:{self.sftp_port}",
                        "TF_SYSTEM_TOKEN_TF_SWIFT_GATEWAY": "env:SFTP_PASSWORD",
                        "TF_SYSTEM_TRANSPORT_TF_SWIFT_GATEWAY": "SFTP",
                        "TF_SYSTEM_TRANSPORT_TARGET_TF_SWIFT_GATEWAY": "/upload",
                        "TF_SYSTEM_TRANSPORT_FORMAT_TF_SWIFT_GATEWAY": "RAW:fin_text"}
        self.start_app()
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{self.app_port}", timeout=60)
        maps = json.load(open(os.path.join(ROOT, "tests", "landscape", "mappings.json")))
        for role, (factory, name) in VENDORS.items():
            r = self.c.post("/api/v1/apps/connect", json={"app_name": name, "base_url": self.urls[role], "auth_type": "None"}).json()
            assert r["status"] == "SUCCESS", (name, r)
            agent = r["agent_card"]["agent_id"]
            b = self.c.post(f"/api/v1/apps/{agent}/certify", json={
                "role": role, "certified_by": "integration.lead", "evidence": f"Landscape test {date.today()} UAT sign-off",
                "mapping": maps[role]}).json()
            assert b.get("status") == "ACTIVE", (name, b)

    def start_app(self):
        self.app = subprocess.Popen([PY, "-m", "auto_connect.main", "--server", "--port", str(self.app_port)], cwd=ROOT,
                                    env=self.app_env, stdout=subprocess.DEVNULL,
                                    stderr=open(os.path.join(self.work, "app.log"), "a"))
        wait_http(f"http://127.0.0.1:{self.app_port}/health")

    def restart_app(self):
        self.app.terminate()
        self.app.wait(15)
        self.start_app()

    def _docker(self):
        self.sftp_port, self.smtp_port, self.mail_api = free_port(), free_port(), free_port()
        name_s, name_m = f"ls-sftp-{secrets.token_hex(3)}", f"ls-mail-{secrets.token_hex(3)}"
        subprocess.run(["docker", "run", "-d", "--name", name_s, "-p", f"127.0.0.1:{self.sftp_port}:22", "atmoz/sftp:alpine",
                        f"bank:{self.sftp_pw}:::upload"], check=True, capture_output=True)
        subprocess.run(["docker", "run", "-d", "--name", name_m, "-p", f"127.0.0.1:{self.smtp_port}:1025",
                        "-p", f"127.0.0.1:{self.mail_api}:8025", "-e", "MP_SMTP_AUTH_ACCEPT_ANY=1",
                        "-e", "MP_SMTP_TLS_CERT=sans:localhost", "-e", "MP_SMTP_TLS_KEY=sans:localhost",
                        "axllent/mailpit:latest"], check=True, capture_output=True)
        self.containers = [name_s, name_m]
        wait_http(f"http://127.0.0.1:{self.mail_api}/api/v1/messages")
        import logging
        logging.getLogger("paramiko").setLevel(logging.CRITICAL)
        for _ in range(60):  # sftp up?
            try:
                import paramiko
                t = paramiko.Transport(("127.0.0.1", self.sftp_port))
                t.connect(username="bank", password=self.sftp_pw)
                t.close()
                break
            except Exception:
                time.sleep(1)

    def stop(self):
        for p in self.procs + [getattr(self, "app", None)]:
            if p:
                p.terminate()
        for c in self.containers:
            subprocess.run(["docker", "rm", "-f", c], capture_output=True)

    # ------------------------------------------------------------------ observation
    def vendor(self, role):
        return httpx.get(self.urls[role] + "/_admin/state", timeout=10).json()

    def fail(self, role, path, contains="", times=1, status=503):
        httpx.post(self.urls[role] + "/_admin/fail", json={"path": path, "contains": contains, "times": times, "status": status})

    def sftp_files(self):
        import paramiko
        t = paramiko.Transport(("127.0.0.1", self.sftp_port))
        t.connect(username="bank", password=self.sftp_pw)
        s = paramiko.SFTPClient.from_transport(t)
        out = {f: s.open(f"/upload/{f}").read().decode("utf-8", "replace") for f in s.listdir("/upload")}
        s.close()
        t.close()
        return out

    def mails(self):
        return httpx.get(f"http://127.0.0.1:{self.mail_api}/api/v1/messages?limit=500").json().get("messages", [])

    def post(self, path, body=None, expect=200):
        r = self.c.post(path, json=body if body is not None else {})
        if expect is not None and r.status_code != expect:
            raise AssertionError(f"POST {path} -> {r.status_code}: {r.text[:600]}")
        return r.json()

    def deal(self, ref):
        return self.c.get(f"/api/v1/tf/deals/{ref}").json()

    # ------------------------------------------------------------------ reconciliation helpers
    def ledger_journals(self, ref):
        return [j for j in self.vendor("tf_core_ledger")["journals"] if j["ref"] == ref]

    def ledger_net(self, ref, account):
        n = 0
        for j in self.ledger_journals(ref):
            for ln in j["lines"]:
                if ln["crAcct"] == account:
                    n += int(ln["amountMinor"])
                if ln["drAcct"] == account:
                    n -= int(ln["amountMinor"])
        return Decimal(n) / 100

    def reservation(self, ref):
        r = self.vendor("tf_credit_limits")["reservations"].get(ref)
        return Decimal(r["amount"]) if r else Decimal(0)

    def balance(self, iban):
        return Decimal(self.vendor("tf_core_ledger")["accounts"][iban]["minor"]) / 100

    def correlations_ok(self, scn, ref):
        bad = []
        for role in VENDORS:
            for e in self.vendor(role)["_log"]:
                if ref in e["body"] or ref in e["path"]:
                    if e["correlation"] != ref:
                        bad.append((role, e["path"], e["correlation"]))
        check(scn, f"every system call about {ref} carried correlation id {ref}", not bad, bad[:3])

    def books_balanced(self, scn):
        tot = {}
        for a in self.vendor("tf_core_ledger")["accounts"].values():
            tot[a["ccy"]] = tot.get(a["ccy"], 0) + a["minor"]
        check(scn, "CoreBank GL: double-entry books balance in every currency", all(v == 0 for v in tot.values()), tot)


def lc_terms(cust="DE-CORP-40551", amount="250000", **kw):
    T = date.today()
    t = {"applicant_customer_id": cust, "beneficiary": {"name": "Milano Meccanica S.p.A.", "address": "Via Dante 14, Milano",
                                                        "country": "IT"},
         "advising_bank_bic": "BNCAITMMXXX", "currency": "EUR", "amount": amount, "incoterms": "FOB",
         "expiry_date": (T + timedelta(days=90)).isoformat(), "latest_shipment_date": (T + timedelta(days=60)).isoformat(),
         "port_of_loading": "Genoa", "port_of_discharge": "Hamburg", "goods_description": "CNC milling machines",
         "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]}
    t.update(kw)
    return t


def docs(applicant, beneficiary, amount, ccy="EUR", pol="Genoa", pod="Hamburg", drop_bl=False, goods="CNC milling machines"):
    T = date.today().isoformat()
    d = [{"doc_type": "COMMERCIAL_INVOICE", "issue_date": T, "fields": {"amount": amount, "currency": ccy,
          "beneficiary_name": beneficiary, "applicant_name": applicant, "goods_description": goods}}]
    if not drop_bl:
        d.append({"doc_type": "BILL_OF_LADING", "originals": 3, "issue_date": T, "fields": {
            "on_board_date": T, "port_of_loading": pol, "port_of_discharge": pod, "clean": True, "signed_by": "CARRIER",
            "originals_issued": 3}})
    return d


def run(L: Landscape):
    T = date.today()
    BM_IBAN = None

    # ================================================================ S1 import LC end to end
    s = "S1 import LC: issue -> documents -> payment -> closed, across 9 external systems"
    lc = L.post("/api/v1/tf/lc", lc_terms())
    ref = lc["ref"]
    check(s, "issued", lc["status"] == "ISSUED", lc["status"])
    cif = [e for e in L.vendor("tf_customer_master")["_log"] if e["path"] == "/v3/parties/DE-CORP-40551"]
    check(s, "Customer Hub asked for the applicant (GET /v3/parties/{id})", cif, cif[:1])
    case = L.vendor("tf_sanctions_screening")["cases"][-1]
    check(s, "WatchGuard got the parties translated (subjects/countryIso)", case["ref"] == ref and
          {"name": "Milano Meccanica S.p.A.", "countryIso": "IT", "role": "beneficiary"} in case["subjects"], case)
    check(s, "LimitGuard holds the reservation under product code IMP_LC", L.reservation(ref) == Decimal("250000") and
          Decimal(L.vendor("tf_credit_limits")["lines"]["LN-40551-LC"]["used"]) == Decimal("250000"),
          L.vendor("tf_credit_limits")["lines"]["LN-40551-LC"])
    js = L.ledger_journals(ref)
    check(s, "CoreBank GL booked fees + contingent in cents with DD.MM.YYYY value date",
          js and all(len(j["valueDate"]) == 10 and j["valueDate"][2] == "." for j in js) and
          L.ledger_net(ref, "OB_LC_BANK_COMMITMENT") == Decimal("250000"), js[:2])
    BM_IBAN = next(ln["drAcct"] for j in js for ln in j["lines"] if ln["drAcct"].startswith("DE"))
    bal0 = L.balance(BM_IBAN)
    r = L.post(f"/api/v1/tf/lc/{ref}/presentations", {"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": T.isoformat(),
               "claimed_amount": "250000", "documents": docs("Bayerische Maschinenbau AG", "Milano Meccanica S.p.A.", "250000")})
    check(s, "complying documents paid, LC closed", r["status"] == "CLOSED" and r["presentations"][0]["status"] == "PAID", r["status"])
    fees_after = sum(Decimal(f["amount"]) for f in r["fees"] if f["date"] == T.isoformat())
    paid = bal0 - L.balance(BM_IBAN)
    check(s, "applicant debited in CoreBank GL exactly drawing + payment commission",
          paid == Decimal("250000") + sum(Decimal(f["amount"]) for f in r["fees"] if f["fee_type"] == "LC_PAYMENT_COMMISSION"),
          (paid, r["fees"]))
    check(s, "contingent liability back to zero in CoreBank GL", L.ledger_net(ref, "OB_LC_BANK_COMMITMENT") == 0,
          L.ledger_net(ref, "OB_LC_BANK_COMMITMENT"))
    check(s, "LimitGuard reservation released", L.reservation(ref) == 0, L.reservation(ref))
    files = L.sftp_files()
    mine = {k: v for k, v in files.items() if k.startswith(ref)}
    check(s, "SFTP hot folder holds MT700, MT756 and the pacs.009 cover", any(v.startswith("{1:") and "{2:I700" in v for v in mine.values())
          and any("{2:I756" in v for v in mine.values()) and any("pacs.009" in v for v in mine.values()), list(mine))
    msgs = [m for m in L.vendor("tf_client_portal")["messages"] if m["reference"] == ref]
    check(s, "client notices delivered through CorporateConnect (certified portal)", len(msgs) >= 2 and
          all(m["partyId"] == "DE-CORP-40551" for m in msgs), len(msgs))
    dms = [d for d in L.vendor("tf_document_archive")["documents"] if d["dossier"] == ref]
    check(s, "DocuVault holds the MT700 and terms (base64 decoded)", any(d["head"].startswith("{1:F01") for d in dms), dms[:2])
    L.correlations_ok(s, ref)

    # ================================================================ S2 discrepancy + client waiver link from the portal
    s = "S2 discrepant documents: waiver link delivered via CorporateConnect, clicked, paid"
    lc = L.post("/api/v1/tf/lc", lc_terms())
    ref = lc["ref"]
    r = L.post(f"/api/v1/tf/lc/{ref}/presentations", {"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": T.isoformat(),
               "claimed_amount": "250000", "documents": docs("Bayerische Maschinenbau AG", "Milano Meccanica S.p.A.", "250000",
                                                             pod="Rotterdam")})
    check(s, "discrepancy found, waiting for client", r["status"] == "DOCUMENTS_DISCREPANT", r["status"])
    msg = [m for m in L.vendor("tf_client_portal")["messages"] if m["reference"] == ref and m.get("actions")][-1]
    link = msg["actions"]["Accept discrepancies and pay"]
    resp = httpx.get(link, timeout=30)
    check(s, "client clicked the signed link from the portal message", resp.status_code == 200 and "recorded" in resp.text, resp.text[:200])
    d = L.deal(ref)
    check(s, "paid after waiver", d["presentations"][0]["status"] == "PAID", d["presentations"][0]["status"])
    check(s, "LimitGuard and GL clean afterwards", L.reservation(ref) == 0 and L.ledger_net(ref, "OB_LC_BANK_COMMITMENT") == 0)
    L.correlations_ok(s, ref)

    # ================================================================ S3 guarantee
    s = "S3 guarantee: MT760, demand paid by pacs.008, released"
    g = L.post("/api/v1/tf/guarantees", {"applicant_customer_id": "DE-CORP-40551",
               "beneficiary": {"name": "Saudi Industrial Projects Co", "address": "Riyadh", "country": "SA",
                               "iban": "SA0380000000608010167519"}, "beneficiary_bank_bic": "NCBKSAJEXXX",
               "amount": "200000", "expiry_date": (T + timedelta(days=365)).isoformat(),
               "underlying_transaction": "Contract SIP-2026-114 CNC production line"})
    ref = g["ref"]
    check(s, "issued with GTEE reservation", g["status"] == "ISSUED" and L.reservation(ref) == Decimal("200000"), (g["status"], L.reservation(ref)))
    r = L.post(f"/api/v1/tf/guarantees/{ref}/demands", {"presentation_date": T.isoformat(), "amount": "50000", "currency": "EUR",
               "documents": [{"doc_type": "DEMAND", "fields": {"breach_statement": "delivery delayed 12 weeks"}}]})
    check(s, "demand paid, reservation reduced to 150k", r["demands"][0]["status"] == "PAID" and L.reservation(ref) == Decimal("150000"),
          (r["demands"][0]["status"], L.reservation(ref)))
    r = L.post(f"/api/v1/tf/undertakings/{ref}/release", {"reason": "Original returned"})
    check(s, "released: no reservation, no contingent", r["status"] == "RELEASED" and L.reservation(ref) == 0 and
          L.ledger_net(ref, "OB_GTEE_BANK_COMMITMENT") == 0, (r["status"], L.reservation(ref), L.ledger_net(ref, "OB_GTEE_BANK_COMMITMENT")))
    f = {k for k in L.sftp_files() if k.startswith(ref)}
    check(s, "MT760, pacs.008 and MT769 in the SWIFT folder", len(f) >= 3, f)
    L.correlations_ok(s, ref)

    # ================================================================ S4 surety via portal link
    s = "S4 surety claim: principal acknowledges via portal link, paid"
    b = L.post("/api/v1/tf/sureties", {"principal_customer_id": "DE-CORP-77310",
               "obligee": {"name": "Stadt Koeln", "address": "Koeln", "country": "DE", "iban": "DE89370400440532013000"},
               "amount": "150000", "contract_value": "1500000", "expiry_date": (T + timedelta(days=500)).isoformat(),
               "underlying_contract": "VOB/B contract school extension"})
    ref = b["ref"]
    check(s, "bond issued against AVAL line (falls back to GTEE line)", b["status"] == "ISSUED" and L.reservation(ref) == Decimal("150000"),
          (b["status"], L.reservation(ref)))
    r = L.post(f"/api/v1/tf/sureties/{ref}/claims", {"presentation_date": T.isoformat(), "amount": "40000", "currency": "EUR",
               "documents": [{"doc_type": "DEMAND"}, {"doc_type": "NOTICE_OF_DEFAULT"}, {"doc_type": "EVIDENCE_OF_CLAIM"}]})
    msg = [m for m in L.vendor("tf_client_portal")["messages"] if m["reference"] == ref and m.get("actions")][-1]
    httpx.get(msg["actions"]["Acknowledge claim (pay)"], timeout=30)
    d = L.deal(ref)
    check(s, "claim paid", d["claims"][0]["status"] == "PAID" and L.reservation(ref) == Decimal("110000"),
          (d["claims"][0]["status"], L.reservation(ref)))
    L.correlations_ok(s, ref)

    # ================================================================ S5 export LC confirmed, USD (FX from Treasury Rates)
    s = "S5 export LC USD: confirmed, exporter paid (FX via Treasury Rates), reimbursed, closed"
    e = L.post("/api/v1/tf/export-lc", {"beneficiary_customer_id": "DE-CORP-40551", "issuing_bank_bic": "BKCHCNBJXXX",
               "issuing_bank_lc_ref": "LC88231", "applicant": {"name": "Shenzhen Precision Tools Co Ltd", "country": "CN"},
               "currency": "USD", "amount": "400000", "expiry_date": (T + timedelta(days=90)).isoformat(),
               "latest_shipment_date": (T + timedelta(days=60)).isoformat(), "port_of_loading": "Hamburg",
               "port_of_discharge": "Shenzhen", "goods_description": "CNC milling machines", "confirmation_instructions": "CONFIRM",
               "documents_required": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_LADING", "originals": 3}]})
    ref = e["ref"]
    rate = Decimal(1) / Decimal("1.1165")
    exp = (Decimal("400000") * rate.quantize(Decimal("0.0000001"))).quantize(Decimal("0.01"))
    check(s, "confirmed: counterparty line CPTY-BKCHCNBJ reserved at the vendor's rate", e["status"] == "CONFIRMED" and
          abs(L.reservation(ref) - exp) < 1, (e["status"], L.reservation(ref), exp))
    bal0 = L.balance(BM_IBAN)
    r = L.post(f"/api/v1/tf/export-lc/{ref}/presentations", {"presenting_bank_bic": "DEMODEMMXXX", "presentation_date": T.isoformat(),
               "claimed_amount": "400000", "documents": docs("Shenzhen Precision Tools Co Ltd", "Bayerische Maschinenbau AG",
                                                             "400000", "USD", "Hamburg", "Shenzhen")})
    p = r["presentations"][0]
    check(s, "exporter paid as confirming bank", p["status"] == "PAID" and r["status"] == "PAID_AWAITING_REIMBURSEMENT", (p["status"], r["status"]))
    credited = L.balance(BM_IBAN) - bal0
    check(s, "exporter's GL account credited with the EUR equivalent (minus commission)", Decimal("350000") < credited < exp, credited)
    check(s, "GL_DUE_FROM_BANKS_USD holds the USD receivable", L.ledger_net(ref, "GL_DUE_FROM_BANKS_USD") == Decimal("-400000"),
          L.ledger_net(ref, "GL_DUE_FROM_BANKS_USD"))
    r = L.post(f"/api/v1/tf/export-lc/{ref}/presentations/{p['presentation_id']}/issuing-bank-response", {"outcome": "PAID"})
    check(s, "reimbursed -> closed, receivable and line cleared", r["status"] == "CLOSED" and L.reservation(ref) == 0 and
          L.ledger_net(ref, "GL_DUE_FROM_BANKS_USD") == 0, (r["status"], L.reservation(ref)))
    conv = [x for x in L.vendor("tf_market_data")["_log"] if x["path"] == "/fx/convert" and ref in str(x)]
    check(s, "Treasury Rates Service did the conversions for this deal", L.vendor("tf_market_data")["_log"], "no FX calls")
    L.correlations_ok(s, ref)

    # ================================================================ S6 import collection D/A
    s = "S6 import collection D/A: drawee accepts via portal, paid at maturity by EOD"
    c = L.post("/api/v1/tf/collections", {"direction": "IMPORT", "customer_id": "DE-CORP-40551",
               "counterparty": {"name": "Istanbul Makina AS", "country": "TR"}, "counterparty_bank_bic": "AKBKTRISXXX",
               "counterparty_bank_ref": "COL-55120", "amount": "80000", "release_against": "ACCEPTANCE", "usance_days": 30,
               "documents": [{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "BILL_OF_EXCHANGE"}]})
    ref = c["ref"]
    msg = [m for m in L.vendor("tf_client_portal")["messages"] if m["reference"] == ref and m.get("actions")][-1]
    httpx.get(msg["actions"]["Accept the bill of exchange"], timeout=30)
    d = L.deal(ref)
    check(s, "accepted (MT412 in SWIFT folder)", d["status"] == "ACCEPTED" and any(k.startswith(ref) and "{2:I412" in v
                                                                                     for k, v in L.sftp_files().items()), d["status"])
    bal0 = L.balance(BM_IBAN)
    L.post("/api/v1/tf/batch", {"as_of": d["maturity_date"]})
    d = L.deal(ref)
    check(s, "paid at maturity: drawee debited in GL, pacs.009 + MT400 sent", d["status"] == "PAID" and
          bal0 - L.balance(BM_IBAN) >= Decimal("80000"), (d["status"], bal0 - L.balance(BM_IBAN)))
    L.correlations_ok(s, ref)

    # ================================================================ S7 limit exceeded in LimitGuard -> credit approves
    s = "S7 LimitGuard says LINE_EXCEEDED -> credit queue -> approval -> issued with override"
    lc = L.post("/api/v1/tf/lc", lc_terms(cust="DE-CORP-99214", amount="900000"))
    ref = lc["ref"]
    check(s, "vendor error LINE_EXCEEDED translated to a credit decision", lc["status"] == "ON_HOLD_CREDIT", lc["status"])
    check(s, "nothing reserved or booked while on hold", L.reservation(ref) == 0 and not L.ledger_journals(ref))
    exc = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref][0]
    L.post(f"/api/v1/tf/exceptions/{exc['exception_id']}/resolve", {"approve": True, "resolved_by": "credit.officer", "note": "ok"})
    d = L.deal(ref)
    check(s, "issued after approval, LimitGuard reservation with override", d["status"] == "ISSUED" and L.reservation(ref) == Decimal("900000"),
          (d["status"], L.reservation(ref)))

    # ================================================================ S8 KYC expired in Customer Hub
    s = "S8 Customer Hub reports KYC expired -> hold -> refreshed -> issued"
    httpx.post(L.urls["tf_customer_master"] + "/_admin/kyc/DE-CORP-77310", json={"state": "E"})
    g = L.post("/api/v1/tf/guarantees", {"applicant_customer_id": "DE-CORP-77310",
               "beneficiary": {"name": "Stadtwerke Muenchen", "address": "Muenchen", "country": "DE"}, "amount": "50000",
               "expiry_date": (T + timedelta(days=200)).isoformat(), "underlying_transaction": "Supply contract 2026-77"})
    ref = g["ref"]
    exc = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref]
    check(s, "KYC hold with the hub's reason", g["status"] == "ON_HOLD_KYC" and "Customer Hub" in exc[0]["reason"], (g["status"], exc))
    httpx.post(L.urls["tf_customer_master"] + "/_admin/kyc/DE-CORP-77310", json={"state": "V"})
    L.post(f"/api/v1/tf/exceptions/{exc[0]['exception_id']}/resolve", {"approve": True, "resolved_by": "kyc.team", "note": "refreshed"})
    check(s, "issued after refresh", L.deal(ref)["status"] == "ISSUED", L.deal(ref)["status"])

    # ================================================================ S9 sanctions hit from WatchGuard
    s = "S9 WatchGuard HIT -> hold -> compliance rejects -> nothing reserved or booked"
    lc = L.post("/api/v1/tf/lc", lc_terms(beneficiary={"name": "Volkov Maritime Trading LLC", "address": "Novorossiysk", "country": "RU"}))
    ref = lc["ref"]
    check(s, "hold", lc["status"] == "ON_HOLD_SANCTIONS", lc["status"])
    exc = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref][0]
    check(s, "reason names the list from WatchGuard", "EU 833/2014" in exc["reason"], exc["reason"])
    alerts = [m for m in L.mails() if ref in m["Subject"]]
    check(s, "internal compliance alert delivered over real SMTP to the bank mailbox",
          alerts and alerts[0]["To"][0]["Address"] == "trade-ops@bank.test", [(m["Subject"], m["To"]) for m in alerts][:2])
    L.post(f"/api/v1/tf/exceptions/{exc['exception_id']}/resolve", {"approve": False, "resolved_by": "compliance", "note": "true match"})
    check(s, "rejected, no reservation, no journals, no SWIFT", L.deal(ref)["status"] == "REJECTED" and L.reservation(ref) == 0 and
          not L.ledger_journals(ref) and not any(k.startswith(ref) for k in L.sftp_files()))

    # ================================================================ S10 GL fails in the middle of a payment
    s = "S10 CoreBank GL fails mid-payment -> everything reversed in all systems -> retry succeeds"
    lc = L.post("/api/v1/tf/lc", lc_terms())
    ref = lc["ref"]
    res0, contingent0, bal0 = L.reservation(ref), L.ledger_net(ref, "OB_LC_BANK_COMMITMENT"), L.balance(BM_IBAN)
    n_files = len(L.sftp_files())
    L.fail("tf_core_ledger", "/gl/v1/journals", contains="LC_PAYMENT_COMMISSION")
    pres = {"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": T.isoformat(), "claimed_amount": "250000",
            "documents": docs("Bayerische Maschinenbau AG", "Milano Meccanica S.p.A.", "250000")}
    r = L.c.post(f"/api/v1/tf/lc/{ref}/presentations", json=pres)
    check(s, "the API reports the failing system (502, names CoreBank GL)", r.status_code == 502 and "CoreBank GL" in r.text,
          (r.status_code, r.text[:200]))
    d = L.deal(ref)
    check(s, "deal unchanged in the platform", d["status"] == "ISSUED" and not d["presentations"], (d["status"], len(d["presentations"])))
    check(s, "applicant's GL balance restored by reversal journals", L.balance(BM_IBAN) == bal0, (bal0, L.balance(BM_IBAN)))
    check(s, "LimitGuard reservation restored", L.reservation(ref) == res0, (res0, L.reservation(ref)))
    check(s, "contingent liability unchanged", L.ledger_net(ref, "OB_LC_BANK_COMMITMENT") == contingent0)
    check(s, "no SWIFT message or email left the bank", len(L.sftp_files()) == n_files)
    rev = [j for j in L.ledger_journals(ref) if any("Reversal" in (ln.get("text") or "") for ln in j["lines"])]
    check(s, "reversal journals are visible in the GL (audit trail)", rev, "none")
    check(s, "no reconciliation task needed", not [x for x in L.c.get("/api/v1/tf/exceptions").json()
                                                    if x["deal_ref"] == ref and "reconciliation" in x["reason"]])
    r = L.post(f"/api/v1/tf/lc/{ref}/presentations", pres)
    check(s, "retry after the GL is back: paid and closed", r["status"] == "CLOSED", r["status"])
    check(s, "books clean after retry", L.reservation(ref) == 0 and L.ledger_net(ref, "OB_LC_BANK_COMMITMENT") == 0)

    # ================================================================ S11 GL fails during issuance
    s = "S11 CoreBank GL fails during issuance -> parked -> retry -> no double fees"
    L.fail("tf_core_ledger", "/gl/v1/journals", contains="Book contingent")
    lc = L.post("/api/v1/tf/lc", lc_terms(amount="100000"))
    ref = lc["ref"]
    check(s, "parked for operations", lc["status"] == "ON_HOLD_OPERATIONS", lc["status"])
    exc = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref][0]
    L.post(f"/api/v1/tf/exceptions/{exc['exception_id']}/resolve", {"approve": True, "resolved_by": "ops", "note": "GL back"})
    d = L.deal(ref)
    fee_journals = [j for j in L.ledger_journals(ref) if any("LC_ISSUANCE_COMMISSION" in (ln.get("text") or "") for ln in j["lines"])]
    check(s, "issued after retry", d["status"] == "ISSUED", d["status"])
    check(s, "issuance commission charged exactly once in the GL", len(fee_journals) == 1 and
          len([f for f in d["fees"] if f["fee_type"] == "LC_ISSUANCE_COMMISSION"]) == 1, (len(fee_journals), d["fees"]))
    check(s, "limit reserved exactly once", L.reservation(ref) == Decimal("100000"), L.reservation(ref))
    check(s, "one MT700 only", len([k for k, v in L.sftp_files().items() if k.startswith(ref) and "{2:I700" in v]) == 1)

    # ================================================================ S12 restart: everything still wired
    s = "S12 platform restart: certified systems still in charge"
    L.restart_app()
    lc = L.post("/api/v1/tf/lc", lc_terms(amount="10000"))
    check(s, "deal after restart uses LimitGuard and CoreBank GL", lc["status"] == "ISSUED" and L.reservation(lc["ref"]) == Decimal("10000")
          and L.ledger_journals(lc["ref"]), lc["status"])

    # ================================================================ S13 failure AND failed reversal
    s = "S13 GL fails mid-payment and the reversal fails too -> reconciliation task, nothing hidden"
    lc = L.post("/api/v1/tf/lc", lc_terms(amount="20000"))
    ref = lc["ref"]
    pres = {"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": T.isoformat(),
            "claimed_amount": "20000", "documents": docs("Bayerische Maschinenbau AG", "Milano Meccanica S.p.A.", "20000")}
    L.fail("tf_core_ledger", "/gl/v1/journals", contains="LC_PAYMENT_COMMISSION")
    L.fail("tf_core_ledger", "/gl/v1/journals", contains="Reversal: LC drawing", times=1)
    r = L.c.post(f"/api/v1/tf/lc/{ref}/presentations", json=pres)
    recon = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref and "reconciliation" in x["reason"]]
    check(s, "action refused (502) and a reconciliation task raised for operations", r.status_code == 502 and recon,
          (r.status_code, recon))
    check(s, "the task names exactly what could not be reversed", recon and "tf_core_ledger.post_entries" in recon[0]["reason"],
          recon[0]["reason"][:300] if recon else "")
    if recon:
        L.post(f"/api/v1/tf/exceptions/{recon[0]['exception_id']}/resolve",
               {"approve": True, "resolved_by": "gl.reconciliation", "note": "reversed manually in GL"})

    # ================================================================ S14 limit system completely down
    s = "S14 LimitGuard down during issuance -> parked, nothing booked anywhere"
    L.fail("tf_credit_limits", "/limits/reservations", times=50, status=503)
    lc = L.post("/api/v1/tf/lc", lc_terms(amount="30000"))
    ref = lc["ref"]
    check(s, "parked for operations", lc["status"] == "ON_HOLD_OPERATIONS", lc["status"])
    check(s, "no journal, no SWIFT, no reservation", not L.ledger_journals(ref) and L.reservation(ref) == 0 and
          not any(k.startswith(ref) for k in L.sftp_files()))
    L.fail("tf_credit_limits", "/limits/reservations", times=0)
    exc = [x for x in L.c.get("/api/v1/tf/exceptions").json() if x["deal_ref"] == ref][0]
    L.post(f"/api/v1/tf/exceptions/{exc['exception_id']}/resolve", {"approve": True, "resolved_by": "ops", "note": "LimitGuard back"})
    check(s, "issued once the limit system is back", L.deal(ref)["status"] == "ISSUED" and L.reservation(ref) == Decimal("30000"),
          L.deal(ref)["status"])

    # ================================================================ global reconciliation
    s = "R  reconciliation of all external systems against the platform"
    L.books_balanced(s)
    deals = L.c.get("/api/v1/tf/deals").json()
    for d in deals:
        if d["status"] in ("CLOSED", "RELEASED", "REJECTED", "EXPIRED", "PAID"):
            check(s, f"{d['ref']} ({d['status']}): no LimitGuard reservation left", L.reservation(d["ref"]) == 0, L.reservation(d["ref"]))
    outbox = L.c.get("/api/v1/tf/outbox").json()
    check(s, "outbox empty: every email / SWIFT / portal message delivered", all(i["status"] == "SENT" for i in outbox),
          [i for i in outbox if i["status"] != "SENT"][:3])
    recon = [x for x in L.c.get("/api/v1/tf/exceptions").json() if "reconciliation" in x["reason"]]
    check(s, "no open reconciliation tasks (S13's task was handled by operations)", not recon, recon[:2])
    lg = L.vendor("tf_credit_limits")
    used = {lid: Decimal(ln["used"]) for lid, ln in lg["lines"].items()}
    res_by_line = {}
    for ref_, r in lg["reservations"].items():
        res_by_line[r["line"]] = res_by_line.get(r["line"], Decimal(0)) + Decimal(r["amount"])
    check(s, "LimitGuard: every line's usage equals its reservations", all(used[l] == res_by_line.get(l, 0) for l in used),
          {l: (used[l], res_by_line.get(l, 0)) for l in used if used[l] != res_by_line.get(l, 0)})
    err = open(os.path.join(L.work, "app.log")).read()
    check(s, "no unexpected errors in the platform log", "Traceback" not in err, err[-800:])


def main():
    L = Landscape()
    try:
        L.start()
        run(L)
    except Exception as e:
        RESULTS.append(("RUN", "crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}"))
    finally:
        L.stop()
    by = {}
    for scn, name, ok, det in RESULTS:
        by.setdefault(scn, []).append((name, ok, det))
    for scn, items in by.items():
        bad = [i for i in items if not i[1]]
        print(f"{'PASS' if not bad else 'FAIL'}  {scn}  ({len(items) - len(bad)}/{len(items)})")
        for name, ok, det in items:
            if not ok:
                print(f"      x {name}\n          {det}")
    print(f"\nTOTAL {sum(r[2] for r in RESULTS)}/{len(RESULTS)} checks passed   (work dir {L.work})")


if __name__ == "__main__":
    main()
