# Architecture

## Layers

| Layer | Modules | Responsibility |
|---|---|---|
| Screens | `templates/`, `trade/portal_api.py`, `trade/plain.py` | Client portal, bank workbench, technical console; plain-language views |
| REST API | `trade/api.py`, `dashboard.py` | `/api/v1/tf/...` (transactions), `/api/v1/portal/...` (clients), `/api/v1/staff/...` (employees) |
| Security | `security.py` | Named users and roles, endpoint permissions, four-eyes / maker-checker, hash-chained audit log |
| Workflow engine | `trade/engine.py`, `trade/products.py` | Product lifecycles, exception queue, end-of-day batch |
| Rules | `trade/rules.py`, `trade/bankdays.py` | UCP 600, URDG 758, ISP98, URC 522, BGB checks; banking-day calendar |
| Messages | `trade/swift.py`, `trade/iso20022.py` | SWIFT MT7xx / MT4xx, ISO 20022 pacs.008 / pacs.009 |
| Bank-system roles | `trade/systems.py` | Customer master, sanctions, market data, core ledger, credit limits, SWIFT gateway, archive, email, client portal |
| Integration | `trade/integration.py`, `trade/mapping.py`, `executor.py`, `transports.py`, `vault.py` | Binding real applications to roles, declarative field mapping, certification, circuit breaker, REST / file / SFTP / MQ |
| Persistence | `trade/database.py`, `trade/state.py` | SQLite / PostgreSQL (or JSON), unit of work, original documents |
| Discovery | `brain.py`, `introspector.py`, `onboarding.py`, `mesh.py` | Reading OpenAPI / OData / 3270 screens, agent cards, A2A mesh |

## One business action

```
begin transaction (row lock / advisory lock)
  ├─ steps call bank systems through the TradeBus (correlation id = deal reference)
  ├─ external bookings register a compensation (saga)
  ├─ emails / SWIFT releases are queued in the outbox
commit  ─────────────► outbox dispatched (at-least-once, retried)
on error: rollback; compensations reverse what other systems already booked;
          a failed reversal becomes a reconciliation task for operations
```

Retries are safe: steps that already succeeded are recorded (`_did`), one-off fees are charged once.

## Applications

Client submissions are stored in the `applications` table **before** processing:

```
SUBMITTED ─► PROCESSED (transaction created)
          ├► NEEDS_CORRECTION (typing mistakes: wrong BIC, past dates ... - client corrects and resubmits)
          ├► WAITING_FOR_SYSTEM (a bank system was down - retried automatically)
          ├► REJECTED (cannot be processed) ─► client may edit and resubmit
          └► WITHDRAWN
```

An employee can also send a data-quality stop back to the client while nothing was booked yet.

## Connecting a real system

1. Register the application (URL / OpenAPI) and bind it to a role.
2. Describe the field mapping in JSON (`from`, `const`, `map`, `date`, `scale`, `each`, `template`, `errors`).
3. Contract tests run against it; a second administrator certifies it (four-eyes).
4. From then on the role is served by the real application; bindings survive restarts.

## Data protection in the client portal

Clients only see their own company. Internal checks (sanctions, KYC, credit) are shown as "Under review" – the
reason is never disclosed (no tipping-off).
