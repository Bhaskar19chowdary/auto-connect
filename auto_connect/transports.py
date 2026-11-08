"""
Non-HTTP connections to bank systems: shared folders, SFTP and IBM MQ.

Many core bank systems are not reached through REST APIs but by exchanging messages:
* FILE    - a shared "hot folder" (NFS / SMB) watched by the target system, e.g. SWIFT Alliance Access file
            transfer or a core-banking batch interface. Replies (if any) are picked up from a reply folder.
* SFTP    - the same over SFTP (paramiko), user + password or private key from the vault.
* IBM_MQ  - IBM MQ through its official REST messaging API (no native MQ client libraries needed): the request is
            put on a queue, the reply is taken from the reply queue by correlation id.

A request is a JSON envelope {operation, method, path, params, request_id, correlation_id}, or - with the
transport_format 'RAW:<param>' - just the text of one parameter (e.g. the FIN / ISO 20022 message for SWIFT).
Without a reply channel the call is one-way: the answer confirms delivery (status 202) with a message id.
"""

from __future__ import annotations

import asyncio
import json
import os
import posixpath
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .vault import resolve_secret


def _envelope(cred, operation: str, method: str, path: str, params: Dict[str, Any], request_id: str,
              correlation: str) -> Tuple[str, str]:
    """(content, file extension)"""
    fmt = cred.transport_format or "JSON"
    if fmt.upper().startswith("RAW:"):
        text = str(params.get(fmt.split(":", 1)[1], ""))
        ext = "xml" if text.lstrip().startswith("<") else "fin" if text.startswith("{1:") else "txt"
        return text, ext
    return json.dumps({"operation": operation, "method": method, "path": path, "params": params,
                       "request_id": request_id, "correlation_id": correlation}, default=str), "json"


def _accepted(request_id: str, transport: str, where: str) -> Dict[str, Any]:
    return {"status_code": 202, "data": {"message_id": request_id, "status": "DELIVERED_TO_TRANSPORT",
                                         "transport": transport, "destination": where}}


def _reply(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        return {"status_code": 502, "data": {"error": "Reply is not JSON", "raw_text": text[:500]}}
    status = int(data.pop("status_code", 200)) if isinstance(data, dict) else 200
    return {"status_code": status, "data": data.get("data", data) if isinstance(data, dict) else data}


# ---------------------------------------------------------------------------- FILE
async def _file(cred, name: str, content: str, request_id: str) -> Dict[str, Any]:
    out_dir = cred.transport_target
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, f".{name}.part")
    with open(tmp, "w", encoding="utf-8") as f:  # write + rename: the watcher never sees a half-written file
        f.write(content)
    final = os.path.join(out_dir, name)
    os.replace(tmp, final)
    if not cred.transport_reply:
        return _accepted(request_id, "FILE", final)
    reply = os.path.join(cred.transport_reply, f"{request_id}.json")
    deadline = time.monotonic() + cred.reply_timeout_seconds
    while time.monotonic() < deadline:
        if os.path.exists(reply):
            with open(reply, "r", encoding="utf-8") as f:
                text = f.read()
            os.remove(reply)
            return _reply(text)
        await asyncio.sleep(0.2)
    return {"status_code": 504, "network_error": True, "error": f"No reply in {cred.transport_reply} within "
                                                                 f"{cred.reply_timeout_seconds}s"}


# ---------------------------------------------------------------------------- SFTP
def _sftp_session(cred):
    import paramiko

    u = urlparse(cred.base_url)  # sftp://user@host:22
    transport = paramiko.Transport((u.hostname, u.port or 22))
    password = resolve_secret(cred.token)
    if cred.client_key_file:
        key = paramiko.RSAKey.from_private_key_file(cred.client_key_file, password=password)
        transport.connect(username=u.username, pkey=key)
    else:
        transport.connect(username=u.username, password=password)
    return transport, paramiko.SFTPClient.from_transport(transport)


