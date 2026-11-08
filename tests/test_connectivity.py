"""Real connectivity: OAuth 2.0 client credentials, mutual TLS with real certificates, correlation ids,
outbound allow-list and proxy - against live local HTTPS / HTTP servers."""
import asyncio
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

from auto_connect.executor import UniversalAPIExecutor, correlation_id
from auto_connect.models import VaultCredential
from auto_connect.vault import ZeroTrustVault

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl needed to create test certificates")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(app, **ssl_kwargs):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", **ssl_kwargs))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    return server, port


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")

    def run(*args):
        subprocess.run(["openssl", *args], cwd=d, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=Test Bank CA")
    for name, extra in [("server", ["-addext", "subjectAltName=IP:127.0.0.1"]), ("client", [])]:
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}")
        ext = d / f"{name}.ext"
        ext.write_text("subjectAltName=IP:127.0.0.1\n" if name == "server" else "extendedKeyUsage=clientAuth\n")
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", f"{name}.pem", "-days", "2", "-extfile", str(ext))
    return d


def test_mutual_tls_with_real_certificates(pki):
    app = FastAPI()

    @app.get("/accounts/{iban}/balance")
    async def balance(iban: str):
        return {"iban": iban, "balance": "1000.00"}
    server, port = serve(app, ssl_certfile=str(pki / "server.pem"), ssl_keyfile=str(pki / "server.key"),
                         ssl_ca_certs=str(pki / "ca.pem"), ssl_cert_reqs=ssl.CERT_REQUIRED)
    try:
        vault = ZeroTrustVault()
        vault.store_credential(VaultCredential(app_id="core", base_url=f"https://127.0.0.1:{port}", auth_type="mTLS",
                                               client_cert_file=str(pki / "client.pem"), client_key_file=str(pki / "client.key"),
                                               ca_bundle_file=str(pki / "ca.pem")))
        vault.store_credential(VaultCredential(app_id="nocert", base_url=f"https://127.0.0.1:{port}", auth_type="None",
                                               ca_bundle_file=str(pki / "ca.pem")))
        ex = UniversalAPIExecutor(vault)
        ok = asyncio.run(ex.execute_dynamic_call("core", "/accounts/{iban}/balance", "GET", {"iban": "DE89"}, "TRADE_FINANCE"))
        assert ok["status_code"] == 200 and ok["data"]["balance"] == "1000.00"
        refused = asyncio.run(ex.execute_dynamic_call("nocert", "/accounts/{iban}/balance", "GET", {"iban": "DE89"}, "TRADE_FINANCE"))
        assert refused["status_code"] == 503 and refused.get("network_error")  # TLS handshake rejected without certificate
        with pytest.raises(ValueError):  # missing key file is refused at registration
            vault.store_credential(VaultCredential(app_id="bad", base_url="https://x", auth_type="mTLS",
                                                   client_cert_file=str(pki / "client.pem")))
    finally:
        server.should_exit = True


def test_oauth_client_credentials_refresh_correlation_and_allow_list(monkeypatch):
    issued, seen = {}, {}
    idp = FastAPI()

    @idp.post("/oauth2/token")
    async def token(request: Request):
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant_type, client_id, client_secret, scope = (form.get("grant_type"), form.get("client_id"),
                                                       form.get("client_secret"), form.get("scope"))
        if grant_type != "client_credentials" or client_id != "auto-connect" or client_secret != "s3cr3t-from-env":
            raise HTTPException(401, "invalid_client")
        tok = f"tok{len(issued) + 1}"
        issued[tok] = scope
        return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600}
    idp_server, idp_port = serve(idp)

    api = FastAPI()
    state = {"revoked": set()}

    @api.get("/limits/{customer}")
    async def limits(customer: str, request: Request, authorization: str = Header(None)):
        tok = (authorization or "").replace("Bearer ", "")
        if tok not in issued or tok in state["revoked"]:
            raise HTTPException(401, "invalid token")
        seen["correlation"] = request.headers.get("x-correlation-id")
        return {"customer": customer, "available": "5000000"}
    api_server, api_port = serve(api)
    try:
        monkeypatch.setenv("LIMITS_CLIENT_SECRET", "s3cr3t-from-env")
        vault = ZeroTrustVault()
        vault.store_credential(VaultCredential(
            app_id="limits", base_url=f"http://127.0.0.1:{api_port}", auth_type="OAuth2",
            oauth_token_url=f"http://127.0.0.1:{idp_port}/oauth2/token", oauth_client_id="auto-connect",
            oauth_client_secret="env:LIMITS_CLIENT_SECRET", oauth_scope="limits.read"))
        ex = UniversalAPIExecutor(vault)

        async def call():
            correlation_id.set("ILC2026000042")
            return await ex.execute_dynamic_call("limits", "/limits/{customer}", "GET", {"customer": "DE-CORP-40551"}, "TRADE_FINANCE")
        r = asyncio.run(call())
        assert r["status_code"] == 200 and r["data"]["available"] == "5000000"
        assert seen["correlation"] == "ILC2026000042" and issued["tok1"] == "limits.read"
        assert "s3cr3t" not in str(vault._credentials["limits"].oauth_client_secret)  # only the reference is stored
        asyncio.run(call())
        assert len(issued) == 1  # cached token reused
        state["revoked"].add("tok1")
        r = asyncio.run(call())
        assert r["status_code"] == 200 and len(issued) == 2  # 401 -> fresh token -> retried once

        monkeypatch.setenv("LIMITS_CLIENT_SECRET", "wrong")
        ex2 = UniversalAPIExecutor(vault)
        r = asyncio.run(ex2.execute_dynamic_call("limits", "/limits/{c}", "GET", {"c": "x"}, "TRADE_FINANCE"))
        assert r["status_code"] == 401 and "OAuth token request failed" in r["data"]["error"]

        monkeypatch.setenv("AUTO_CONNECT_ALLOWED_HOSTS", "apigw.bank.internal")
        r = asyncio.run(call())
        assert r["status_code"] == 403 and "allow-list" in r["data"]["error"]
        monkeypatch.delenv("AUTO_CONNECT_ALLOWED_HOSTS")

        monkeypatch.setenv("AUTO_CONNECT_HTTPS_PROXY", f"http://127.0.0.1:{free_port()}")  # nothing listens there
        monkeypatch.setenv("LIMITS_CLIENT_SECRET", "s3cr3t-from-env")
        ex3 = UniversalAPIExecutor(vault)
        r = asyncio.run(ex3.execute_dynamic_call("limits", "/limits/{c}", "GET", {"c": "x"}, "TRADE_FINANCE"))
        assert r["status_code"] in (401, 503)  # the call really went to the (dead) proxy
    finally:
        idp_server.should_exit = True
        api_server.should_exit = True
