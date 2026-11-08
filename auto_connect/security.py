"""
Users, roles, permissions and the tamper-evident audit trail.

Production mode (AUTO_CONNECT_ENV=production, or a users file present): every API call carries the personal
key of a named user (X-API-Key). Keys are stored only as SHA-256 hashes. Each endpoint is allowed to certain
roles, exception decisions are taken by the role responsible for the category, and high-risk approvals need
two different people (four-eyes) who are not the person who entered the deal (maker-checker).

Demo mode (no users file): the single optional AUTO_CONNECT_API_KEY of earlier versions applies.

Create users:  python -m auto_connect.security add-user --id j.mueller --name "Julia Mueller" --roles COMPLIANCE
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import settings

ROLES = {
    "VIEWER": "Read-only access (audit, management)",
    "OPERATIONS": "Trade operations: enters applications, presentations, demands, claims; runs the daily batch",
    "DOC_CHECKER": "Document checker: verifies automatically read documents and data-quality holds",
    "COMPLIANCE": "Compliance / AML: decides sanctions and KYC holds",
    "CREDIT": "Credit risk: decides limit excesses and open-ended exposures",
    "LEGAL": "Legal: decides disputed surety and guarantee claims",
    "ADMIN": "IT administration: connects applications, manages reference data and configuration",
    "CLIENT": "Corporate client user: client portal only, sees and acts on its own company's transactions",
}
# which role decides an exception of a category, and how many different approvers an APPROVAL needs
EXCEPTION_ROLE = {"SANCTIONS": "COMPLIANCE", "KYC": "COMPLIANCE", "CREDIT": "CREDIT", "LEGAL": "LEGAL",
                  "DATA_QUALITY": "DOC_CHECKER", "OPERATIONS": "OPERATIONS"}
APPROVALS_REQUIRED = {"SANCTIONS": 2, "CREDIT": 2}

_OPS = {"OPERATIONS"}
# (method, path regex, roles allowed). ADMIN may do everything; unlisted write calls are ADMIN only.
RULES = [
    ("GET", r"^/api/v1/portal/", {"CLIENT"}),
    ("POST", r"^/api/v1/portal/", {"CLIENT"}),
    ("GET", r"^/api/v1/audit", {"ADMIN", "VIEWER"}),
    ("GET", r"^/(api|agents|\.well-known)/", {"VIEWER", "OPERATIONS", "DOC_CHECKER", "COMPLIANCE", "CREDIT", "LEGAL"}),
    ("POST", r"^/api/v1/tf/exceptions/[^/]+/resolve$", {"OPERATIONS", "DOC_CHECKER", "COMPLIANCE", "CREDIT", "LEGAL"}),
    ("POST", r"^/api/v1/tf/lc/[^/]+/presentations/[^/]+/documents$", {"DOC_CHECKER"}),
    ("POST", r"^/api/v1/tf/documents/read$", _OPS | {"DOC_CHECKER"}),
    ("POST", r"^/api/v1/tf/(lc|guarantees|sureties|export-lc|collections)$", _OPS),
    ("POST", r"^/api/v1/tf/(lc|guarantees|sureties|undertakings|export-lc|collections)/[^/]+/.+$", _OPS),
    ("POST", r"^/api/v1/tf/outbox/dispatch$", _OPS),
    ("POST", r"^/api/v1/staff/applications(/[^/]+/retry)?$", _OPS),
    ("POST", r"^/api/v1/staff/exceptions/[^/]+/return-to-client$", _OPS | {"DOC_CHECKER"}),
    ("POST", r"^/api/v1/staff/deals/[^/]+/presentations/[^/]+/re-examine$", _OPS | {"DOC_CHECKER"}),
    ("POST", r"^/api/v1/tf/batch$", _OPS),
    ("POST", r"^/api/v1/tf/reference/refresh$", _OPS),
    ("POST", r"^/api/v1/(orchestrate|trade/examine-application)$", _OPS),
]
PUBLIC = re.compile(r"^/($|trade$|portal$|bank$|portal/login-info$|bank/login-info$|assets/|apps/|portal/decision|"
                    r"health$|ready$|docs|openapi\.json|static/)")


def production() -> bool:
    return os.environ.get("AUTO_CONNECT_ENV", "").strip().lower() == "production"


def users_file() -> str:
    return os.environ.get("AUTO_CONNECT_USERS_FILE", "").strip() or os.path.join(settings.PROJECT_DIR, "state", "users.json")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class UserDirectory:
    def __init__(self, path: Optional[str] = None):
        self.path = path or users_file()
        self.users: List[Dict[str, Any]] = []
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                self.users = json.load(f).get("users", [])

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    def authenticate(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not key:
            return None
        h = hash_key(key)
        return next((u for u in self.users if u.get("active", True) and hmac.compare_digest(u["api_key_sha256"], h)), None)

    def add(self, user_id: str, name: str, roles: List[str], email: str = "", customer_id: str = "") -> str:
        bad = [r for r in roles if r not in ROLES]
        if bad:
            raise ValueError(f"Unknown roles {bad}; choose from {list(ROLES)}")
        if "CLIENT" in roles and (len(roles) > 1 or not customer_id):
            raise ValueError("A CLIENT user needs --customer-id and no other role (clients never get bank roles)")
        if any(u["user_id"] == user_id for u in self.users):
            raise ValueError(f"User {user_id} exists")
        key = "demo_" + secrets.token_urlsafe(32)
        self.users.append({"user_id": user_id, "name": name, "email": email, "roles": roles, "active": True,
                           "api_key_sha256": hash_key(key), "created_at": datetime.now(timezone.utc).isoformat(),
                           **({"customer_id": customer_id} if customer_id else {})})
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"users": self.users}, f, indent=1)
        os.chmod(self.path, 0o600)
        return key


def allowed(user: Dict[str, Any], method: str, path: str) -> bool:
    roles = set(user.get("roles", []))
    if "ADMIN" in roles:
        return True
    for m, rx, ok in RULES:
        if m == method and re.search(rx, path):
            return bool(roles & ok)
    return False


# ---------------------------------------------------------------------------- audit trail
class AuditLog:
    """Append-only JSON-lines log; every record carries the hash of the previous one (tamper-evident)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.environ.get("AUTO_CONNECT_AUDIT_FILE", "").strip() or \
            os.path.join(settings.PROJECT_DIR, "state", "audit", "audit.jsonl")
        self.lock = threading.Lock()

    def _last_hash(self) -> str:
        if not os.path.exists(self.path):
            return "0" * 64
        with open(self.path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", errors="replace").strip().splitlines()
        return json.loads(lines[-1])["hash"] if lines else "0" * 64

    def record(self, **event):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            entry = {"at": datetime.now(timezone.utc).isoformat(), **event, "prev": self._last_hash()}
            entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def verify(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {"valid": True, "records": 0}
        prev, n = "0" * 64, 0
        with open(self.path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                e = json.loads(line)
                h = e.pop("hash")
                if e.get("prev") != prev or hashlib.sha256(json.dumps(e, sort_keys=True, default=str).encode()).hexdigest() != h:
                    return {"valid": False, "records": n, "broken_at_record": n}
                prev = h
        return {"valid": True, "records": n}


def main():
    ap = argparse.ArgumentParser(description="Manage Auto-Connect users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("--id", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--roles", required=True, help=f"comma separated: {','.join(ROLES)}")
    a.add_argument("--email", default="")
    a.add_argument("--customer-id", default="", help="for CLIENT users: the company they act for")
    sub.add_parser("list")
    sub.add_parser("verify-audit")
    args = ap.parse_args()
    d = UserDirectory()
    if args.cmd == "add-user":
        key = d.add(args.id, args.name, [r.strip().upper() for r in args.roles.split(",")], args.email, args.customer_id)
        print(f"User {args.id} created. Personal API key (shown once, store it safely):\n{key}")
    elif args.cmd == "list":
        for u in d.users:
            print(f"{u['user_id']:20} {u['name']:30} {','.join(u['roles'])}")
    else:
        print(AuditLog().verify())


if __name__ == "__main__":
    main()
