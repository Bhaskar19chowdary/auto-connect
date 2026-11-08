"""
Database storage of the bank's books (SQLite locally, PostgreSQL in the bank) - same code, one setting:

    AUTO_CONNECT_DATABASE_URL=sqlite:///state/bank.db
    AUTO_CONNECT_DATABASE_URL=postgresql://user:password@db-host:5432/tradefinance

* Every engine action is one database transaction: all of its bookings are stored, or none (rollback).
* Only changes are written: ledger, SWIFT messages, screening cases, notices and archive entries are
  append-only rows; customers, accounts, facilities, exceptions and the deals touched by the action are upserted.
* Several application servers can share one database: writes are serialised by a database lock
  (BEGIN IMMEDIATE / pg_advisory_xact_lock) and a version counter tells a server when another one has changed
  the books, so it reloads them before acting.
* Tables carry plain columns (status, amounts, accounts, dates) for SQL reports and audits; the full record is
  kept as JSON in `data`.

Backup:  python -m auto_connect.trade.database backup bank-backup.db     (SQLite; PostgreSQL: pg_dump)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .state import BankState, seed_state

logger = logging.getLogger("auto_connect.trade.database")

KEYED = {  # table: (state attribute, key column, extra columns -> function(record dict))
    "customers": ("customers", "customer_id", {"name": lambda r: r["name"], "kyc_status": lambda r: r["kyc_status"]}),
    "facilities": ("facilities", "facility_id", {"customer_id": lambda r: r["customer_id"], "product": lambda r: r["product"],
                                                 "limit_amount": lambda r: r["limit"], "utilised": lambda r: r["utilised"]}),
    "accounts": ("accounts", "account_id", {"owner": lambda r: r["owner"], "currency": lambda r: r["currency"],
                                            "balance": lambda r: r["balance"]}),
    "deals": ("deals", "ref", {"product": lambda r: r["product"], "customer_id": lambda r: r["customer_id"],
                               "status": lambda r: r["status"], "currency": lambda r: r["currency"],
                               "amount": lambda r: r["amount"], "outstanding": lambda r: r["outstanding"],
                               "created_by": lambda r: r.get("created_by")}),
    "exceptions": ("exceptions", "exception_id", {"deal_ref": lambda r: r["deal_ref"], "category": lambda r: r["category"],
                                                  "status": lambda r: r["status"], "resolved_by": lambda r: r.get("resolved_by")}),
    "swift_messages": ("swift_messages", "message_id", {"deal_ref": lambda r: r["deal_ref"], "mt_type": lambda r: r["mt_type"],
                                                        "receiver_bic": lambda r: r["receiver_bic"],
                                                        "created_at": lambda r: r["created_at"]}),
    "applications": ("applications", "application_id", {"customer_id": lambda r: r["customer_id"],
                                                        "product": lambda r: r["product"], "status": lambda r: r["status"],
                                                        "deal_ref": lambda r: r.get("deal_ref"),
                                                        "submitted_at": lambda r: r["submitted_at"]}),
    "outbox": ("outbox", "id", {"kind": lambda r: r["kind"], "status": lambda r: r["status"],
                                "deal_ref": lambda r: r.get("deal_ref"), "created_at": lambda r: r["created_at"]}),
}
# Records that only change in a few known fields: a cheap fingerprint decides whether they must be rewritten,
# so a commit does not re-serialise every message / exception ever created (constant cost as the books grow)
# Tables that only grow and whose few changes are reported by the code making them (store.mark): a commit writes
# just the new and the marked records - constant cost however large the books become.
TRACKED = {"swift_messages", "outbox", "exceptions", "applications"}
LISTS = {  # table: (state attribute, extra columns)
    "ledger": ("ledger", {"entry_id": lambda r: r["entry_id"], "booking_date": lambda r: r["booking_date"],
                          "value_date": lambda r: r["value_date"], "deal_ref": lambda r: r["deal_ref"],
                          "debit_account": lambda r: r["debit_account"], "credit_account": lambda r: r["credit_account"],
                          "amount": lambda r: r["amount"], "currency": lambda r: r["currency"],
                          "narrative": lambda r: r["narrative"]}),
    "sanctions_cases": ("sanctions_cases", {"deal_ref": lambda r: r.get("deal_ref"), "result": lambda r: r.get("result")}),
    "notifications": ("notifications", {"deal_ref": lambda r: r.get("deal_ref")}),
    "documents": ("documents", {"deal_ref": lambda r: r.get("deal_ref")}),
}
NUMERIC_COLS = {"limit_amount", "utilised", "balance", "amount", "outstanding"}
META_FIELDS = ["business_date", "reference", "counters", "portal_secret", "bindings"]
SCHEMA_VERSION = "1"


class DatabaseStore:
    def __init__(self, url: str, seed: bool = True, legacy_json_path: str = ""):
        self.url = url
        self.pg = url.startswith(("postgres://", "postgresql://"))
        self.conn = self._connect()
        self.in_transaction = False
        self.version = -1
        self._saved: Dict[str, Dict[str, str]] = {}
        self._dirty: Dict[str, Set[str]] = {}
        self._saved_len: Dict[str, int] = {}
        self._create_schema()
        if self._empty():
            self.state = self._initial_state(seed, legacy_json_path)
            self._write_all()
        else:
            self._load()

    # ------------------------------------------------------------------ connection / SQL
    def _connect(self):
        if self.pg:
            import psycopg

            conn = psycopg.connect(self.url, autocommit=True)
            return conn
        path = self.url.split("sqlite:///", 1)[1] if self.url.startswith("sqlite:///") else self.url
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def _exec(self, sql: str, params: Iterable[Any] = ()):
        return self.conn.execute(self._q(sql), tuple(params))

    def _many(self, sql: str, rows: List[Tuple]):
        if not rows:
            return
        if self.pg:
            with self.conn.cursor() as cur:
                cur.executemany(self._q(sql), rows)
        else:
            self.conn.executemany(sql, rows)

    def _create_schema(self):
        num = "NUMERIC(24,6)" if self.pg else "TEXT"
        txt = "TEXT"
        ddl = [f"CREATE TABLE IF NOT EXISTS meta (key {txt} PRIMARY KEY, value {txt} NOT NULL)"]
        for table, (_, key, cols) in KEYED.items():
            extra = "".join(f", {c} {num if c in NUMERIC_COLS else txt}" for c in cols)
            ddl.append(f"CREATE TABLE IF NOT EXISTS {table} ({key} {txt} PRIMARY KEY{extra}, data {txt} NOT NULL, "
                       f"updated_at {txt})")
        for table, (_, cols) in LISTS.items():
            extra = "".join(f", {c} {num if c in NUMERIC_COLS else txt}" for c in cols)
            ddl.append(f"CREATE TABLE IF NOT EXISTS {table} (seq INTEGER PRIMARY KEY{extra}, data {txt} NOT NULL)")
        ddl += ["CREATE INDEX IF NOT EXISTS ix_ledger_deal ON ledger (deal_ref)",
                "CREATE INDEX IF NOT EXISTS ix_deals_status ON deals (status)",
                "CREATE INDEX IF NOT EXISTS ix_applications_customer ON applications (customer_id)",
                "CREATE INDEX IF NOT EXISTS ix_exceptions_status ON exceptions (status)",
                "CREATE INDEX IF NOT EXISTS ix_swift_deal ON swift_messages (deal_ref)"]
        blob = "BYTEA" if self.pg else "BLOB"
        ddl.append(f"CREATE TABLE IF NOT EXISTS files (file_id {txt} PRIMARY KEY, deal_ref {txt}, item_id {txt}, "
                   f"filename {txt}, mime {txt}, size INTEGER, sha256 {txt}, uploaded_at {txt}, uploaded_by {txt}, "
                   f"content {blob} NOT NULL)")
        ddl.append("CREATE INDEX IF NOT EXISTS ix_files_deal ON files (deal_ref)")
        for stmt in ddl:
            self._exec(stmt)
        self._exec("INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT (key) DO NOTHING", [SCHEMA_VERSION])

    def _empty(self) -> bool:
        return self._exec("SELECT COUNT(*) FROM meta WHERE key = 'business_date'").fetchone()[0] == 0

    def _initial_state(self, seed: bool, legacy_json_path: str) -> BankState:
        if legacy_json_path and os.path.exists(legacy_json_path):  # one-time migration of the JSON books
            with open(legacy_json_path, "r", encoding="utf-8") as f:
                state = BankState.model_validate_json(f.read())
            logger.warning(f"Migrated the bank's books from {legacy_json_path} into the database")
            return state
        return seed_state() if seed else BankState()

    # ------------------------------------------------------------------ serialisation
    @staticmethod
    def _records(state: BankState, attr: str) -> Dict[str, Any]:
        return getattr(state, attr)

    @staticmethod
    def _dump(obj) -> Tuple[str, Dict[str, Any]]:
        if hasattr(obj, "model_dump"):
            rec = obj.model_dump(mode="json")
        else:
            rec = json.loads(json.dumps(obj, default=str))
        return json.dumps(rec, separators=(",", ":"), sort_keys=True), rec

    def _cols(self, cols: Dict[str, Any], rec: Dict[str, Any]) -> List[Any]:
        out = []
        for c, fn in cols.items():
            v = fn(rec)
            out.append(None if v is None else str(v))
        return out

    # ------------------------------------------------------------------ load
    def _load(self):
        meta = dict(self._exec("SELECT key, value FROM meta").fetchall())
        data: Dict[str, Any] = {k: json.loads(meta[k]) for k in META_FIELDS if k in meta}
        self._saved = {}
        self._dirty = {}
        for table, (attr, key, _) in KEYED.items():
            rows = self._exec(f"SELECT {key}, data FROM {table}").fetchall()
            data[attr] = {k: json.loads(v) for k, v in rows}
            self._saved[table] = {k: v for k, v in rows}
        for table, (attr, _) in LISTS.items():
            rows = self._exec(f"SELECT data FROM {table} ORDER BY seq").fetchall()
            data[attr] = [json.loads(r[0]) for r in rows]
            self._saved_len[table] = len(rows)
        self.state = BankState.model_validate(data)
        self._saved["meta"] = {k: meta[k] for k in META_FIELDS if k in meta}
        self.version = int(meta.get("version", "0"))

    # ------------------------------------------------------------------ write
    def _write(self, dirty_deals: Optional[Set[str]] = None, full: bool = False):
        now = datetime.now(timezone.utc).isoformat()
        st = self.state
        for table, (attr, key, cols) in KEYED.items():
            current = self._records(st, attr)
            saved = self._saved.setdefault(table, {})
            upserts = []
            if not full and len(current) >= len(saved) and (table in TRACKED or (table == "deals" and dirty_deals is not None)):
                dirty = set(dirty_deals) if table == "deals" else self._dirty.pop(table, set())
                new_keys = []
                for k in reversed(current):  # new records sit at the end: stop at the first one already saved
                    if k in saved:
                        break
                    new_keys.append(k)
                keys = new_keys[::-1] + [k for k in dirty if k in current and k in saved]
                candidates = ((k, current[k]) for k in keys)
            else:
                self._dirty.pop(table, None)
                candidates = current.items()
            for k, obj in candidates:
                if not full and k in saved and table == "deals" and dirty_deals is not None and k not in dirty_deals:
                    continue
                text, rec = self._dump(obj)
                if full or saved.get(k) != text:
                    upserts.append((k, *self._cols(cols, rec), text, now))
                    saved[k] = text
            gone = [k for k in saved if k not in current] if len(saved) != len(current) else []  # deletions are rare
            for k in gone:
                self._exec(f"DELETE FROM {table} WHERE {key} = ?", [k])
                saved.pop(k)
            names = [key, *cols, "data", "updated_at"]
            sets = ", ".join(f"{c} = excluded.{c}" for c in names[1:])
            self._many(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))}) "
                       f"ON CONFLICT ({key}) DO UPDATE SET {sets}", upserts)
        for table, (attr, cols) in LISTS.items():
            items = getattr(st, attr)
            start = self._saved_len.get(table, 0)
            if full or len(items) < start:  # list was reset: rewrite it
                self._exec(f"DELETE FROM {table}")
                start = 0
            rows = []
            for i, obj in enumerate(items[start:], start):
                text, rec = self._dump(obj)
                rows.append((i, *self._cols(cols, rec), text))
            names = ["seq", *cols, "data"]
            self._many(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", rows)
            self._saved_len[table] = len(items)
        meta_saved = self._saved.setdefault("meta", {})
        for field in META_FIELDS:
            text = json.dumps(json.loads(st.model_dump_json(include={field}))[field], separators=(",", ":"), sort_keys=True)
            if full or meta_saved.get(field) != text:
                self._exec("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                           [field, text])
                meta_saved[field] = text
        self.version = self._bump_version()

    def _bump_version(self) -> int:
        row = self._exec("SELECT value FROM meta WHERE key = 'version'").fetchone()
        v = int(row[0]) + 1 if row else 1
        self._exec("INSERT INTO meta (key, value) VALUES ('version', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   [str(v)])
        return v

    def _write_all(self):
        self._lock_begin()
        try:
            self._write(full=True)
            self._exec("COMMIT")
        except Exception:
            self._exec("ROLLBACK")
            raise

    # ------------------------------------------------------------------ unit of work
    def _lock_begin(self):
        if self.pg:
            self._exec("BEGIN")
            self._exec("SELECT pg_advisory_xact_lock(7316402)")  # one writer at a time across servers
        else:
            self._exec("BEGIN IMMEDIATE")

    def _db_version(self) -> int:
        row = self._exec("SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row[0]) if row else 0

    def begin(self) -> None:
        self._lock_begin()
        self.in_transaction = True
        if self._db_version() != self.version:  # another server changed the books: act on the latest state
            self._load()

    def commit(self, dirty_deals: Optional[Set[str]] = None) -> None:
        try:
            self._write(dirty_deals)
            self._exec("COMMIT")
        except Exception:
            self._exec("ROLLBACK")
            self.in_transaction = False
            self._load()
            raise
        self.in_transaction = False

    def rollback(self) -> None:
        self._exec("ROLLBACK")
        self.in_transaction = False
        self._load()  # discard every in-memory change of the failed action

    def save(self, dirty_deals: Optional[Set[str]] = None) -> None:
        """Outside a unit of work (e.g. administrative changes): its own small transaction."""
        if self.in_transaction:
            return
        self._lock_begin()
        try:
            self._write(dirty_deals)
            self._exec("COMMIT")
        except Exception:
            self._exec("ROLLBACK")
            raise

    # ------------------------------------------------------------------ original documents (binary)
    FILE_COLS = ["file_id", "deal_ref", "item_id", "filename", "mime", "size", "sha256", "uploaded_at", "uploaded_by"]

    def save_file(self, meta: Dict[str, Any], data: bytes) -> None:
        """Stores an uploaded original document. Inside an action it is part of that action's transaction:
        if the action fails, the file is not kept either."""
        sql = (f"INSERT INTO files ({', '.join(self.FILE_COLS)}, content) VALUES ({', '.join('?' * (len(self.FILE_COLS) + 1))})")
        params = [meta.get(c) for c in self.FILE_COLS] + [data]
        if self.in_transaction:
            self._exec(sql, params)
        else:
            self._lock_begin()
            try:
                self._exec(sql, params)
                self._exec("COMMIT")
            except Exception:
                self._exec("ROLLBACK")
                raise

    def load_file(self, file_id: str):
        row = self._exec(f"SELECT {', '.join(self.FILE_COLS)}, content FROM files WHERE file_id = ?", [file_id]).fetchone()
        if not row:
            return None, None
        return dict(zip(self.FILE_COLS, row[:-1])), bytes(row[-1])

    def mark(self, table: str, key: str) -> None:
        """A tracked record (SWIFT message, outbox item, exception) was changed in place."""
        self._dirty.setdefault(table, set()).add(key)

    def refresh_if_changed(self) -> bool:
        if self.in_transaction:
            return False
        if self._db_version() != self.version:
            self._load()
            return True
        return False

    def reset(self) -> None:
        self.state = seed_state()
        self._write_all()

    def describe(self) -> str:
        return "postgresql" if self.pg else f"sqlite:{self.url.split('sqlite:///', 1)[-1]}"

    def healthy(self) -> bool:
        try:
            self._exec("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def backup(self, target: str):
        if self.pg:
            raise RuntimeError("Use pg_dump for PostgreSQL backups")
        dest = sqlite3.connect(target)
        with dest:
            self.conn.backup(dest)
        dest.close()


def open_store(seed: bool = True):
    """The store configured in settings: database (default), or JSON file / memory for demos and tests."""
    from .. import settings
    from .state import StateStore

    url = settings.database_url()
    if url:
        return DatabaseStore(url, seed=seed, legacy_json_path=settings.state_file())
    return StateStore(path=settings.state_file(), seed=seed)


def main():
    ap = argparse.ArgumentParser(description="Auto-Connect database tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="online backup of the SQLite database")
    b.add_argument("target")
    sub.add_parser("info")
    args = ap.parse_args()
    store = open_store()
    if args.cmd == "backup":
        store.backup(args.target)
        print(f"Backup written to {args.target}")
    else:
        st = store.state
        print(f"{store.describe()}: {len(st.deals)} deals, {len(st.ledger)} ledger entries, books balanced: {st.ledger_balanced()}")


if __name__ == "__main__":
    main()
