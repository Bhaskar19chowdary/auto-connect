# Operations Runbook – Auto-Connect Trade Finance

## Daily

| When | What | Who |
|---|---|---|
| Start of day | `GET /ready` must say `READY`. Check `GET /api/v1/tf/reference` (ECB date = previous TARGET day, EU + OFAC `LOADED`). | Operations |
| During the day | Work the exception queue (`GET /api/v1/tf/exceptions`) – each item names the deciding role. | Compliance, Credit, Legal, Document checkers |
| End of day | The batch runs automatically (`TF_AUTO_BATCH_MINUTES`); result in `GET /api/v1/tf/summary` → `scheduler.last_run`. | Operations |

## Exception queue – who decides

| Category | Role | Approvals | Typical action |
|---|---|---|---|
| SANCTIONS | COMPLIANCE | 2 to release, 1 to reject | Confirm/dismiss the alert; rejection blocks the deal/payment |
| KYC | COMPLIANCE | 1 | Approve after the customer file is updated |
| CREDIT | CREDIT | 2 to approve | Approve the excess (temporary; removed when the deal runs off) |
| LEGAL | LEGAL | 1 | Approve = pay the claim, reject = claim refused |
| DATA_QUALITY | DOC_CHECKER | 1 | Correct read documents (`POST …/presentations/{id}/documents`), then approve to examine |
| OPERATIONS | OPERATIONS | 1 | System was down: approve to retry; or unfunded payment follow-up |

Watch the deadlines: a presentation must be examined within 5 banking days (UCP 600 Art. 14(b)); the
exception text shows the deadline.

## Incidents

| Symptom | Meaning | Action |
|---|---|---|
| Deals in `ON_HOLD_OPERATIONS` | a bank system was unavailable during issuance | fix the system, approve the exception to retry |
| Email "`[OPS] … served by fallback system`" | a connected vendor failed; the incumbent system answered | check the vendor; binding re-tested nightly |
| `/ready` → `sanctions_lists_loaded: false` | lists could not be loaded and no cache exists | check internet/proxy to the sources; `POST /api/v1/tf/reference/refresh` |
| `/ready` → `audit_trail_intact: false` | the audit log was modified | security incident: preserve the file, inform IT security |
| `/ready` → `books_balanced: false` | ledger out of balance | stop processing, inform finance / IT |
| `/ready` → `database: false` | database unreachable | check the PostgreSQL service; no action was half-booked (each action is one transaction) |
| HTTP 502 from the API | a bank system failed during a request; message names it | retry after the system is back (nothing was booked or sent) |
| Deals `ON_HOLD_OPERATIONS` with `SANCTIONS_LISTS_NOT_LOADED` | lists still loading after a restart, or the download failed | wait for `/ready`, then approve the items to retry |
| `GET /api/v1/tf/outbox?status=FAILED` not empty | an email / SWIFT release failed after commit | fix SMTP / SWIFT interface, then `POST /api/v1/tf/outbox/dispatch` (also retried by the daily batch) |

## Users

```
python -m auto_connect.security add-user --id j.mueller --name "Julia Mueller" --roles COMPLIANCE
python -m auto_connect.security list
python -m auto_connect.security verify-audit
```

Keys are shown once. To revoke a user set `"active": false` in the users file.

## Backups

* PostgreSQL: the bank's standard backup (pg_dump / point-in-time recovery).
* SQLite (pilot): `python -m auto_connect.trade.database backup bank-backup.db` (online, consistent);
  `python -m auto_connect.trade.database info` shows deals, ledger entries and whether the books balance.
