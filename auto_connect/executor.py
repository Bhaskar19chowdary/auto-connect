"""
Single Universal REST/GraphQL/OData/MCP Executor
Executes dynamic API calls using Zero-Trust Vault credentials without hardcoded functions.
"""

from typing import Dict, Any, Optional, Tuple
import contextvars
import os
import time
import logging
import uuid
from urllib.parse import urlparse
from .vault import ZeroTrustVault, resolve_secret

logger = logging.getLogger("auto_connect.executor")

# Correlation id of the business action in progress (e.g. the deal reference) - sent with every outbound call so
# one LC can be traced across all systems in the API gateway's logs.
correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("correlation_id", default=None)


def allowed_hosts() -> list:
    """AUTO_CONNECT_ALLOWED_HOSTS=apigw.bank.internal,login.microsoftonline.com: in production every outbound call must
    go to one of these hosts (normally only the bank's API gateway and identity provider)."""
    return [h.strip().lower() for h in os.environ.get("AUTO_CONNECT_ALLOWED_HOSTS", "").split(",") if h.strip()]


def host_allowed(url: str) -> bool:
    hosts = allowed_hosts()
    if not hosts:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


class UniversalAPIExecutor:
    """
    Universal Dynamic Executor.
    Takes any discovered endpoint, inspects the vault for credentials and executes the call against live APIs
    (API key, bearer token, OAuth 2.0 client credentials, mutual TLS, corporate proxy) or registered in-process
    simulators.
    """

    def __init__(self, vault: ZeroTrustVault):
        self.vault = vault
        # Optional in-process mock handlers: app_id -> callable(method, path, params, headers)
        self._in_process_handlers: Dict[str, Any] = {}
        # Optional httpx transport (tests inject one that routes to local ASGI apps)
        self.transport = None
        self.timeout_seconds = 15.0
        self._tokens: Dict[str, Tuple[str, float]] = {}  # app_id -> (OAuth access token, expiry epoch)

    def http_client(self, app_id: Optional[str] = None):
        """httpx client for live calls: the app's client certificate (mTLS), the bank CA bundle and proxy."""
        import httpx

        kwargs: Dict[str, Any] = {"timeout": self.timeout_seconds, "follow_redirects": True}
        cred = self.vault._credentials.get(app_id) if app_id else None
        ca = (cred.ca_bundle_file if cred is not None else None) or os.environ.get("AUTO_CONNECT_CA_BUNDLE", "").strip()
        cert = cred.client_cert_file if cred is not None else None
        if ca or cert:
            import ssl

            ctx = ssl.create_default_context(cafile=ca or None)  # bank PKI for the server certificate
            if cert:  # our client certificate, presented in the TLS handshake (mutual TLS)
                ctx.load_cert_chain(cert, cred.client_key_file)
            kwargs["verify"] = ctx
        proxy = os.environ.get("AUTO_CONNECT_HTTPS_PROXY", "").strip()
        if proxy:
            kwargs["proxy"] = proxy
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.AsyncClient(**kwargs)

    async def oauth_token(self, app_id: str, force: bool = False) -> str:
        """OAuth 2.0 client-credentials grant; the token is cached until shortly before it expires."""
        cred = self.vault._credentials[app_id]
        cached = self._tokens.get(app_id)
        if cached and not force and cached[1] > time.time() + 30:
            return cached[0]
        if not host_allowed(cred.oauth_token_url):
            raise PermissionError(f"Token endpoint {cred.oauth_token_url} is not on the outbound allow-list")
        form = {"grant_type": "client_credentials", "client_id": cred.oauth_client_id,
                "client_secret": resolve_secret(cred.oauth_client_secret)}
        if cred.oauth_scope:
            form["scope"] = cred.oauth_scope
        async with self.http_client(app_id) as client:
            r = await client.post(cred.oauth_token_url, data=form, headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise PermissionError(f"OAuth token request failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        token = body.get("access_token")
        if not token:
            raise PermissionError("OAuth token response has no access_token")
        self._tokens[app_id] = (token, time.time() + float(body.get("expires_in", 300)))
        return token

    async def auth_headers(self, app_id: str, requester_department: str, force_refresh: bool = False) -> Dict[str, str]:
        """All headers needed to call the app: vault credentials (incl. a fresh OAuth token) + correlation id."""
        headers = {k: v for k, v in self.vault.build_auth_headers(app_id, requester_department).items()
                   if not k.startswith("X-Vault")}
        cred = self.vault._credentials[app_id]
        if cred.auth_type == "OAuth2":
            headers["Authorization"] = f"Bearer {await self.oauth_token(app_id, force_refresh)}"
        cid = correlation_id.get() or f"AC-{uuid.uuid4().hex[:16]}"
        headers["X-Correlation-ID"] = cid
        headers["X-Request-ID"] = uuid.uuid4().hex
        return headers

    def has_in_process_handler(self, app_id: str) -> bool:
        return app_id in self._in_process_handlers

    def unregister_in_process_handler(self, app_id: str) -> None:
        self._in_process_handlers.pop(app_id, None)

    def register_in_process_handler(self, app_id: str, handler_func: Any) -> None:
        """Allows mock bank engines to be called locally during tests and simulations."""
        self._in_process_handlers[app_id] = handler_func

    async def execute_dynamic_call(
        self,
        app_id: str,
        endpoint_path: str,
        http_method: str,
        parameters: Dict[str, Any],
        requester_department: str,
    ) -> Dict[str, Any]:
        """
        Executes a live call to the discovered application endpoint using Vault credentials.
        """
        start_time = time.perf_counter()

        # Step 1: Zero-Trust Security Gate validation & header injection
        # (single vault lookup -> exactly one audit entry per call)
        cred = self.vault.get_credential(app_id, requester_department)
        if not cred:
            raise PermissionError(f"Access to application '{app_id}' forbidden by Zero-Trust Gate.")
        auth_headers = self.vault.headers_for(cred, requester_department)

        # Step 2: In-Process Simulation Check
        if app_id in self._in_process_handlers:
            handler = self._in_process_handlers[app_id]
            res = await handler(http_method.upper(), endpoint_path, parameters, auth_headers)
            if not isinstance(res, dict):
                res = {"status_code": 500, "data": {"error": f"Handler for {app_id} returned no response"}}
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status_code": res.get("status_code", 200),
                "data": res.get("data", res),
                "latency_ms": round(elapsed_ms, 2),
                "target_endpoint": f"{cred.base_url}{endpoint_path}",
                "auth_type_used": cred.auth_type,
            }

        # Step 3: Handle Legacy UI Terminal Keystroke / Screen Actions
        method = http_method.upper()
        if method == "UI_ACTION":  # never pretend: there is no live 3270 terminal connection in this platform
            return {
                "status_code": 501,
                "data": {"error": "No live 3270 / terminal connection is configured for this application. Green-screen "
                                  "actions run only against the built-in simulator; connect the host through an API or "
                                  "MQ interface instead."},
                "latency_ms": 0,
                "target_endpoint": f"{cred.base_url}{endpoint_path}",
                "auth_type_used": cred.auth_type,
            }

        # Step 3: message-based systems (shared folder, SFTP, IBM MQ)
        if (cred.transport or "HTTP").upper() != "HTTP":
            from . import transports

            return await transports.send(self, cred, app_id, http_method.upper(), endpoint_path, dict(parameters or {}),
                                         auth_headers, correlation_id.get() or f"AC-{uuid.uuid4().hex[:16]}")

        # Step 4: Live Network Call via httpx
        method = http_method.upper()
        parameters = dict(parameters or {})
        # substitute OpenAPI path parameters, e.g. /customers/{customer_id}
        import re as _re

        for name in _re.findall(r"{([^}]+)}", endpoint_path):
            if name in parameters:
                endpoint_path = endpoint_path.replace("{" + name + "}", str(parameters.pop(name)))
        url = f"{cred.base_url.rstrip('/')}/{endpoint_path.lstrip('/')}"
        if not host_allowed(url):
            return {"status_code": 403, "data": {"error": f"{urlparse(url).hostname} is not on the outbound allow-list "
                                                          f"(AUTO_CONNECT_ALLOWED_HOSTS) - route the call via the API gateway"},
                    "target_endpoint": url, "latency_ms": 0}

        async def send(force_refresh: bool = False):
            headers = {"Content-Type": "application/json", "Accept": "application/json", **auth_headers}
            headers.pop("X-Vault-Audit-Verified", None)
            if cred.auth_type == "OAuth2":
                headers["Authorization"] = f"Bearer {await self.oauth_token(app_id, force_refresh)}"
            headers["X-Correlation-ID"] = correlation_id.get() or f"AC-{uuid.uuid4().hex[:16]}"
            headers["X-Request-ID"] = uuid.uuid4().hex
            async with self.http_client(app_id) as client:
                if method == "GET":
                    return await client.get(url, params=parameters, headers=headers)
                if method in ["POST", "PUT", "PATCH"]:
                    return await client.request(method, url, json=parameters, headers=headers)
                if method == "DELETE":
                    return await client.delete(url, params=parameters, headers=headers)
                raise ValueError(f"Unsupported HTTP method: {method}")

        try:
            response = await send()
            if response.status_code == 401 and cred.auth_type == "OAuth2":  # token revoked / expired early
                response = await send(force_refresh=True)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            try:
                data = response.json()
            except Exception:
                data = {"raw_text": response.text}
            return {
                "status_code": response.status_code,
                "data": data,
                "latency_ms": round(elapsed_ms, 2),
                "target_endpoint": url,
                "auth_type_used": cred.auth_type,
            }
        except PermissionError as e:  # OAuth token could not be obtained
            return {"status_code": 401, "data": {"error": str(e)}, "target_endpoint": url, "latency_ms": 0}
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error(f"Universal Executor error calling {url}: {e}")
            return {
                "status_code": 503,
                "network_error": True,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": round(elapsed_ms, 2),
                "target_endpoint": url,
            }
