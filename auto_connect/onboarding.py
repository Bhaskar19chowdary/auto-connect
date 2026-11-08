"""
Live onboarding of a new bank application from its URL.

1. Discovery  - finds the machine-readable API description (OpenAPI 3.x / Swagger 2.0, JSON or YAML)
                at the URL the operator entered, trying the locations API gateways normally publish it at.
2. Parsing    - normalises endpoints, parameters, required fields, $refs (see ApplicationIntrospector).
3. Probe      - proves connectivity and credentials with a READ-ONLY call (GET). Nothing that could
                change data in the target system is ever called during onboarding.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("auto_connect.onboarding")

SPEC_LOCATIONS = [
    "/openapi.json", "/openapi.yaml", "/openapi.yml", "/v3/api-docs", "/v2/api-docs", "/swagger.json",
    "/swagger.yaml", "/swagger/v1/swagger.json", "/api-docs", "/api/openapi.json", "/api/swagger.json",
    "/docs/openapi.json", "/.well-known/openapi.json", "/.well-known/openapi.yaml",
]

PROBE_HINTS = ("health", "status", "ping", "info", "version", "heartbeat", "alive", "ready")


class DiscoveryError(Exception):
    def __init__(self, message: str, tried: List[Dict[str, Any]]):
        super().__init__(message)
        self.tried = tried


def _parse_document(text: str) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(text)
    except Exception:
        try:
            import yaml

            doc = yaml.safe_load(text)
        except Exception:
            return None
    if isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) and isinstance(doc.get("paths"), dict):
        return doc
    return None


def candidate_urls(base_url: str, spec_url: Optional[str]) -> List[str]:
    base = base_url.rstrip("/")
    urls: List[str] = []
    if spec_url:
        urls.append(spec_url.strip())
    if base:
        urls.append(base)  # some gateways serve the spec at the root
        urls += [base + p for p in SPEC_LOCATIONS]
        # also try the host root when the base URL contains a path (e.g. https://host/api/v2)
        from urllib.parse import urlparse

        u = urlparse(base)
        if u.path not in ("", "/"):
            root = f"{u.scheme}://{u.netloc}"
            urls += [root + p for p in SPEC_LOCATIONS]
    return list(dict.fromkeys(urls))


async def discover_spec(executor, base_url: str, spec_url: Optional[str], headers: Dict[str, str],
                        app_id: Optional[str] = None) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    """Returns (spec, url_found_at, attempts). Raises DiscoveryError if nothing usable was found."""
    tried: List[Dict[str, Any]] = []
    auth_failures = 0
    async with executor.http_client(app_id) as client:
        for url in candidate_urls(base_url, spec_url):
            try:
                r = await client.get(url, headers={"Accept": "application/json, application/yaml;q=0.9, */*;q=0.5", **headers})
            except Exception as e:
                tried.append({"url": url, "result": f"unreachable ({type(e).__name__})"})
                continue
            if r.status_code in (401, 403):
                auth_failures += 1
                tried.append({"url": url, "result": f"HTTP {r.status_code} (credentials rejected)"})
                continue
            if r.status_code != 200:
                tried.append({"url": url, "result": f"HTTP {r.status_code}"})
                continue
            doc = _parse_document(r.text)
            if doc is None:
                tried.append({"url": url, "result": "HTTP 200 but not an OpenAPI/Swagger document"})
                continue
            tried.append({"url": url, "result": "OpenAPI document found"})
            return doc, url, tried
    if tried and all(t["result"].startswith("unreachable") for t in tried):
        raise DiscoveryError(f"Cannot reach {base_url or spec_url}: host unreachable", tried)
    if auth_failures:
        raise DiscoveryError("401 UNAUTHORIZED: the application rejected the credentials while reading its API description", tried)
    raise DiscoveryError(
        "No OpenAPI/Swagger description found at the URL. Enter the exact specification URL, or paste the "
        "specification, or list the endpoints manually.", tried)


def pick_probe_endpoint(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A GET endpoint that needs no input - preferably a health/status endpoint."""
    candidates = [ep for ep in parsed.get("endpoints", []) if ep["method"] == "GET" and not ep.get("required")
                  and "{" not in ep["path"]]
    for ep in candidates:
        if any(h in ep["path"].lower() for h in PROBE_HINTS):
            return ep
    return candidates[0] if candidates else None


async def probe(executor, app_id: str, parsed: Dict[str, Any], department: str, spec_found_at: Optional[str],
                headers: Dict[str, str]) -> Dict[str, Any]:
    """Read-only connectivity + authentication check."""
    ep = pick_probe_endpoint(parsed)
    if ep:
        res = await executor.execute_dynamic_call(app_id=app_id, endpoint_path=ep["path"], http_method="GET",
                                                  parameters={}, requester_department=department)
        code = res.get("status_code", 500)
        return {
            "endpoint": f"GET {ep['path']}",
            "status_code": code,
            "ok": 200 <= code < 300,
            "auth_rejected": code in (401, 403),
            "unreachable": bool(res.get("network_error")),
            "latency_ms": res.get("latency_ms"),
            "detail": res.get("error") or (res.get("data") if not (200 <= code < 300) else "reachable and authorised"),
            "auth_verified": True,
        }
    if spec_found_at:
        async with executor.http_client(app_id) as client:
            try:
                r = await client.get(spec_found_at, headers=headers)
                return {"endpoint": f"GET {spec_found_at}", "status_code": r.status_code, "ok": r.status_code == 200,
                        "auth_rejected": r.status_code in (401, 403), "unreachable": False, "latency_ms": None,
                        "detail": "API has no read-only endpoint; connectivity verified via its specification URL",
                        "auth_verified": False}
            except Exception as e:
                return {"endpoint": f"GET {spec_found_at}", "status_code": 503, "ok": False, "auth_rejected": False,
                        "unreachable": True, "latency_ms": None, "detail": str(e), "auth_verified": False}
    return {"endpoint": None, "status_code": None, "ok": True, "auth_rejected": False, "unreachable": False,
            "latency_ms": None, "detail": "No read-only endpoint available; no live call made (nothing is called "
                                          "that could change data)", "auth_verified": False}
