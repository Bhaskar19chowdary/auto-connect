# Auto-Connect Trade Finance – Deployment Guide

This guide is for the bank's integration, IT security and operations teams. It explains what the system does
on its own, how it is plugged into the bank's existing applications, which controls are built in, and what
the bank has to do before go-live.

---

## 1. What the system does automatically

| Product | Straight-through (no human) | Goes to a human, by design |
|---|---|---|
| **Import LC** (UCP 600) | validation, KYC check, sanctions/embargo/dual-use screening, limit earmark (incl. reallocation, cash collateral, delegated excess), fees, contingent booking, MT700, amendments MT707, document reading (IDR), examination, honour / waiver request / refusal MT734, payment MT756 + pacs.009, deferred payment at maturity, expiry | sanctions hit, credit above delegated authority, unreadable documents, KYC expired |
| **Guarantee / Standby** (URDG 758 / ISP98) | issuance MT760, amendments MT767, demand examination, extend-or-pay suspension, payment pacs.008 with recourse, quarterly aval commission, expiry and release MT769 | open-ended guarantees, sanctions, credit |
| **Surety bond** (Bürgschaft, §§ 765 ff. BGB) | underwriting rules (VOB/B %, § 551 BGB rent cap), deed, claims (accessory: principal asked for defences; on first demand: paid), payment after deadline, release | principal disputes a claim (legal) |
| **Export LC** (UCP 600, the bank as advising / confirming bank) | MT700 screening, advice + MT730, confirmation against the issuing bank's counterparty limit, transfer MT720, examination, payment to the exporter with MT754 / MT742 reimbursement claim, documents for honour or on approval (MT750), deferred payment at maturity, expiry | confirmation above the bank limit, issuing bank refuses documents we paid (legal) |
| **Documentary collection** (URC 522, D/P and D/A) | export: documents checked against the list and sent, proceeds credited on MT400, acceptance MT412, non-payment MT416; import: MT410, drawee's pay / accept / refuse via signed link, proceeds pacs.009 + MT400, acceptance paid at maturity, non-payment advised when the drawee is silent | sanctions |
| **Counter-guarantee** (URDG 758 Art. 15(b)) | MT760 ICCO with the local undertaking, demand needs the guarantor's statement, payment bank-to-bank (pacs.009) | – |
| **Every day** (end-of-day batch) | maturities, deadlines, refusals after silence, reminders, commissions, expiries, KYC review alerts, ECB rates + EU/OFAC list refresh, re-test of suspended integrations | – |

Banking days follow the calendar of a bank in Munich (TARGET2 + German + Bavarian holidays + 24/31 Dec).

---

## 2. Connecting the bank's applications

The engine talks to nine bank systems over an internal agent bus. Each one runs as a built-in **simulator**
until the bank points it at the real application. Every system publishes its exact contract (OpenAPI):
`GET /api/v1/tf/systems/{agent_id}/openapi`.

| Agent id | Role | Operations | Typical bank application |
|---|---|---|---|
| `tf_customer_master` | Customer master & KYC | get_customer, periodic_reviews_due | CIF / KYC platform |
| `tf_sanctions_screening` | Sanctions, embargo, dual-use | screen_parties | Screening engine (name, vessel, goods) |
| `tf_market_data` | FX reference rates | fx_rate, convert | Treasury market data feed (ECB rates built in) |
| `tf_core_ledger` | Postings, balances, blocked funds | post_entries, account_balance, block_funds, unblock_funds | Core banking |
| `tf_credit_limits` | Facilities, earmarks, collateral | earmark_limit, release_limit, customer_exposure | Credit / collateral management |
| `tf_swift_gateway` | SWIFT release (MT7xx, ISO 20022 pacs) | release_message | SWIFT interface |
| `tf_document_archive` | Evidence archive | store_document | DMS / archive (WORM) |
| `tf_email_gateway` | Email | dispatch_message | Exchange / SMTP relay |
| `tf_client_portal` | Client notices and decision links | notify_client | Corporate banking portal |

**Option A – adapter service (recommended):** the bank's integration layer exposes
`POST {base}/api/v1/{agent_id}/{operation}` following the published contract and translates to the real system.
Then set:

```
TF_SYSTEM_URL_TF_CORE_LEDGER=https://tf-adapter.internal/ledger
TF_SYSTEM_TOKEN_TF_CORE_LEDGER=<service token from the bank's secret store>
```

**Option B – connect by discovery:** for read-only roles (sanctions screening, FX rates) an application can be
connected from the dashboard by URL. The platform reads its OpenAPI description, classifies it, runs contract
tests and switches over only if they pass, with automatic failover, circuit breaker and nightly re-test.
Roles that post, pay or send are never switched automatically.

