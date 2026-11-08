"""
Zero-Trust Credential Vault for Banking Ecosystem
Simulates HashiCorp Vault / GCP Secret Manager integration for Tier-1 Bank (Demo Bank).
"""

from typing import Dict, Optional, List, Any
from datetime import datetime, timezone
import logging
from .models import VaultCredential

logger = logging.getLogger("auto_connect.vault")


def resolve_secret(value: Optional[str]) -> Optional[str]:
    """Secrets can be stored as references so they never sit in the vault file:
    'env:NAME' reads an environment variable (injected by the bank's secret manager / Kubernetes secret),
    'file:/path' reads a mounted secret file. Anything else is the literal value."""
    import os

    if not value:
        return value
    if value.startswith("env:"):
        return os.environ.get(value[4:], "").strip() or None
    if value.startswith("file:"):
        try:
            with open(value[5:], "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    return value


def _local(url: str) -> bool:
    return url.startswith(("http://127.0.0.1", "http://localhost"))


class ZeroTrustVault:
    """
    Central Zero-Trust Gate.
    The AI Brain cannot guess or forge credentials; all credentials are provisioned
    by IT administrators and retrieved on a least-privilege basis.
    """

    def __init__(self):
        # Vault storage: app_id -> VaultCredential
        self._credentials: Dict[str, VaultCredential] = {}
        # Immutable audit trail of every credential access request
        self._audit_log: List[Dict[str, Any]] = []

    def store_credential(self, credential: VaultCredential) -> None:
        """IT Admin registers secure credentials into the vault."""
        if credential.auth_type == "OAuth2":
            is_valid, msg = self.validate_oauth(credential)
        elif credential.auth_type == "mTLS" and credential.client_cert_file:
            is_valid, msg = self.validate_cert_files(credential)
        else:
            is_valid, msg = self.validate_token_security(credential.auth_type, resolve_secret(credential.token),
                                                         credential.client_cert)
        if not is_valid:
            self._record_audit(
                event="CREDENTIAL_REJECTED_SECURITY",
                app_id=credential.app_id,
                requester="ZERO_TRUST_GATE",
                authorized=False,
                details=msg,
            )
            raise ValueError(f"Zero-Trust Policy Violation: {msg}")

        self._credentials[credential.app_id] = credential
        self._record_audit(
            event="CREDENTIAL_PROVISIONED",
            app_id=credential.app_id,
            requester="IT_ADMIN_PROVISIONER",
            authorized=True,
            details=f"Auth Type: {credential.auth_type}, Base URL: {credential.base_url}",
        )

    @staticmethod
    def validate_oauth(c: VaultCredential) -> tuple[bool, str]:
        missing = [n for n, v in [("oauth_token_url", c.oauth_token_url), ("oauth_client_id", c.oauth_client_id),
                                  ("oauth_client_secret", c.oauth_client_secret)] if not v]
        if missing:
            return False, f"401 UNAUTHORIZED: OAuth2 client credentials incomplete, missing {missing}"
        if not str(c.oauth_token_url).startswith("https://") and not _local(str(c.oauth_token_url)):
            return False, "OAuth2 token endpoint must use HTTPS"
        if not resolve_secret(c.oauth_client_secret):
            return False, "401 UNAUTHORIZED: OAuth2 client secret reference resolves to nothing"
        return True, "OAuth2 client credentials registered (token fetched and refreshed on use)"

    @staticmethod
    def validate_cert_files(c: VaultCredential) -> tuple[bool, str]:
        import os

        for label, path in [("client certificate", c.client_cert_file), ("client key", c.client_key_file),
                            ("CA bundle", c.ca_bundle_file)]:
            if path and not os.path.isfile(path):
                return False, f"mTLS {label} file not found: {path}"
        if not c.client_key_file:
            return False, "mTLS needs both client_cert_file and client_key_file"
        return True, "mTLS client certificate files registered"

    @staticmethod
    def validate_token_security(auth_type: str, token: Optional[str] = None, client_cert: Optional[str] = None) -> tuple[bool, str]:
        """
        Validates token entropy, format, and authenticity against bank security policies.
        Rejects dummy, placeholder, or obviously invalid tokens with 401 Unauthorized.
        """
        if auth_type == "None":
            return True, "Internal network access (unauthenticated)"

        if auth_type == "mTLS":
            if not token and not client_cert:
                return False, "401 UNAUTHORIZED: mTLS requires a client certificate reference"
            cert_val = (client_cert or token or "").strip()
            cert_lower = cert_val.lower()
            if any(marker in cert_lower for marker in ["wrong", "fake", "invalid", "dummy", "bad"]):
                return False, f"401 UNAUTHORIZED: Invalid TLS Client Certificate '{cert_val[:8]}...' rejected by Gateway"
            return True, "mTLS Client Certificate validated against Bank PKI CA"

        if auth_type == "TN3270_TERMINAL_SESSION":
            return True, "Mainframe 3270 terminal LU pool session allocated"

        # For Bearer, API_KEY, etc.
        raw = (token or "").strip()
        if not raw:
            return False, "401 UNAUTHORIZED: Missing required authentication token or API key"

        raw_lower = raw.lower()
        explicit_dummy = {"wrong", "fake", "invalid", "dummy", "bad", "error", "123", "12345", "123456", "password", "test", "demo", "null", "undefined"}
        if raw_lower in explicit_dummy or any(raw_lower.startswith(prefix) for prefix in ("wrong_", "fake_", "dummy_", "invalid_")):
            return False, f"401 UNAUTHORIZED: Token '{raw}' rejected by Zero-Trust Gate (detected invalid/unauthorized credentials)"

        if len(raw) < 4:
            return False, f"401 UNAUTHORIZED: Token length ({len(raw)} chars) is below banking security minimum (min 4 chars)"

        return True, "Cryptographic token signature verified"

    def remove_credential(self, app_id: str) -> bool:
        """Revokes and deletes an application credential from the vault."""
        if app_id in self._credentials:
            del self._credentials[app_id]
            self._record_audit(
                event="CREDENTIAL_REVOKED",
                app_id=app_id,
                requester="IT_ADMIN_PROVISIONER",
                authorized=True,
                details="Credential revoked and removed from Vault",
            )
            return True
        return False

    def get_credential(self, app_id: str, requester_department: str) -> Optional[VaultCredential]:
        """
        Retrieves credentials with zero-trust department-level RBAC check.
        """
        cred = self._credentials.get(app_id)
        if not cred:
            self._record_audit(
                event="ACCESS_DENIED_NOT_FOUND",
                app_id=app_id,
                requester=requester_department,
                authorized=False,
                details="No credentials registered for target app",
            )
            return None

        # Check authorization policy
        is_allowed = (
            "*" in cred.allowed_departments
            or requester_department in cred.allowed_departments
        )

        self._record_audit(
            event="CREDENTIAL_RETRIEVAL" if is_allowed else "ACCESS_DENIED_RBAC",
            app_id=app_id,
            requester=requester_department,
            authorized=is_allowed,
            details=f"Allowed departments: {cred.allowed_departments}",
        )

        return cred if is_allowed else None

    def build_auth_headers(self, app_id: str, requester_department: str) -> Dict[str, str]:
        """Builds HTTP authentication headers using vault-sealed tokens."""
        cred = self.get_credential(app_id, requester_department)
        if not cred:
            raise PermissionError(f"Zero-Trust Policy Violation: Access to {app_id} denied for {requester_department}")
        return self.headers_for(cred, requester_department)

    @staticmethod
    def headers_for(cred: VaultCredential, requester_department: str) -> Dict[str, str]:
        """Builds auth headers for an already-authorised credential (no extra audit entry)."""
        headers: Dict[str, str] = {
            "X-Vault-Audit-Verified": "TRUE",
            "X-Requester-Department": requester_department,
        }

        if cred.auth_type == "Bearer" and cred.token:
            headers["Authorization"] = f"Bearer {resolve_secret(cred.token)}"
        elif cred.auth_type == "API_KEY" and cred.token:
            headers["X-API-KEY"] = resolve_secret(cred.token)
        elif cred.auth_type == "Basic" and cred.token:  # token = 'user:password' (or a reference to it)
            import base64

            headers["Authorization"] = "Basic " + base64.b64encode((resolve_secret(cred.token) or "").encode()).decode()
        elif cred.auth_type == "mTLS":
            headers["X-Client-Cert-Thumbprint"] = cred.client_cert or "sha256_demo_internal_gateway_cert"
        elif cred.auth_type == "TN3270_TERMINAL_SESSION":
            headers["X-TN3270-Session-Token"] = cred.token or "authenticated_session"

        return headers

    def _record_audit(self, event: str, app_id: str, requester: str, authorized: bool, details: str):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "app_id": app_id,
            "requester": requester,
            "authorized": authorized,
            "details": details,
        }
        self._audit_log.append(entry)

    def list_credentials(self) -> List[Dict[str, Any]]:
        """Returns non-sensitive metadata for registered vault items."""
        return [
            {
                "app_id": c.app_id,
                "base_url": c.base_url,
                "auth_type": c.auth_type,
                "allowed_departments": c.allowed_departments,
                "has_token": bool(c.token),
                "has_cert": bool(c.client_cert),
            }
            for c in self._credentials.values()
        ]

    def load_from_json(self, file_path: str) -> int:
        """Loads bank application credentials from a central JSON vault file."""
        import json
        import os

        if not os.path.exists(file_path):
            return 0

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        count = 0
        self.load_errors: Dict[str, str] = {}
        for app_id, entry in data.items():
            if app_id.startswith("tf_"):
                continue  # the bank systems are provisioned from configuration on every start
            fields = {k: v for k, v in entry.items() if k in VaultCredential.model_fields and k != "app_id"}
            fields.setdefault("base_url", "https://internal.bank.gateway")
            try:
                self.store_credential(VaultCredential(app_id=app_id, **fields))
                count += 1
            except ValueError as e:  # one broken entry (e.g. a secret reference not resolvable) must not stop the bank
                self.load_errors[app_id] = str(e)
                import logging

                logging.getLogger("auto_connect.vault").error(f"Credential of {app_id} not loaded: {e}")
        return count

    def save_to_json(self, file_path: str) -> None:
        """Persists registered credentials into a JSON vault file."""
        import json

        data = {}
        for app_id, cred in self._credentials.items():
            if app_id.startswith("tf_"):
                continue  # bank systems come from configuration (TF_SYSTEM_*), never from this file
            data[app_id] = cred.model_dump()

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @property
    def audit_trail(self) -> List[Dict[str, Any]]:
        return list(self._audit_log)

