"""
Application Introspector for Banking Systems
Extracts operational schemas from OpenAPI, SAP OData, ISO 20022, and Legacy UI Screens.
"""

from typing import Dict, Any, List, Optional
import json
import logging

logger = logging.getLogger("auto_connect.introspector")


class ApplicationIntrospector:
    """
    Introspects raw bank systems across varied integration protocols:
    - OpenAPI 3.x / Swagger 2.0
    - SAP OData / BAPI Catalogs
    - ISO 20022 Message Definitions
    - Legacy Terminal / Desktop UI Screen maps
    """

    @classmethod
    async def introspect_live_endpoint(cls, spec_url: str) -> Dict[str, Any]:
        """Fetches live OpenAPI or OData catalog from an enterprise gateway URL."""
        import httpx  # lazy: only needed for live spec fetching

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(spec_url)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _resolve(spec: Dict[str, Any], node: Any, depth: int = 0) -> Any:
        """Resolves local JSON references ($ref: '#/components/schemas/X')."""
        if depth > 8 or not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target: Any = spec
            for part in ref[2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return ApplicationIntrospector._resolve(spec, target, depth + 1)
        return node

    @classmethod
    def _schema_properties(cls, spec: Dict[str, Any], schema: Any) -> Dict[str, Any]:
        schema = cls._resolve(spec, schema)
        if not isinstance(schema, dict):
            return {}
        props = dict(schema.get("properties", {}) or {})
        for sub in schema.get("allOf", []) or []:
            props.update(cls._schema_properties(spec, sub))
        return props

    @classmethod
    def parse_openapi_spec(cls, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes OpenAPI 3.0/3.1 or Swagger 2.0 into a unified structure."""
        title = spec.get("info", {}).get("title", "Unknown Bank Application")
        version = spec.get("info", {}).get("version", "1.0")
        description = spec.get("info", {}).get("description", "")

        endpoints = []
        paths = spec.get("paths", {}) or {}
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            shared_params = path_item.get("parameters", []) or []
            for method, op in path_item.items():
                if method.lower() not in ["get", "post", "put", "delete", "patch"] or not isinstance(op, dict):
                    continue

                summary = op.get("summary") or op.get("description") or f"{method.upper()} {path}"
                parameters: List[str] = []
                required: List[str] = []
                locations: Dict[str, str] = {}
                schema_props: Dict[str, Any] = {}

                for p in list(shared_params) + list(op.get("parameters", []) or []):
                    if isinstance(p, str):
                        parameters.append(p)
                        continue
                    p = cls._resolve(spec, p)
                    if not isinstance(p, dict):
                        continue
                    if p.get("in") == "body":  # Swagger 2.0 body parameter
                        body_schema = cls._resolve(spec, p.get("schema", {}))
                        props = cls._schema_properties(spec, body_schema)
                        for prop_name, prop in props.items():
                            if prop_name not in parameters:
                                parameters.append(prop_name)
                                locations[prop_name] = "body"
                                schema_props[prop_name] = cls._resolve(spec, prop)
                        required += [r for r in (body_schema.get("required", []) if isinstance(body_schema, dict) else [])]
                        continue
                    name = p.get("name", "param")
                    if p.get("in") == "header":
                        continue
                    if name not in parameters:
                        parameters.append(name)
                        locations[name] = p.get("in", "query")
                        schema_props[name] = cls._resolve(spec, p.get("schema", {"type": p.get("type", "string")}))
                    if p.get("required") or p.get("in") == "path":
                        required.append(name)

                # OpenAPI 3 requestBody
                rb = cls._resolve(spec, op.get("requestBody", {}) or {})
                content = (rb.get("content", {}) or {}) if isinstance(rb, dict) else {}
                media = content.get("application/json") or next(iter(content.values()), {}) if content else {}
                body_schema = cls._resolve(spec, (media or {}).get("schema", {}))
                for prop_name, prop in cls._schema_properties(spec, body_schema).items():
                    if prop_name not in parameters:
                        parameters.append(prop_name)
                        locations[prop_name] = "body"
                        schema_props[prop_name] = cls._resolve(spec, prop)
                if isinstance(body_schema, dict):
                    required += [r for r in body_schema.get("required", []) or [] if r not in required]

                endpoints.append({
                    "path": path,
                    "method": method.upper(),
                    "summary": summary,
                    "operation_id": op.get("operationId"),
                    "tags": op.get("tags", []),
                    "parameters": parameters,
                    "required": [r for r in dict.fromkeys(required)],
                    "locations": locations,
                    "schemas": schema_props,
                })

        return {
            "application_type": "REST_API",
            "title": title,
            "version": version,
            "description": description,
            "endpoints": endpoints,
            "raw_spec": spec,
        }

    @classmethod
    def parse_sap_odata_catalog(cls, odata_spec: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes SAP OData / NetWeaver catalog definitions."""
        service_name = odata_spec.get("service_name", "SAP S/4HANA OData Service")
        version = odata_spec.get("version", "2.0")
        actions = odata_spec.get("actions", []) or odata_spec.get("entity_sets", [])

        endpoints = []
        for action in actions:
            endpoints.append({
                "path": action.get("url_path", f"/sap/opu/odata/{action.get('name')}"),
                "method": action.get("http_method", "POST"),
                "summary": action.get("description", action.get("name")),
                "parameters": action.get("parameters", []),
            })

        return {
            "application_type": "SAP_ODATA",
            "title": service_name,
            "version": version,
            "description": f"SAP ERP Enterprise Service: {service_name}",
            "endpoints": endpoints,
            "raw_spec": odata_spec,
        }

    @classmethod
    def parse_legacy_ui_screen(cls, ui_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses a legacy terminal (3270 / Windows desktop) visual layout mapped by
        Gemini Vision / Computer Use.
        """
        app_title = ui_spec.get("window_title", "Legacy Mainframe Terminal")
        screens = ui_spec.get("screens", [])

        endpoints = []
        for screen in screens:
            screen_name = screen.get("screen_id", "MAIN_SCREEN")
            actions = screen.get("interactive_elements", [])
            for act in actions:
                endpoints.append({
                    "path": f"/ui/{screen_name}/{act.get('action_id')}",
                    "method": "UI_ACTION",
                    "summary": f"Operates UI Element '{act.get('label')}' on {screen_name}: {act.get('purpose')}",
                    "parameters": act.get("input_fields", []),
                })

        return {
            "application_type": "LEGACY_UI_SCREEN",
            "title": app_title,
            "version": ui_spec.get("version", "Legacy"),
            "description": f"Mainframe / GUI Desktop Screen Operator: {app_title}",
            "endpoints": endpoints,
            "raw_spec": ui_spec,
        }

    @classmethod
    def parse_terminal_screen_text(cls, screen_text: str) -> Dict[str, Any]:
        """
        Parses raw IBM 3270 / VT100 green-screen text dump.
        Extracts title, input fields [ ____ ], and action triggers (e.g. F3 to execute).
        """
        import re

        lines = [line.strip() for line in screen_text.strip().split("\n") if line.strip()]
        title = "IBM 3270 Mainframe Core Banking"
        input_fields = []
        action_key = "F3"
        action_label = "Execute Transaction"

        for line in lines:
            # Detect header / screen ID e.g. [CORE BANKING SYSTEM - SCREEN 104]
            title_match = re.search(r"\[([A-Z0-9\s\-]+)\]", line)
            if title_match and any(k in title_match.group(1).upper() for k in ["CORE", "SYSTEM", "SCREEN", "MAIN"]):
                title = title_match.group(1).strip()

            # Detect fields e.g. APPLICANT ACC: [ ____________ ]
            field_match = re.search(r"([A-Za-z0-9\s\(\)]+)\s*:\s*\[([^\]]*)\]", line)
            if field_match:
                raw_label = field_match.group(1).strip()
                field_name = raw_label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")
                if field_name and field_name not in input_fields:
                    input_fields.append(field_name)

            # Detect trigger keys e.g. PRESS [F3] TO EXECUTE
            key_match = re.search(r"(?:PRESS|HIT)\s*\[?([Ff][0-9]{1,2}|ENTER)\]?\s*(?:TO)?\s*([A-Za-z\s]+)", line, re.IGNORECASE)
            if key_match:
                action_key = key_match.group(1).upper()
                action_label = key_match.group(2).strip()

        if not input_fields:
            input_fields = ["account_number", "transaction_amount", "currency", "notes"]

        screen_slug = title.lower().replace(" ", "_").replace("-", "_")
        endpoint_path = f"/ui/{screen_slug}/{action_key.lower()}"

        return {
            "application_type": "LEGACY_UI_SCREEN",
            "title": title,
            "version": "IBM 3270 Mainframe",
            "description": f"Mainframe Terminal Screen: {title}",
            "endpoints": [
                {
                    "path": endpoint_path,
                    "method": "UI_ACTION",
                    "summary": f"Emulates keystroke [{action_key}] ({action_label}) after typing {len(input_fields)} screen fields",
                    "parameters": input_fields,
                    "trigger_key": action_key,
                }
            ],
            "raw_spec": {"screen_text": screen_text, "detected_fields": input_fields, "action_key": action_key},
        }

