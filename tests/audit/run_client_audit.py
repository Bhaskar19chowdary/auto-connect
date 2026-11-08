"""
Independent audit: clients fill in applications and upload real documents (PDF / scanned image), with the kinds
of mistakes real clients make. Every expected reaction is checked, including the emails that actually arrive.
Findings are recorded as found - nothing is fixed during the audit.
"""
import base64, json, os, secrets, socket, subprocess, sys, tempfile, time, traceback
from datetime import date, timedelta
import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable
T = date.today()
D = lambda n: (T + timedelta(days=n))
R = []  # (area, check, ok, detail)


def check(area, name, ok, detail=""):
    R.append((area, name, bool(ok), "" if ok else str(detail)[:500]))
    return bool(ok)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


# ------------------------------------------------------------------ documents (real PDFs)
def pdf(lines):
    def esc(t): return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 50 790 Td 15 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offs)
    return (out + f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF").encode("latin-1")


def f(name, data):
    return {"filename": name, "content_base64": base64.b64encode(data).decode()}


def scan_of(pdf_bytes, work):
    p = os.path.join(work, f"s{secrets.token_hex(3)}.pdf"); open(p, "wb").write(pdf_bytes)
    subprocess.run(["pdftoppm", "-r", "200", "-png", "-singlefile", p, p[:-4]], check=True)
    return open(p[:-4] + ".png", "rb").read()


def invoice(amount="EUR 300,000.00", seller="Milano Meccanica S.p.A.", buyer="Bayerische Maschinenbau AG",
            goods="CNC milling machines model MM-500", date_=None, wording="standard"):
    d = (date_ or T).strftime("%d.%m.%Y")
    if wording == "standard":
        return pdf(["COMMERCIAL INVOICE", "Invoice No: MM-2026-0815", f"Date: {d}", f"Seller: {seller}", f"Buyer: {buyer}",
                    f"Description of goods: {goods}", "Terms: FOB Genoa", f"Total amount: {amount}", "Country of origin: Italy"])
    # how many real exporters write it
    return pdf(["INVOICE", f"Invoice Date {d}", f"Exporter: {seller}", f"Consignee: {buyer}", f"Goods: {goods}",
                f"Invoice Value: {amount}", "Payment: Letter of Credit"])


def bill_of_lading(ship=None, pol="Genoa, Italy", pod="Hamburg, Germany", clean=True, originals="THREE (3)", wording="standard",
                   imo="IMO 9839131"):
    s = ship or T
    claus = [] if clean else ["Remarks: 2 crates damaged, packing insufficient"]
    if wording == "standard":
        return pdf(["BILL OF LADING", "Shipper: Milano Meccanica S.p.A.", f"Vessel: MSC AURORA {imo}", f"Port of loading: {pol}",
                    f"Port of discharge: {pod}", f"Shipped on board: {s.strftime('%d %B %Y')}", f"Number of original B/L: {originals}",
                    "Signed by: Mediterranean Shipping Company as carrier", f"Date of issue: {s.strftime('%d.%m.%Y')}"] + claus)
    return pdf(["OCEAN BILL OF LADING", "Shipper: Milano Meccanica S.p.A.", f"Ocean Vessel: MSC AURORA {imo}",
                f"Port of Loading: {pol}", f"Port of Discharge: {pod}", f"Date of Shipment: {s.strftime('%B %d, %Y')}",
                f"No. of Original B(s)/L: {originals}", "As Carrier: Mediterranean Shipping Company"] + claus)


def insurance(value="EUR 330,000.00"):
    return pdf(["INSURANCE CERTIFICATE", f"Date of issue: {T.strftime('%d.%m.%Y')}", f"Insured value: {value}",
                "Voyage from: Genoa", "Voyage to: Hamburg", "Risks covered: Institute Cargo Clauses (A)"])


def packing():
    return pdf(["PACKING LIST", f"Date: {T.strftime('%d.%m.%Y')}", "Description of goods: CNC milling machines model MM-500",
                "Gross weight: 18,400 kg", "Number of packages: 4"])


# ------------------------------------------------------------------ platform + mail server
class Env:
    def __init__(self):
        self.work = tempfile.mkdtemp(prefix="audit_")
        self.smtp, self.mapi, self.port = free_port(), free_port(), free_port()
        self.mail = f"audit-mail-{secrets.token_hex(3)}"
        subprocess.run(["docker", "run", "-d", "--name", self.mail, "-p", f"127.0.0.1:{self.smtp}:1025", "-p",
                        f"127.0.0.1:{self.mapi}:8025", "-e", "MP_SMTP_AUTH_ACCEPT_ANY=1", "-e", "MP_SMTP_TLS_CERT=sans:localhost",
                        "-e", "MP_SMTP_TLS_KEY=sans:localhost", "axllent/mailpit:latest"], check=True, capture_output=True)
        env = {**os.environ, "PYTHONPATH": ROOT, "GEMINI_API_KEY": "", "TF_LIVE_REFERENCE_DATA": "true",
               "REFERENCE_DATA_DIR": os.environ.get("REFERENCE_DATA_DIR", os.path.join(tempfile.gettempdir(), "auto_connect_refdata")),
               "AUTO_CONNECT_DATABASE_URL": f"sqlite:///{self.work}/bank.db", "AUTO_CONNECT_STATE_FILE": f"{self.work}/x.json",
               "AUTO_CONNECT_OUTPUT_DIR": f"{self.work}/out", "AUTO_CONNECT_VAULT_FILE": f"{self.work}/vault.json",
               "AUTO_CONNECT_USERS_FILE": f"{self.work}/users.json", "AUTO_CONNECT_AUDIT_FILE": f"{self.work}/audit.jsonl",
               "PUBLIC_BASE_URL": f"http://127.0.0.1:{self.port}", "TF_AUTO_BATCH_MINUTES": "0",
               "SEND_LIVE_EMAIL": "true", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(self.smtp), "SMTP_USER": "trade-ops@bank.test",
               "SMTP_PASSWORD": "audit", "BANK_SENDER_EMAIL": "trade-ops@bank.test", "DEFAULT_CLIENT_EMAIL": "client@corp.test",
               "CLIENT_EMAIL_DE_CORP_99214": "treasury@hanse-solar.test", "CLIENT_EMAIL_DE_CORP_40551": "finance@bm-ag.test",
               "CLIENT_EMAIL_DE_CORP_77310": "office@rheinbau.test"}
        self.app = subprocess.Popen([PY, "-m", "auto_connect.main", "--server", "--port", str(self.port)], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=open(f"{self.work}/app.log", "w"))
        self.c = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=120)
        for _ in range(300):
            try:
                if self.c.get("/ready").status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.3)

    def post(self, path, body):
        r = self.c.post(path, json=body)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)

    def mails(self, ref=None, to=None):
        msgs = httpx.get(f"http://127.0.0.1:{self.mapi}/api/v1/messages?limit=500").json().get("messages", [])
        out = []
        for m in msgs:
            if ref and ref not in m["Subject"]:
                continue
            if to and to not in [x["Address"] for x in m["To"]]:
                continue
            body = httpx.get(f"http://127.0.0.1:{self.mapi}/api/v1/message/{m['ID']}").json().get("Text", "")
            out.append({"subject": m["Subject"], "to": [x["Address"] for x in m["To"]], "body": body})
        return out

    def exc(self, ref):
        return [e for e in self.c.get("/api/v1/tf/exceptions").json() if e["deal_ref"] == ref]

    def stop(self):
        self.app.terminate()
        subprocess.run(["docker", "rm", "-f", self.mail], capture_output=True)


