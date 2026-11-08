"""
Automatic integration of newly connected bank applications into the trade finance engine.

When an operator connects a new API, the Integration Manager:

1. Works out which engine role the application can fill (sanctions screening, FX market data, ...),
   either because it implements the engine's published contract exactly ("NATIVE") or because a
   built-in adapter understands its API shape ("ADAPTER": name-screening APIs, FX-rate APIs).
2. Runs live CONTRACT TESTS against the application (read-only calls only) - the same acceptance
   tests a bank's integration team runs before go-live. Only if they pass is the role switched over.
3. Monitors the live binding: if the application fails (timeouts, 5xx, unreadable answers) the call
   fails over to the incumbent system for read-only operations, operations are alerted, and after
   3 consecutive failures the binding is suspended (circuit breaker). The end-of-day batch re-tests
   suspended bindings and re-activates them automatically once they pass again.

Roles that WRITE (ledger postings, SWIFT release, limit bookings, client communication) are never
switched automatically: in a real bank they require certification against the vendor's test
environment first. Such applications are still connected as agents (A2A + operator console).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .. import settings
from .systems import BusError

logger = logging.getLogger("auto_connect.trade.integration")

BINDABLE_ROLES = {
    "tf_sanctions_screening": {"ops": ["screen_parties"], "read_only": {"screen_parties"}},
    "tf_market_data": {"ops": ["fx_rate", "convert"], "read_only": {"fx_rate", "convert"}},
    "tf_customer_master": {"ops": ["get_customer", "periodic_reviews_due"], "read_only": {"get_customer", "periodic_reviews_due"}},
}
# roles that post, pay or send: an application takes them over only after a recorded certification (four-eyes)
CERTIFIABLE_ROLES = ["tf_core_ledger", "tf_credit_limits", "tf_swift_gateway", "tf_email_gateway", "tf_document_archive",
                     "tf_client_portal", "tf_customer_master", "tf_sanctions_screening", "tf_market_data"]
DEPARTMENT_ROLE = {
    "COMPLIANCE_AML": "tf_sanctions_screening",
    "TREASURY_MARKETS": "tf_market_data",
    "CORE_BANKING": "tf_core_ledger",
    "PAYMENTS_SWIFT": "tf_swift_gateway",
    "CREDIT_RISK": "tf_credit_limits",
    "OFFICE_COMMUNICATION": "tf_email_gateway",
    "CLIENT_ENGAGEMENT": "tf_client_portal",
    "CUSTOMER_DATA": "tf_customer_master",
}
NAME_FIELDS = ["name", "entity_name", "full_name", "party_name", "subject_name", "search_name", "query", "q", "term"]
COUNTRY_FIELDS = ["country", "country_code", "nationality", "jurisdiction", "country_iso", "domicile"]
BASE_FIELDS = ["base", "from", "from_currency", "source", "base_currency", "currency", "ccy", "symbol", "sell"]
QUOTE_FIELDS = ["to", "quote", "target", "to_currency", "symbols", "quote_currency", "counter", "counter_currency", "buy"]
CIRCUIT_BREAKER_FAILURES = 3


class Binding(BaseModel):
    role: str
    agent_id: str
    application_name: str
    mode: str = "NONE"  # NATIVE | ADAPTER | NONE
    status: str = "NOT_BOUND"  # ACTIVE | SUSPENDED | NOT_BOUND
    reason: str = ""
    operations: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    settings: Dict[str, Any] = Field(default_factory=dict)
    contract_tests: List[Dict[str, Any]] = Field(default_factory=list)
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    failovers: int = 0
    last_error: Optional[str] = None
    bound_at: Optional[str] = None


def _get_path(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _set_path(target: Dict[str, Any], path: str, value: Any):
    parts = path.split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _snake(text: str) -> str:
    """camelCase / PascalCase / UPPER_CASE / kebab-case -> snake_case."""
    text = text or ""
    if not (text.isupper() or text.islower()):
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _find_field(params: List[str], candidates: List[str]) -> Optional[str]:
    lower = {p.lower(): p for p in params}
    for c in candidates:
        if c in lower:
            return lower[c]
    return None


def _first(d: Dict[str, Any], keys: List[str]):
    for k in keys:
        for dk, dv in d.items():
            if dk.lower() == k:
                return dv
    return None


def _unwrap(resp: Any) -> Any:
    if isinstance(resp, dict):
        for k in ("data", "result", "payload", "response"):
            if isinstance(resp.get(k), dict) and len(resp) <= 3:
                return resp[k]
    return resp


# ---------------------------------------------------------------------------
# response normalisation for adapters
# ---------------------------------------------------------------------------

_HIT = {"hit", "match", "matched", "blocked", "true_match", "positive", "confirmed", "deny", "denied", "reject", "rejected"}
_REVIEW = {"review", "potential", "potential_match", "possible", "possible_match", "pending", "manual_review", "escalate", "alert"}
_CLEAR = {"clear", "no_match", "nomatch", "none", "negative", "pass", "passed", "ok", "clean", "approved", "allow", "false_positive"}


def normalise_screening(resp: Any) -> Tuple[str, Optional[float], str]:
    r = _unwrap(resp)
    if not isinstance(r, dict):
        raise ValueError("screening response is not a JSON object")
    decision = _first(r, ["decision", "result", "status", "outcome", "match_status", "screening_result", "verdict"])
    if isinstance(decision, str):
        d = _snake(decision)
        if d in _HIT:
            label = "HIT"
        elif d in _REVIEW:
            label = "REVIEW"
        elif d in _CLEAR:
            label = "CLEAR"
        else:
            raise ValueError(f"unknown screening decision '{decision}'")
    else:
        flag = _first(r, ["is_match", "match", "hit", "matched", "is_hit", "sanctioned"])
        matches = _first(r, ["matches", "hits", "alerts", "results", "findings"])
        if isinstance(flag, bool):
            label = "HIT" if flag else "CLEAR"
        elif isinstance(matches, list):
            label = "REVIEW" if matches else "CLEAR"
        else:
            raise ValueError("screening response has no recognisable decision field")
    score = _first(r, ["score", "match_score", "confidence", "similarity"])
    try:
        score = float(score) if score is not None else None
        if score is not None and score > 1:
            score = score / 100
    except (TypeError, ValueError):
        score = None
    matches = _first(r, ["matches", "hits", "alerts", "results", "findings"])
    detail = ""
    if isinstance(matches, list) and matches:
        m0 = matches[0]
        if isinstance(m0, dict):
            lst = _first(m0, ["list_name", "list", "source", "program", "sanctions_list"])
            who = _first(m0, ["name", "entity_name", "matched_name", "match_name"])
            detail = (" - ".join(str(x) for x in (lst, who) if x) or str(m0))[:160]
        else:
            detail = str(m0)[:120]
    return label, score, detail


def parse_rate(resp: Any, quote: str) -> Decimal:
    r = _unwrap(resp)
    if isinstance(r, (int, float, str)):
        return Decimal(str(r))
    if not isinstance(r, dict):
        raise ValueError("rate response is not a JSON object")
    rates = _first(r, ["rates", "quotes", "conversion_rates"])
    if isinstance(rates, dict):
        for k, v in rates.items():
            if k.upper().endswith(quote.upper()):
                return Decimal(str(v))
    val = _first(r, ["rate", "mid", "value", "price", "eur_per_unit", "conversion_rate", "exchange_rate", "fx_rate", "result"])
    if isinstance(val, dict):
        return parse_rate(val, quote)
    if val is None:
        raise ValueError("rate response has no recognisable rate field")
    return Decimal(str(val))


# ---------------------------------------------------------------------------
class IntegrationManager:
    def __init__(self, engine):
        self.engine = engine
        self.bus = engine.bus
        self.executor = engine.bus.executor
        self.mesh = engine.mesh
        self.bindings: Dict[str, Binding] = {
            k: Binding(**v) for k, v in (getattr(engine.state, "bindings", None) or {}).items()}  # survive restarts
        self.bus.router = self

    def persist(self):
        """Bindings are part of the bank's configuration: saved with the books, restored at start."""
        self.engine.state.bindings = {k: b.model_dump(mode="json") for k, b in self.bindings.items()}
        if not getattr(self.engine.store, "in_transaction", False):
            self.engine.store.save(dirty_deals=set())

    # -- certification of applications for roles that post, pay or send ---------------
    def certify(self, agent_id: str, role: str, certified_by: str, evidence: str, endpoints: List[Dict[str, Any]],
                mapping: Optional[Dict[str, Dict[str, Any]]] = None, needs_second_approval: bool = False,
                application_name: str = "") -> Binding:
        if role not in CERTIFIABLE_ROLES:
            raise ValueError(f"Unknown role {role}; certifiable roles: {CERTIFIABLE_ROLES}")
        if not evidence or len(evidence.strip()) < 10:
            raise ValueError("Certification needs evidence (e.g. test run id / sign-off reference in the test environment)")
        role_ops = list(self.engine.systems[role].operations)
        ops: Dict[str, Dict[str, Any]] = {}
        for op in role_ops:
            if mapping and op in mapping:  # admin-described adapter: endpoint + field mapping
                m = mapping[op]
                if not m.get("path") or not m.get("method"):
                    raise ValueError(f"Mapping for {op} needs method and path")
                ops[op] = {"method": m["method"].upper(), "path": m["path"], "request": m.get("request") or {},
                           "request_template": m.get("request_template") or {}, "response": m.get("response") or {},
                           "constants": m.get("constants") or {}, "errors": m.get("errors") or {}}
                continue
            ep = next((e for e in endpoints if e["path"].rstrip("/").endswith("/" + op)
                       or _snake(e.get("operation_id") or "") == op), None)
            if ep:
                ops[op] = {"method": ep["method"], "path": ep["path"]}
        missing = [op for op in role_ops if op not in ops]
        if missing:
            raise ValueError(f"The application does not provide {missing} for {role}: implement the contract at "
                             f"/api/v1/tf/systems/{role}/openapi or describe a mapping for these operations")
        prior = self.bindings.get(agent_id)
        b = Binding(role=role, agent_id=agent_id,
                    application_name=application_name or (prior.application_name if prior else agent_id),
                    mode="MAPPED" if mapping else "NATIVE", operations=ops,
                    status="PENDING_APPROVAL" if needs_second_approval else "ACTIVE",
                    settings={"certified_by": certified_by, "evidence": evidence.strip(),
                              "certified_at": datetime.now(timezone.utc).isoformat()})
        if b.status == "ACTIVE":
            self._activate(b, f"Certified by {certified_by}: {evidence.strip()[:120]}")
        else:
            b.reason = f"Certified by {certified_by}; waiting for a second administrator's approval (four-eyes)"
        self.bindings[agent_id] = b
        self.persist()
        return b

    def approve_certification(self, agent_id: str, approved_by: str) -> Binding:
        b = self.bindings.get(agent_id)
        if not b or b.status != "PENDING_APPROVAL":
            raise ValueError("No certification waiting for approval")
        if approved_by == b.settings.get("certified_by"):
            raise ValueError("Four-eyes: the approval must come from a different administrator")
        b.settings["approved_by"] = approved_by
        self._activate(b, f"Certified by {b.settings['certified_by']}, approved by {approved_by}")
        self.persist()
        return b

    def _activate(self, b: Binding, reason: str):
        previous = self.active_for(b.role)
        if previous and previous.agent_id != b.agent_id:
            previous.status, previous.reason = "NOT_BOUND", f"Replaced by {b.application_name}"
        b.status, b.reason = "ACTIVE", f"{reason} - now serving {b.role} ({b.mode})"
        b.bound_at = datetime.now(timezone.utc).isoformat()

    # -- routing -------------------------------------------------------
    def active_for(self, role: str) -> Optional[Binding]:
        for b in self.bindings.values():
            if b.role == role and b.status == "ACTIVE":
                return b
        return None

    def summary(self) -> List[Dict[str, Any]]:
        return [b.model_dump() for b in self.bindings.values()]

    def unbind(self, agent_id: str) -> bool:
        removed = self.bindings.pop(agent_id, None) is not None
        if removed:
            self.persist()
        return removed

    async def _vendor(self, b: Binding, sender: str, method: str, path: str, params: Dict[str, Any], intent: str):
        res = await self.executor.execute_dynamic_call(app_id=b.agent_id, endpoint_path=path, http_method=method,
                                                       parameters=params, requester_department=self.bus._department(sender))
        data = res.get("data") if "data" in res else {"error": res.get("error")}
        self.mesh.send_a2a_message(sender, b.agent_id, intent, params, data if isinstance(data, dict) else {"value": data})
        code = res.get("status_code", 500)
        if code >= 400:
            raise BusError(code, "NETWORK" if res.get("network_error") else "VENDOR_ERROR",
                           f"{b.application_name} returned HTTP {code}: {str(data)[:200]}", b.agent_id,
                           data if isinstance(data, dict) else {"body": data})
        return data

    async def call_bound(self, b: Binding, sender: str, operation: str, params: Dict[str, Any], intent: Optional[str]):
        b.calls += 1
        try:
            if b.mode == "NATIVE":
                op = b.operations[operation]
                data = await self._vendor(b, sender, op["method"], op["path"], params, intent or operation.upper())
                if not isinstance(data, dict):
                    raise BusError(502, "UNPARSEABLE_RESPONSE", "Response is not a JSON object", b.agent_id)
            elif b.mode == "MAPPED":
                data = await self._call_mapped(b, sender, operation, params, intent)
            elif b.role == "tf_sanctions_screening":
                data = await self._adapter_screen(b, sender, **params)
            elif b.role == "tf_market_data":
                data = await self._adapter_fx(b, sender, operation, params)
            else:
                raise BusError(500, "NO_ADAPTER", "No adapter for role", b.agent_id)
        except BusError as e:
            self._record_failure(b, e.detail)
            raise
        except (ValueError, KeyError, TypeError, ArithmeticError) as e:
            self._record_failure(b, f"Unreadable response: {e}")
            raise BusError(502, "UNPARSEABLE_RESPONSE", str(e), b.agent_id)
        b.consecutive_failures = 0
        return data

    def _record_failure(self, b: Binding, error: str):
        b.failures += 1
        b.consecutive_failures += 1
        b.last_error = error[:300]
        if b.consecutive_failures >= CIRCUIT_BREAKER_FAILURES and b.status == "ACTIVE":
            b.status = "SUSPENDED"
            b.reason = f"Circuit breaker opened after {b.consecutive_failures} consecutive failures: {b.last_error}"

    async def on_failover(self, b: Binding, sender: str, role: str, operation: str, error: BusError):
        b.failovers += 1
        self.mesh.send_a2a_message(sender, role, f"FAILOVER_FROM_{b.agent_id}".upper(), {"operation": operation},
                                   {"reason": error.detail})
        try:
            await self.bus.call(self.engine.agent_id, "tf_email_gateway", "dispatch_message", {
                "to": [settings.bank_sender_email()],
                "subject": f"[OPS] {b.application_name} failed - {role} served by fallback system",
                "body": f"Operation '{operation}' on {b.application_name} failed: {error.detail}\n"
                        f"The request was served by the incumbent system. Binding status: {b.status} "
                        f"({b.consecutive_failures} consecutive failures).",
                "file_stem": f"OPS_FAILOVER_{b.agent_id}_{b.failovers:03d}"}, "ALERT_OPERATIONS", _no_route=True)
        except BusError:
            pass

    # -- binding -------------------------------------------------------
    async def try_bind(self, card, parsed: Dict[str, Any]) -> Binding:
        endpoints = parsed.get("endpoints", [])
        role_hint = DEPARTMENT_ROLE.get(card.banking_department)
        b = Binding(role=role_hint or "", agent_id=card.agent_id, application_name=card.application_name)

        # 1. exact implementation of the engine's published contract?
        for role, spec in BINDABLE_ROLES.items():
            ops = {}
            for op in spec["ops"]:
                ep = next((e for e in endpoints if e["path"].rstrip("/").endswith("/" + op)
                           or _snake(e.get("operation_id") or "") == op), None)
                if ep:
                    ops[op] = {"method": ep["method"], "path": ep["path"]}
            if len(ops) == len(spec["ops"]):
                b.role, b.mode, b.operations = role, "NATIVE", ops
                break

        # 2. otherwise a built-in adapter for the detected role
        if b.mode == "NONE" and role_hint in BINDABLE_ROLES:
            b.role = role_hint
            if role_hint == "tf_sanctions_screening":
                self._match_screening(b, endpoints)
            else:
                self._match_fx(b, endpoints)

        if b.mode == "NONE":
            if role_hint and role_hint not in BINDABLE_ROLES:
                b.reason = (f"Detected as a {card.banking_department} system. Roles that post, pay or send "
                            f"({role_hint}) are not switched automatically: certify it against its test environment "
                            f"first. It is connected as an agent and usable via its console and the A2A mesh.")
            elif role_hint in BINDABLE_ROLES:
                b.reason = (f"Detected as {role_hint} but its endpoints could not be mapped (no recognisable "
                            f"name/country or currency fields). Implement the contract at "
                            f"/api/v1/tf/systems/{role_hint}/openapi to plug it in.")
            else:
                b.reason = "No trade-engine role matches this application; connected as a standalone agent."
            self.bindings[b.agent_id] = b
            return b

        passed = await self._contract_tests(b)
        if passed:
            previous = self.active_for(b.role)
            if previous and previous.agent_id != b.agent_id:
                previous.status = "NOT_BOUND"
                previous.reason = f"Replaced by {b.application_name}"
            b.status = "ACTIVE"
            b.bound_at = datetime.now(timezone.utc).isoformat()
            b.reason = f"Contract tests passed - now serving {b.role} for the trade engine ({b.mode})"
        else:
            b.status = "NOT_BOUND"
            failed = [t for t in b.contract_tests if not t["passed"]]
            b.reason = "Contract tests failed: " + "; ".join(f"{t['test']}: {t['detail']}" for t in failed)
        self.bindings[b.agent_id] = b
        self.persist()
        return b

    async def health_check(self) -> List[str]:
        """Re-tests suspended bindings (called by the end-of-day batch)."""
        out = []
        for b in self.bindings.values():
            if b.status == "SUSPENDED":
                if await self._contract_tests(b):
                    b.status, b.consecutive_failures = "ACTIVE", 0
                    b.reason = "Re-activated after passing contract tests"
                    out.append(f"{b.application_name}: re-activated")
                else:
                    out.append(f"{b.application_name}: still suspended")
        return out

    def _match_screening(self, b: Binding, endpoints):
        best = None
        for ep in endpoints:
            text = f"{ep['path']} {ep.get('operation_id') or ''} {ep.get('summary') or ''}".lower()
            if not any(k in text for k in ("screen", "check", "search", "match", "lookup", "sanction", "watchlist")):
                continue
            name_f = _find_field(ep["parameters"], NAME_FIELDS)
            if not name_f or "{" in ep["path"]:
                continue
            score = (2 if ep["method"] == "POST" else 1) + (1 if "screen" in text else 0)
            if not best or score > best[0]:
                best = (score, ep, name_f, _find_field(ep["parameters"], COUNTRY_FIELDS))
        if best:
            _, ep, name_f, country_f = best
            b.mode = "ADAPTER"
            b.operations = {"screen_parties": {"method": ep["method"], "path": ep["path"]}}
            b.settings = {"name_field": name_f, "country_field": country_f}

    def _match_fx(self, b: Binding, endpoints):
        best = None
        for ep in endpoints:
            text = f"{ep['path']} {ep.get('operation_id') or ''} {ep.get('summary') or ''}".lower()
            if not any(k in text for k in ("rate", "fx", "exchange", "currenc", "quote", "convert")):
                continue
            base_f = _find_field(ep["parameters"], BASE_FIELDS)
            if not base_f and not re.search(r"{[^}]*(currency|ccy|base|from|symbol)[^}]*}", ep["path"], re.I):
                continue
            score = (2 if ep["method"] == "GET" else 1) + (1 if "rate" in text else 0)
            if not best or score > best[0]:
                path_param = re.search(r"{([^}]+)}", ep["path"])
                best = (score, ep, base_f or (path_param.group(1) if path_param else None),
                        _find_field(ep["parameters"], QUOTE_FIELDS))
        if best:
            _, ep, base_f, quote_f = best
            b.mode = "ADAPTER"
            b.operations = {"fx_rate": {"method": ep["method"], "path": ep["path"]}}
            b.settings = {"base_field": base_f, "quote_field": quote_f, "invert": False}

    # -- adapters ------------------------------------------------------
    async def _call_mapped(self, b: Binding, sender: str, operation: str, params: Dict[str, Any], intent: Optional[str]):
        """Admin-described adapter (see trade/mapping.py): builds the application's request from the engine's
        parameters, translates its answer back and maps its error codes to the engine's."""
        from . import mapping

        m = b.operations[operation]
        if m.get("request_template"):
            body = mapping.build(m["request_template"], params)
        elif m.get("request"):
            body = {}
            for ours, theirs in m["request"].items():
                if ours in params:
                    _set_path(body, theirs, params[ours])
        else:
            body = dict(params)
        for k, v in (m.get("constants") or {}).items():
            _set_path(body, k, v)
        try:
            data = await self._vendor(b, sender, m["method"], m["path"], body, intent or operation.upper())
        except BusError as e:
            code = mapping.translate_error(m.get("errors") or {}, e.data, e.status_code) if m.get("errors") else ""
            if code:
                raise BusError(e.status_code, code, e.detail, b.agent_id, e.data)
            raise
        if m.get("response"):
            out = {}
            for ours, spec in m["response"].items():
                value = mapping.apply(spec, data)
                if value is None and isinstance(spec, str):
                    raise ValueError(f"mapped response lacks {ours} ({spec})")
                if value is not None:
                    out[ours] = value
            return out
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        return data

    async def _adapter_screen(self, b: Binding, sender: str, deal_ref: str, parties: List[Dict[str, Any]],
                              places=None, goods: str = "", vessel_imo=None, export_license_ref=None):
        # Name screening by the vendor; embargo country/region, vessel and dual-use checks stay with the
        # bank's own rules engine (as in real banks, where vendors screen names only).
        internal = await self.bus.call(sender, "tf_sanctions_screening", "screen_parties", {
            "deal_ref": deal_ref, "parties": [{**p, "name": ""} for p in parties], "places": places or [], "goods": goods,
            "vessel_imo": vessel_imo, "export_license_ref": export_license_ref}, "INTERNAL_EMBARGO_CHECKS", _no_route=True)
        alerts = list(internal.get("alerts", []))
        op = b.operations["screen_parties"]
        for p in parties:
            if not p.get("name"):
                continue
            payload = {b.settings["name_field"]: p["name"]}
            if b.settings.get("country_field") and p.get("country"):
                payload[b.settings["country_field"]] = p["country"]
            resp = await self._vendor(b, sender, op["method"], op["path"], payload, "SCREEN_NAME")
            label, score, detail = normalise_screening(resp)
            if label != "CLEAR":
                alerts.append({"type": "NAME", "subject": f"{p.get('role', 'party')}: {p['name']}", "decision": label,
                               "reason": f"{b.application_name}: {label}" + (f" ({detail})" if detail else "")
                               + (f", score {score:.2f}" if score is not None else ""), "score": score,
                               "list_entry": detail or None})
        decisions = {a["decision"] for a in alerts}
        result = ("BLOCKED" if "HIT" in decisions else "REVIEW_REQUIRED" if "REVIEW" in decisions
                  else "CLEAR_WITH_DISCOUNTED_ALERTS" if alerts else "CLEAR")
        case = {"case_id": f"VND-{secrets.token_hex(4).upper()}", "deal_ref": deal_ref, "result": result, "alerts": alerts,
                "screened_on": self.engine.today.isoformat(), "parties_screened": len(parties),
                "lists": [f"{b.application_name} (vendor)", "Bank embargo rules"]}
        self.engine.state.sanctions_cases.append(case)
        return case

    async def _rate(self, b: Binding, sender: str, currency: str) -> Decimal:
        """EUR value of one unit of `currency` from the vendor."""
        if currency == "EUR":
            return Decimal("1")
        op = b.operations["fx_rate"]
        s = b.settings
        params = {s["base_field"]: currency}
        if s.get("quote_field"):
            params[s["quote_field"]] = "EUR"
        resp = await self._vendor(b, sender, op["method"], op["path"], params, "FX_RATE")
        rate = parse_rate(resp, "EUR")
        if rate <= 0:
            raise ValueError("non-positive rate")
        return (Decimal("1") / rate) if s.get("invert") else rate

    async def _adapter_fx(self, b: Binding, sender: str, operation: str, params: Dict[str, Any]):
        if operation == "fx_rate":
            return {"currency": params["currency"], "eur_per_unit": await self._rate(b, sender, params["currency"]),
                    "source": b.application_name}
        if operation == "convert":
            src = await self._rate(b, sender, params["from_currency"])
            dst = await self._rate(b, sender, params["to_currency"])
            amount = Decimal(str(params["amount"])) * src / dst
            return {"amount": amount.quantize(Decimal("0.01")), "currency": params["to_currency"],
                    "rate": (src / dst).quantize(Decimal("0.000001")), "source": b.application_name}
        raise KeyError(operation)

    # -- contract tests --------------------------------------------------
    async def _contract_tests(self, b: Binding) -> bool:
        tests: List[Dict[str, Any]] = []
        sender = self.engine.agent_id

        async def run(name, fn):
            try:
                ok, detail = await fn()
            except BusError as e:
                ok, detail = False, e.detail
            except Exception as e:  # unreadable response etc.
                ok, detail = False, f"{type(e).__name__}: {e}"
            tests.append({"test": name, "passed": ok, "detail": str(detail)[:200]})

        if b.role == "tf_sanctions_screening":
            clean = {"deal_ref": "CONTRACT-TEST", "parties": [{"name": "Hanse Solartechnik GmbH", "country": "DE",
                                                               "role": "test"}], "places": [], "goods": ""}

            async def t_clean():
                if b.mode == "NATIVE":
                    r = await self._vendor(b, sender, b.operations["screen_parties"]["method"],
                                           b.operations["screen_parties"]["path"], clean, "CONTRACT_TEST")
                    ok = r.get("result") in ("CLEAR", "CLEAR_WITH_DISCOUNTED_ALERTS", "REVIEW_REQUIRED", "BLOCKED") \
                        and isinstance(r.get("alerts"), list)
                    return ok, f"result={r.get('result')}"
                r = await self._adapter_screen(b, sender, **clean)
                return r["result"] != "BLOCKED", f"known clean party -> {r['result']}"
            await run("Known clean party is not blocked", t_clean)

            hit_name = os.environ.get("TF_SANCTIONS_TEST_HIT_NAME", "").strip()
            if hit_name:
                async def t_hit():
                    params = {"deal_ref": "CONTRACT-TEST", "parties": [{"name": hit_name, "country": os.environ.get(
                        "TF_SANCTIONS_TEST_HIT_COUNTRY", ""), "role": "test"}], "places": [], "goods": ""}
                    if b.mode == "NATIVE":
                        r = await self._vendor(b, sender, b.operations["screen_parties"]["method"],
                                               b.operations["screen_parties"]["path"], params, "CONTRACT_TEST")
                    else:
                        r = await self._adapter_screen(b, sender, **params)
                    return r["result"] in ("BLOCKED", "REVIEW_REQUIRED"), f"vendor test record -> {r['result']}"
                await run("Vendor test record is detected", t_hit)
        elif b.role == "tf_customer_master":
            known = os.environ.get("TF_CONTRACT_TEST_CUSTOMER", "").strip() or next(iter(self.engine.state.customers), "")

            async def t_customer():
                r = await self.call_bound(b, sender, "get_customer", {"customer_id": known}, "CONTRACT_TEST")
                return isinstance(r.get("kyc_ok"), bool) and isinstance(r.get("customer"), dict), \
                    f"get_customer({known}) -> kyc_ok={r.get('kyc_ok')}"
            await run("Known customer returns KYC verdict", t_customer)
        elif b.role == "tf_market_data":
            ref = self.engine.systems["tf_market_data"].rates()

            async def t_fx():
                checks = []
                for ccy in ("USD", "GBP"):
                    if b.mode == "NATIVE":
                        op = b.operations["fx_rate"]
                        r = await self._vendor(b, sender, op["method"], op["path"], {"currency": ccy}, "CONTRACT_TEST")
                        val = Decimal(str(r["eur_per_unit"]))
                    else:
                        b.settings["invert"] = False
                        val = await self._rate(b, sender, ccy)
                    checks.append((ccy, val, ref[ccy]))
                # rate quotation direction: accept the vendor rate if within 25% of the bank's reference,
                # or its inverse (vendor quotes the other way round)
                direct = all(abs(v - r) / r <= Decimal("0.25") for _, v, r in checks)
                inverse = all(abs(1 / v - r) / r <= Decimal("0.25") for _, v, r in checks if v)
                if direct:
                    return True, "rates plausible vs reference: " + ", ".join(f"{c} {v}" for c, v, _ in checks)
                if inverse and b.mode == "ADAPTER":
                    b.settings["invert"] = True
                    return True, "vendor quotes per EUR; inverted: " + ", ".join(f"{c} {1 / v:.6f}" for c, v, _ in checks)
                return False, "rates implausible vs reference: " + ", ".join(f"{c} {v} (ref {r})" for c, v, r in checks)
            await run("FX rates plausible (USD, GBP)", t_fx)

        b.contract_tests = tests
        return bool(tests) and all(t["passed"] for t in tests)