def _sftp_put_get(cred, name: str, content: str, request_id: str) -> Dict[str, Any]:
    transport, sftp = _sftp_session(cred)
    try:
        tmp, final = posixpath.join(cred.transport_target, f".{name}.part"), posixpath.join(cred.transport_target, name)
        with sftp.open(tmp, "w") as f:
            f.write(content.encode("utf-8"))
        sftp.rename(tmp, final)
        if not cred.transport_reply:
            return _accepted(request_id, "SFTP", final)
        reply = posixpath.join(cred.transport_reply, f"{request_id}.json")
        deadline = time.monotonic() + cred.reply_timeout_seconds
        while time.monotonic() < deadline:
            try:
                with sftp.open(reply, "r") as f:
                    text = f.read().decode("utf-8")
                sftp.remove(reply)
                return _reply(text)
            except IOError:
                time.sleep(0.3)
        return {"status_code": 504, "network_error": True, "error": f"No SFTP reply within {cred.reply_timeout_seconds}s"}
    finally:
        sftp.close()
        transport.close()


# ---------------------------------------------------------------------------- IBM MQ (REST messaging API)
async def _mq(executor, cred, app_id: str, content: str, request_id: str, auth_headers: Dict[str, str]) -> Dict[str, Any]:
    qmgr, queue = cred.transport_target.split("/", 1)
    base = cred.base_url.rstrip("/")
    correl = uuid.uuid4().hex + uuid.uuid4().hex[:16]  # 48 hex chars = MQ's 24-byte correlation id
    headers = {**{k: v for k, v in auth_headers.items() if not k.startswith("X-Vault")},
               "ibm-mq-rest-csrf-token": "auto-connect", "Content-Type": "text/plain;charset=utf-8",
               "ibm-mq-md-correlationId": correl}
    if cred.transport_reply:
        rq_mgr, rq = cred.transport_reply.split("/", 1)
        headers["ibm-mq-md-replyTo"] = f"{rq}@{rq_mgr}"
    async with executor.http_client(app_id) as client:
        r = await client.post(f"{base}/ibmmq/rest/v2/messaging/qmgr/{qmgr}/queue/{queue}/message",
                              content=content.encode("utf-8"), headers=headers)
        if r.status_code not in (200, 201):
            return {"status_code": r.status_code, "data": {"error": f"MQ put failed: {r.text[:300]}"}}
        if not cred.transport_reply:
            return _accepted(request_id, "IBM_MQ", f"{queue}@{qmgr}")
        wait_ms = int(cred.reply_timeout_seconds * 1000)
        r = await client.delete(f"{base}/ibmmq/rest/v2/messaging/qmgr/{rq_mgr}/queue/{rq}/message",
                                params={"correlationId": correl, "wait": wait_ms},
                                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                                timeout=cred.reply_timeout_seconds + 10)
    if r.status_code == 204:
        return {"status_code": 504, "network_error": True, "error": f"No MQ reply on {rq} within {cred.reply_timeout_seconds}s"}
    if r.status_code != 200:
        return {"status_code": r.status_code, "data": {"error": f"MQ get failed: {r.text[:300]}"}}
    return _reply(r.text)


# ---------------------------------------------------------------------------- entry point
async def send(executor, cred, app_id: str, method: str, endpoint_path: str, params: Dict[str, Any],
               auth_headers: Dict[str, str], correlation: str) -> Dict[str, Any]:
    started = time.perf_counter()
    request_id = f"REQ{uuid.uuid4().hex[:20].upper()}"
    operation = endpoint_path.rstrip("/").split("/")[-1]
    content, ext = _envelope(cred, operation, method, endpoint_path, params or {}, request_id, correlation)
    name = f"{(correlation or 'NOREF').replace('/', '_')}_{operation}_{request_id}.{ext}"
    try:
        if cred.transport == "FILE":
            res = await _file(cred, name, content, request_id)
        elif cred.transport == "SFTP":
            res = await asyncio.to_thread(_sftp_put_get, cred, name, content, request_id)
        elif cred.transport == "IBM_MQ":
            res = await _mq(executor, cred, app_id, content, request_id, auth_headers)
        else:
            return {"status_code": 500, "data": {"error": f"Unknown transport {cred.transport}"}}
    except Exception as e:  # unreachable SFTP host, MQ down, permission denied ...
        res = {"status_code": 503, "network_error": True, "error": f"{cred.transport}: {type(e).__name__}: {e}"}
    res.setdefault("data", {"error": res.get("error")})
    res["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    res["target_endpoint"] = f"{cred.transport}:{cred.transport_target}"
    res["auth_type_used"] = cred.auth_type
    return res