Other applications (payments hub, KYC platform, collateral, reporting …) can be connected the same way as
agents with an operator console and A2A messages, without taking over an engine role.

---

### Connection options per system

| Need | How |
|---|---|
| API gateway login | OAuth 2.0 client credentials (token cached, refreshed on 401) - `auth_type=OAuth2` |
| Bank PKI | real mutual TLS: client certificate + key presented in the handshake, server verified against the bank CA |
| Only the gateway may be called | `AUTO_CONNECT_ALLOWED_HOSTS` (all other hosts are refused); corporate proxy `AUTO_CONNECT_HTTPS_PROXY` |
| Tracing | every call carries `X-Correlation-ID` = the deal reference |
| Secrets | stored as references (`env:NAME`, `file:/path` from the bank's secret manager), never as values |
| Message-based systems | `FILE` (hot folder, e.g. SWIFT Alliance file transfer, raw FIN / XML), `SFTP`, `IBM_MQ` (MQ REST messaging API, request/reply by correlation id) |
| A system with a different API | certify the application for the role with a field **mapping** (endpoint, request and response fields) - no code |

Applications connected from the dashboard take over **write** roles (ledger, limits, SWIFT, email, archive, portal)
only after **certification** with test evidence, approved by a **second administrator** (`POST /api/v1/apps/{id}/certify`,
`/certify/approve`). Certifications are saved with the books and survive restarts. Emails and SWIFT messages to real
systems always go through the outbox (sent only after the action is committed).

**System of record:** customer data comes from the customer master and account balances from the core ledger on
every action; the engine keeps only trade-finance data (deals, standing instructions, its contingent bookings).

**Capacity (measured):** ~160 complete LC lifecycles per second on SQLite (issue + documents + payment), constant
as the books grow (tested to 5,000 deals / 40,000 ledger entries). The books are held in memory: plan archiving of
closed deals after several years of history.

**Scanned documents:** Tesseract OCR (German + English) and Poppler are in the container; Gemini, when available,
reads first. Uncertain readings always go to a document checker.

**Demo vs production:** in production the simulated demo applications, preset connections and scripted demo
endpoints (`/api/v1/orchestrate`, `/api/v1/trade/examine-application`) are not available.

---

### Proof of coordination: the landscape test

`python tests/landscape/run_landscape.py` (Docker needed) starts nine independent external systems - each in its own
process with its own books and its own API (customer hub, screening, rates, core ledger in minor units, limit system,
DMS, client portal), SWIFT over a real SFTP server and email over a real SMTP server - connects and certifies them
through the platform's API with the field mappings in `tests/landscape/mappings.json`, runs complete deals across all
of them (import LC, discrepancy waiver, guarantee, surety, confirmed export LC with FX, D/A collection, credit and KYC
holds, sanctions hit), injects failures (ledger down mid-payment, ledger down during issuance, failed reversal, limit
system down), restarts the platform, and reconciles every system's own books. Use it as the template for the bank's
integration tests: replace the simulated vendors with the bank's test systems and the mappings with theirs.

When an action fails after external systems have already booked, the platform reverses those bookings automatically
(compensating journals, limit re-reservation); what cannot be reversed becomes a reconciliation task for operations.
A failed issuance resumes where it stopped - fees, reservations and SWIFT messages are never duplicated.

---

## 3. Built-in controls

| Control | How |
|---|---|
| Named users & roles | `python -m auto_connect.security add-user --id … --name … --roles …`; keys stored as SHA-256 only |
| Segregation of duties | each endpoint allowed to specific roles (OPERATIONS, DOC_CHECKER, COMPLIANCE, CREDIT, LEGAL, ADMIN, VIEWER) |
| Responsible decider | sanctions/KYC → COMPLIANCE, credit → CREDIT, disputes → LEGAL, documents → DOC_CHECKER |
| Four-eyes | releasing a sanctions hold and approving a credit excess need two different approvers |
| Maker-checker | the user who entered a deal cannot decide its exceptions |
| Tipping-off (§ 47 GwG) | clients never see sanctions reasons |
| Audit trail | every change appended to a hash-chained log; `GET /api/v1/audit/verify`; tampering makes `/ready` fail |
| Production mode | `AUTO_CONNECT_ENV=production`: refuses to start without users, reset/demo disabled, API docs hidden, CORS closed |
| AI | Gemini on Vertex AI in the bank's own project, EU region (`GOOGLE_GENAI_USE_VERTEXAI=true`); everything also works without AI |
| Payments | ISO 20022 pacs.008 / pacs.009 (CBPR+), validated before release |
| Outbox | emails and SWIFT messages are queued inside the transaction and released only after commit (a rolled-back action sends nothing; a crash after commit re-sends from the outbox) – `GET /api/v1/tf/outbox` |
| Screening completeness | while the EU / OFAC lists are not loaded, no deal is screened – it waits in the operations queue |

---

## 4. Running it

**Database.** The books (customers, facilities, accounts, ledger, deals, SWIFT messages, exceptions) live in a
database, set by `AUTO_CONNECT_DATABASE_URL`:

| Setting | Use |
|---|---|
| `postgresql://user:password@host:5432/tradefinance` | bank operation (PostgreSQL 14+) |
| `sqlite:///state/bank.db` (default) | single server, pilots |

* every business action is **one transaction** – all of its bookings are stored or none (a crash or a failing
  system halfway through a payment leaves nothing half-booked);
* several application servers can share one PostgreSQL database (writes are serialised by a database lock,
  each server reloads changes made by the others);
* tables have plain columns for SQL reporting (`deals`, `ledger`, `accounts`, `facilities`, `exceptions`,
  `swift_messages`, …) – e.g. `select status, count(*), sum(outstanding) from deals group by status`;
* an existing JSON state file is migrated into the database automatically on first start.

**Containers** (app + PostgreSQL):

```
export TF_DB_PASSWORD=$(openssl rand -hex 16)          # from the bank's secret store in practice
docker compose run --rm app python -m auto_connect.security add-user --id ops1 --name "Ops One" --roles OPERATIONS
docker compose up -d
curl http://127.0.0.1:8088/ready
```

`GET /health` (liveness) and `GET /ready` (database reachable, books balanced, users configured, sanctions
lists loaded, audit trail intact) are for the load balancer / orchestrator.

**Backups:** PostgreSQL with the bank's standard tooling (`pg_dump`, point-in-time recovery via WAL archiving);
SQLite online backup: `python -m auto_connect.trade.database backup bank-backup.db`.

---

## 5. What the bank must still do before go-live (cannot be shipped by any vendor)

1. **Integration**: build or configure the adapters to the bank's real systems (section 2) and certify each
   in the bank's test environment.
2. **Data store**: provide the PostgreSQL instance on the bank's platform (high availability, encryption at
   rest, backups / point-in-time recovery, 10-year retention of booking records). Emails and SWIFT files are
   produced through a transactional outbox: they leave the bank only after the action is committed.