def lc_app(**kw):
    a = {"applicant_customer_id": "DE-CORP-40551",
         "beneficiary": {"name": "Milano Meccanica S.p.A.", "address": "Via Dante 14, 20121 Milano", "country": "IT"},
         "advising_bank_bic": "BNCAITMMXXX", "currency": "EUR", "amount": "300000", "tolerance_plus_pct": "0",
         "expiry_date": D(90).isoformat(), "latest_shipment_date": D(60).isoformat(), "port_of_loading": "Genoa, Italy",
         "port_of_discharge": "Hamburg, Germany", "incoterms": "CIF", "incoterms_place": "Hamburg",
         "goods_description": "CNC milling machines model MM-500",
         "documents_required": [{"doc_type": "COMMERCIAL_INVOICE", "originals": 1}, {"doc_type": "BILL_OF_LADING", "originals": 3},
                                {"doc_type": "INSURANCE_DOCUMENT", "originals": 1}, {"doc_type": "PACKING_LIST", "originals": 1}]}
    a.update(kw)
    return a


def upload(E, ref, files, claimed=None):
    body = {"presenting_bank_bic": "BNCAITMMXXX", "presentation_date": T.isoformat(), "files": files}
    if claimed:
        body["claimed_amount"] = claimed
    return E.post(f"/api/v1/tf/lc/{ref}/presentations/upload", body)