3. **Identity**: replace personal API keys with the bank's SSO (Azure AD / ADFS) and connect the roles to
   the bank's entitlement system.
4. **Secrets**: move SMTP / system tokens from environment files to the bank's vault (HSM / secret manager);
   `vault_credentials.json` holds tokens in plain text and must not be used in production.
5. **Licensed data**: replace the public EU list mirror and ECB feed with the bank's licensed screening lists
   and treasury rates; calibrate screening thresholds with Compliance.
6. **Reviews & approvals**: IT security / penetration test, MaRisk AT 7.2 and DORA ICT risk assessment,
   BAIT, data protection impact assessment (GDPR), outsourcing assessment if run in the cloud.
7. **Parallel run**: run alongside the current process on real volumes, compare every decision, then go live
   product by product.

## Client portal and bank workbench

| Address | Who | What |
|---|---|---|
| `/portal` | Corporate clients | Apply for an import LC, guarantee, surety or export collection; upload supporting documents; follow every step; accept/refuse discrepant documents, answer surety claims, extend-or-pay; messages |
| `/bank` | Trade finance staff | Today's work, decisions with a plain explanation of each choice (four-eyes shown), applications, every transaction as a story, customers and limits, applications entered on behalf of a client, Excel report |

* Every application is saved in the `applications` table **before** it is processed. If a bank system is down, it waits
  (`WAITING_FOR_SYSTEM`) and is retried automatically by the scheduler, or by staff with "Retry".
* Clients never see internal checks: a sanctions, KYC or credit hold shows only as "Under review" (no tipping-off).
* The pages load no external scripts or fonts (work inside a closed bank network).

Client users (production): one key per person, bound to one company, no bank roles allowed:

```bash
python -m auto_connect.security add-user --id bma.cfo --name "CFO Bayerische Maschinenbau" --roles CLIENT --customer-id DE-CORP-40551
```

Without a users file (demo only, never in production) the portals offer a company / employee picker instead.
In a real rollout, clients would sign in through the bank's existing online-banking login (SSO) - see the open points.