def codes(deal):
    return [d["code"] for d in deal["presentations"][-1]["discrepancies"]]


def run(E):
    W = E.work
    # ============================================================= LC
    A = "LC-1 correct application + correct documents (standard wording)"
    s, lc = E.post("/api/v1/tf/lc", lc_app())
    ref = lc["ref"]
    check(A, "application accepted and issued", lc["status"] == "ISSUED", lc["status"])
    s, up = upload(E, ref, [f("invoice.pdf", invoice()), f("bl.pdf", bill_of_lading()), f("ins.pdf", insurance()),
                            f("pl.pdf", packing())])
    d = up["deal"]
    check(A, "documents read without needing review", not up["reading"]["needs_review"], up["reading"]["needs_review"])
    check(A, "paid", d["presentations"][-1]["status"] == "PAID", (d["presentations"][-1]["status"], codes(d)))
    m = E.mails(ref, "finance@bm-ag.test")
    check(A, "client got 'issued' and 'debit advice' emails", any("issued" in x["subject"] for x in m) and
          any("Debit advice" in x["subject"] for x in m), [x["subject"] for x in m])

    A = "LC-2 application full of mistakes"
    s, lc = E.post("/api/v1/tf/lc", lc_app(advising_bank_bic="UNICRED", latest_shipment_date=D(120).isoformat(),
                                           documents_required=[{"doc_type": "COMMERCIAL_INVOICE"}, {"doc_type": "PACKING_LIST"}],
                                           amount="300000"))
    ref = lc["ref"]
    ex = E.exc(ref)
    reason = ex[0]["reason"] if ex else ""
    check(A, "application stopped for correction", lc["status"] == "ON_HOLD_DATA_QUALITY", lc["status"])
    for code, what in [("ADVISING_BIC_INVALID", "invalid bank code"), ("SHIPMENT_AFTER_EXPIRY", "shipment after expiry"),
                       ("INSURANCE_NOT_CALLED_FOR", "CIF without insurance"), ("TRANSPORT_DOCUMENT_MISSING", "no transport document")]:
        check(A, f"detects: {what}", code in reason, reason)
    m0 = E.mails(ref, "finance@bm-ag.test")
    check(A, "client's email also explains the missing transport document", m0 and "transport document" in m0[0]["body"],
          [x["body"][:300] for x in m0])
    m = E.mails(ref, "finance@bm-ag.test")
    check(A, "client emailed with the list of problems", m and "needs correction" in m[0]["subject"] and "BIC" in m[0]["body"],
          [(x["subject"], x["body"][:200]) for x in m])
    check(A, "nothing issued, no SWIFT, no limit used", not lc["swift_message_ids"] and lc["limit_utilisation_eur"] in ("0", "0.00"),
          (lc["swift_message_ids"], lc["limit_utilisation_eur"]))

    A = "LC-3 documents with many mistakes"
    s, lc = E.post("/api/v1/tf/lc", lc_app(latest_shipment_date=D(-0).isoformat() if False else D(60).isoformat()))
    ref = lc["ref"]
    s, up = upload(E, ref, [f("invoice.pdf", invoice(amount="EUR 325,000.00", seller="Milano Meccanica SRL",
                                                    buyer="Bavarian Machines Ltd", goods="Used lathes")),
                            f("bl.pdf", bill_of_lading(pod="Rotterdam, Netherlands", clean=False, originals="TWO (2)")),
                            f("ins.pdf", insurance("EUR 200,000.00"))], claimed="325000")
    d = up["deal"]
    c = codes(d)
    for code, what in [("INVOICE_EXCEEDS_CREDIT", "invoice above LC amount"), ("DRAWING_EXCEEDS_AVAILABLE", "drawing above LC"),
                       ("GOODS_DESCRIPTION", "goods do not match"), ("INVOICE_NOT_TO_APPLICANT", "invoice to wrong buyer"),
                       ("BL_PORT_OF_DISCHARGE", "wrong port of discharge"), ("BL_NOT_CLEAN", "claused (dirty) B/L"),
                       ("ORIGINALS_BILL_OF_LADING", "B/L full set missing (2 of 3 originals)"), ("INSURANCE_UNDERINSURED", "underinsured"),
                       ("MISSING_PACKING_LIST", "packing list missing")]:
        check(A, f"detects: {what}", any(code in x for x in c), c)
    check(A, "beneficiary name variation (S.p.A. vs SRL) noticed", any(x in c for x in ("INVOICE_NOT_ISSUED_BY_BENEFICIARY",
                                                                                     "INVOICE_ISSUER_LEGAL_FORM")), c)
    check(A, "not paid - waits for the client's decision", d["presentations"][-1]["status"] == "DISCREPANT_AWAITING_APPLICANT",
          d["presentations"][-1]["status"])
    m = [x for x in E.mails(ref, "finance@bm-ag.test") if "Discrepant" in x["subject"]]
    check(A, "client emailed the discrepancies with accept / refuse links", m and "/portal/decision" in m[0]["body"]
          and "port" in m[0]["body"].lower(), [x["body"][:300] for x in m])

    A = "LC-3b everything correct except the seller's legal form (SRL instead of S.p.A.)"
    s, lc = E.post("/api/v1/tf/lc", lc_app())
    ref = lc["ref"]
    s, up = upload(E, ref, [f("invoice.pdf", invoice(seller="Milano Meccanica SRL")), f("bl.pdf", bill_of_lading()),
                            f("ins.pdf", insurance()), f("pl.pdf", packing())])
    c = codes(up["deal"])
    check(A, "only the legal-form discrepancy is reported", c == ["INVOICE_ISSUER_LEGAL_FORM"], c)
    check(A, "not paid automatically", up["deal"]["presentations"][-1]["status"] == "DISCREPANT_AWAITING_APPLICANT",
          up["deal"]["presentations"][-1]["status"])

    A = "LC-4 documents in common real-world wording (not the reader's preferred labels)"
    s, lc = E.post("/api/v1/tf/lc", lc_app())
    ref = lc["ref"]
    s, up = upload(E, ref, [f("invoice.pdf", invoice(wording="real")), f("bl.pdf", bill_of_lading(wording="real")),
                            f("ins.pdf", insurance()), f("pl.pdf", packing())])
    rd = {x["doc_type"]: x for x in up["reading"]["documents"]}
    inv, bl = rd.get("COMMERCIAL_INVOICE", {}), rd.get("BILL_OF_LADING", {})
    check(A, "invoice recognised", inv, list(rd))
    check(A, "invoice amount read from 'Invoice Value'", inv.get("fields", {}).get("amount") == "300000.00", inv.get("fields"))
    check(A, "buyer read from 'Consignee'", inv.get("fields", {}).get("applicant_name"), inv.get("fields"))
    check(A, "B/L recognised ('OCEAN BILL OF LADING')", bl, list(rd))
    check(A, "shipment date read from 'Date of Shipment'", bl.get("fields", {}).get("on_board_date") == T.isoformat(), bl.get("fields"))
    check(A, "paid automatically - no manual step needed", up["deal"]["presentations"][-1]["status"] == "PAID",
          (up["deal"]["status"], codes(up["deal"]), up["reading"]["needs_review"]))

    A = "LC-5 a scanned (photographed) bill of lading + missing invoice"
    s, lc = E.post("/api/v1/tf/lc", lc_app())
    ref = lc["ref"]
    s, up = upload(E, ref, [f("bl_scan.png", scan_of(bill_of_lading(), W)), f("ins.pdf", insurance()), f("pl.pdf", packing())],
                   claimed="300000")
    rd = {x["doc_type"]: x for x in up["reading"]["documents"]}
    check(A, "scanned B/L read by OCR", "BILL_OF_LADING" in rd and rd["BILL_OF_LADING"]["fields"].get("on_board_date") == T.isoformat(),
          rd.get("BILL_OF_LADING"))
    d = up["deal"]
    check(A, "missing invoice detected", any("COMMERCIAL_INVOICE" in x for x in codes(d)), codes(d))

    A = "LC-6 unreadable upload (blank scan)"
    s, lc = E.post("/api/v1/tf/lc", lc_app())
    ref = lc["ref"]
    blank = scan_of(pdf([" "]), W)
    s, up = upload(E, ref, [f("photo.png", blank)], claimed="300000")
    check(A, "held for a document checker", up["deal"]["status"] == "ON_HOLD_DATA_QUALITY", up["deal"]["status"])
    m = E.mails(ref, "trade-ops@bank.test")
    check(A, "bank staff emailed that documents need checking", any("Documents need checking" in x["subject"] for x in m),
          [x["subject"] for x in m])

    A = "LC-7 sanctioned beneficiary"
    s, lc = E.post("/api/v1/tf/lc", lc_app(beneficiary={"name": "Banco Nacional de Cuba", "address": "Havana", "country": "CU"}))
    ref = lc["ref"]
    check(A, "stopped for compliance", lc["status"] == "ON_HOLD_SANCTIONS", lc["status"])
    client = E.mails(ref, "finance@bm-ag.test")
    check(A, "client NOT told about sanctions (anti tipping-off)", all("sanction" not in (x["subject"] + x["body"]).lower() for x in client),
          [x["subject"] for x in client])
    staff = E.mails(ref, "trade-ops@bank.test")
    check(A, "compliance alerted internally", any("Sanctions" in x["subject"] for x in staff), [x["subject"] for x in staff])

    A = "LC-8 unknown customer / wrong currency / missing fields"
    s1, _ = E.post("/api/v1/tf/lc", lc_app(applicant_customer_id="DE-CORP-11111"))
    s2, _ = E.post("/api/v1/tf/lc", lc_app(currency="XYZ"))
    s3, _ = E.post("/api/v1/tf/lc", {"applicant_customer_id": "DE-CORP-40551", "amount": "1000"})
    check(A, "unknown customer refused clearly (409)", s1 == 409, s1)
    check(A, "unsupported currency refused clearly (409)", s2 == 409, s2)
    check(A, "incomplete form refused clearly (422)", s3 == 422, s3)

    # ============================================================= GUARANTEE
    def gtee(**kw):
        g = {"applicant_customer_id": "DE-CORP-40551", "beneficiary": {"name": "Saudi Industrial Projects Co", "address": "Riyadh",
             "country": "SA", "iban": "SA0380000000608010167519", "email": "claims@sip.test"}, "beneficiary_bank_bic": "NCBKSAJEXXX",
             "guarantee_type": "PERFORMANCE", "amount": "200000", "expiry_date": D(365).isoformat(),
             "underlying_transaction": "Contract SIP-2026-114 supply of CNC production line", "underlying_contract_value": "2000000"}
        g.update(kw)
        return g

    A = "GT-1 guarantee application with mistakes"
    s, g = E.post("/api/v1/tf/guarantees", gtee(expiry_date=D(-5).isoformat(), underlying_transaction="", beneficiary_bank_bic="XX"))
    ref = g["ref"]
    reason = (E.exc(ref) or [{"reason": ""}])[0]["reason"]
    check(A, "stopped", g["status"] == "ON_HOLD_DATA_QUALITY", g["status"])
    for code in ["EXPIRY_IN_PAST", "UNDERLYING_MISSING", "BENEFICIARY_BANK_BIC_INVALID"]:
        check(A, f"detects {code}", code in reason, reason)
    m = E.mails(ref, "finance@bm-ag.test")
    check(A, "client emailed what to correct", m and "needs correction" in m[0]["subject"], [x["subject"] for x in m])

    A = "GT-2 demand letter uploaded as PDF, without statement of breach"
    s, g = E.post("/api/v1/tf/guarantees", gtee())
    ref = g["ref"]
    letter = pdf(["DEMAND FOR PAYMENT UNDER GUARANTEE", f"Date: {T.strftime('%d.%m.%Y')}", f"Guarantee no: {ref}",
                  "Amount demanded: EUR 80.000,00", "We hereby demand payment."])
    s, up = E.post(f"/api/v1/tf/guarantees/{ref}/demands/upload", {"files": [f("demand.pdf", letter)]})
    doc = up["reading"]["documents"][0]
    check(A, "demand letter uploaded as a file and read (type, German amount format)", s == 200 and doc["doc_type"] == "DEMAND"
          and doc["fields"].get("amount") == "80000.00", (s, doc))
    r = up["deal"]
    dm = r["demands"][-1]
    check(A, "amount taken from the letter", dm["amount"] == "80000.00", dm["amount"])
    check(A, "rejected: no statement of breach", dm["status"] == "REJECTED" and "NO_STATEMENT_OF_BREACH" in [x["code"] for x in dm["discrepancies"]],
          dm)
    check(A, "beneficiary told why (MT799 to its bank)", "799" in [m["mt_type"] for m in E.c.get(f"/api/v1/tf/swift?deal_ref={ref}").json()])
    m = E.mails(ref, "finance@bm-ag.test")
    check(A, "client informed of the rejected demand", any("Demand rejected" in x["subject"] for x in m), [x["subject"] for x in m])

    A = "GT-3 correct demand letter -> paid; demand too high / wrong currency rejected"
    letter = pdf(["DEMAND FOR PAYMENT UNDER GUARANTEE", f"Guarantee no: {ref}", "Amount demanded: EUR 50,000.00",
                  "Statement of breach: The applicant failed to deliver the production line by the agreed date."])
    s, up = E.post(f"/api/v1/tf/guarantees/{ref}/demands/upload", {"files": [f("demand2.pdf", letter)]})
    r = up["deal"]
    doc = up["reading"]["documents"][0]
    check(A, "complying demand (uploaded PDF) paid", r["demands"][-1]["status"] == "PAID", r["demands"][-1])
    s, bad = E.post(f"/api/v1/tf/guarantees/{ref}/demands/upload", {"files": [f("x.pdf", pdf(["Some letter", "No amount"]))]})
    check(A, "unreadable amount -> clear request to enter it (422), no guessing", s == 422, (s, str(bad)[:200]))
    s, r = E.post(f"/api/v1/tf/guarantees/{ref}/demands", {"presentation_date": T.isoformat(), "amount": "999999", "currency": "EUR",
                  "documents": [{"doc_type": "DEMAND", "fields": doc["fields"]}]})
    check(A, "demand above remaining amount rejected", r["demands"][-1]["status"] == "REJECTED", r["demands"][-1]["status"])
    s, r = E.post(f"/api/v1/tf/guarantees/{ref}/demands", {"presentation_date": T.isoformat(), "amount": "1000", "currency": "USD",
                  "documents": [{"doc_type": "DEMAND", "fields": doc["fields"]}]})
    check(A, "demand in wrong currency rejected", r["demands"][-1]["status"] == "REJECTED", r["demands"][-1]["status"])

    # ============================================================= SURETY
    def sur(**kw):
        b = {"principal_customer_id": "DE-CORP-77310", "obligee": {"name": "Stadt Koeln", "address": "Koeln", "country": "DE",
             "iban": "DE89370400440532013000", "email": "vergabe@koeln.test"}, "bond_type": "CONTRACT_PERFORMANCE",
             "amount": "150000", "contract_value": "1500000", "expiry_date": D(500).isoformat(),
             "underlying_contract": "VOB/B contract school extension Lot 3"}
        b.update(kw)
        return b

    A = "SU-1 surety application with mistakes"
    s, b = E.post("/api/v1/tf/sureties", sur(bond_type="RENT_DEPOSIT", amount="9000", monthly_rent="2000",
                                                                         contract_value=None))
    ref = b["ref"]
    reason = (E.exc(ref) or [{"reason": ""}])[0]["reason"]
    check(A, "rent deposit above 3 monthly rents detected (BGB 551)", b["status"] == "ON_HOLD_DATA_QUALITY" and "RENT_DEPOSIT_CAP" in reason,
          (b["status"], reason))
    s, b2 = E.post("/api/v1/tf/sureties", sur(amount="900000", contract_value="1000000"))
    reason2 = (E.exc(b2["ref"]) or [{"reason": ""}])[0]["reason"]
    check(A, "performance bond of 90% of contract value detected", "BOND_PERCENTAGE" in reason2, (b2["status"], reason2))
    m = E.mails(ref, "office@rheinbau.test")
    check(A, "client emailed what to correct", m and "needs correction" in m[0]["subject"], [x["subject"] for x in m])

    A = "SU-2 claim with missing documents, then a proper claim"
    s, b = E.post("/api/v1/tf/sureties", sur())
    ref = b["ref"]
    claim_letter = pdf(["CLAIM UNDER SURETY BOND", f"Bond: {ref}", "Amount claimed: EUR 40,000.00",
                        "We hereby claim payment for defective works."])
    s, up = E.post(f"/api/v1/tf/sureties/{ref}/claims/upload", {"files": [f("claim.pdf", claim_letter)]})
    r = up["deal"]
    c = [x["code"] for x in r["claims"][-1]["discrepancies"]]
    check(A, "missing notice of default and evidence detected", "NO_NOTICE_OF_DEFAULT" in c and "NO_EVIDENCE_OF_CLAIM" in c, c)
    m = E.mails(ref, "vergabe@koeln.test")
    check(A, "obligee emailed why the claim was rejected", any("claim" in x["subject"].lower() for x in m), [x["subject"] for x in m])
    notice = pdf(["NOTICE OF DEFAULT", f"Date: {T.strftime('%d.%m.%Y')}", "The contractor failed to remedy the defects within the deadline."])
    evidence = pdf(["EXPERT REPORT", "Evidence of damage: cost of remedy EUR 40,000.00 according to attached estimate."])
    s, up = E.post(f"/api/v1/tf/sureties/{ref}/claims/upload", {"files": [f("claim.pdf", claim_letter), f("notice.pdf", notice),
                                                                         f("report.pdf", evidence)]})
    r = up["deal"]
    check(A, "all three uploaded documents recognised", sorted(d["doc_type"] for d in up["reading"]["documents"]) ==
          ["DEMAND", "EVIDENCE_OF_CLAIM", "NOTICE_OF_DEFAULT"], [d["doc_type"] for d in up["reading"]["documents"]])
    check(A, "proper claim waits for the principal", r["claims"][-1]["status"] == "AWAITING_PRINCIPAL", r["claims"][-1]["status"])
    m = [x for x in E.mails(ref, "office@rheinbau.test") if "defences" in x["subject"]]
    check(A, "principal emailed with acknowledge / dispute links", m and "/portal/decision" in m[0]["body"], [x["subject"] for x in m])



def main():
    E = Env()
    try:
        run(E)
    except Exception as e:
        R.append(("RUN", "audit crashed", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"))
    finally:
        log = open(f"{E.work}/app.log").read()
        R.append(("PLATFORM", "no crash / traceback in the platform log", "Traceback" not in log, log[-600:]))
        E.stop()
    area = None
    for a, n, ok, det in R:
        if a != area:
            area = a; print(f"\n{a}")
        print(f"  {'OK  ' if ok else 'FAIL'} {n}" + ("" if ok else f"\n         -> {det}"))
    print(f"\nTOTAL {sum(r[2] for r in R)}/{len(R)} checks passed")


if __name__ == "__main__":
    main()
