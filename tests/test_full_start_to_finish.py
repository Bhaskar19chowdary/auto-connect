"""
==========================================================================
 FULL START-TO-FINISH INTEGRATION TEST
 Simulates the complete Auto-Connect lifecycle as a real bank operator:

 Phase 1: Empty system – no agents, no credentials, nothing.
 Phase 2: Operator feeds 7 raw API specs + 1 mainframe screen one by one.
          The Brain auto-analyzes each, creates Agent Cards, Vault entries,
          Mock handlers, MCP tools, and links everything into the mesh.
 Phase 3: Verify the mesh is fully wired – every agent discoverable.
 Phase 4: Operator submits a real-world business problem.
          The Brain orchestrates autonomous cross-department resolution.
 Phase 5: Verify every step, every A2A message, every tool call.
 Phase 6: Test the Dashboard REST API end-to-end (connect + orchestrate).
 Phase 7: Dynamic hot-connect – add a BRAND NEW custom API mid-flight,
          verify the Brain assimilates it and it joins the next orchestration.
==========================================================================
"""

import sys
import os
import json
import asyncio
from datetime import datetime, timezone

# Ensure imports work from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console(width=100)

# ── Phase tracking ──
phase_results = []

def phase_pass(phase: str, detail: str):
    phase_results.append(("PASS", phase, detail))
    console.print(f"  [bold green]✓ PASS[/bold green]  {detail}")

def phase_fail(phase: str, detail: str):
    phase_results.append(("FAIL", phase, detail))
    console.print(f"  [bold red]✗ FAIL[/bold red]  {detail}")


async def run_full_start_to_finish():
    from auto_connect.vault import ZeroTrustVault
    from auto_connect.mesh import A2AEcosystemMesh
    from auto_connect.executor import UniversalAPIExecutor
    from auto_connect.brain import AutonomousEcosystemBrain
    from auto_connect.models import VaultCredential
    from auto_connect.mock_apps import (
        DOKA_SPEC, SAP_CREDIT_SPEC, PORTAL_SPEC, MOODYS_SPEC,
        REFINITIV_SPEC, OUTLOOK_TEAMS_SPEC, OFFICE_EXCEL_PPT_SPEC,
        MAINFRAME_3270_SCREEN,
        mock_doka_handler, mock_sap_handler, mock_portal_handler,
        mock_moodys_handler, mock_refinitiv_handler,
        mock_outlook_teams_handler, mock_office_handler,
    )

    console.print(Panel(
        "[bold cyan]🏦 FULL START-TO-FINISH INTEGRATION TEST[/bold cyan]\n\n"
        "Simulates the complete operator experience:\n"
        "  1. Start from scratch (empty system)\n"
        "  2. Feed 8 raw API specs → Brain auto-creates everything\n"
        "  3. Verify mesh wiring & MCP tool registry\n"
        "  4. Submit real-world banking problem → autonomous resolution\n"
        "  5. Test REST API dashboard endpoints\n"
        "  6. Hot-connect a brand new custom API mid-flight\n"
        "  7. Final scorecard",
        title="Auto-Connect: Complete Lifecycle Test",
        border_style="cyan",
        box=box.DOUBLE,
    ))

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: START FROM SCRATCH
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 1: INITIALIZE EMPTY SYSTEM ═══[/bold yellow]")

    vault = ZeroTrustVault()
    mesh = A2AEcosystemMesh()
    executor = UniversalAPIExecutor(vault)
    brain = AutonomousEcosystemBrain(vault=vault, mesh=mesh, executor=executor)

    assert len(mesh.list_agents()) == 0, "Mesh should start empty"
    assert len(vault.list_credentials()) == 0, "Vault should start empty"
    phase_pass("PHASE1", "System initialized: 0 agents, 0 credentials, 0 tools")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: FEED RAW API SPECS → BRAIN AUTO-CREATES EVERYTHING
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 2: FEED 8 RAW API SPECS (Simulating Operator) ═══[/bold yellow]")

    # Define the complete bank application suite
    apps_to_onboard = [
        {
            "spec": DOKA_SPEC,
            "agent_id": "agent_trade_finance_doka",
            "handler": mock_doka_handler,
            "cred": VaultCredential(
                app_id="agent_trade_finance_doka",
                base_url="https://doka.internal.bank.example",
                auth_type="mTLS",
                client_cert="sha256_doka_prod_gateway_cert_9901",
                allowed_departments=["TRADE_FINANCE", "RISK_AUDIT"],
            ),
            "expected_dept": "TRADE_FINANCE",
            "expected_tools_min": 2,
        },
        {
            "spec": SAP_CREDIT_SPEC,
            "agent_id": "agent_credit_collateral_sap",
            "handler": mock_sap_handler,
            "cred": VaultCredential(
                app_id="agent_credit_collateral_sap",
                base_url="https://sap-s4.internal.bank.example",
                auth_type="Bearer",
                token="demo_vault_s4_sec_tok_9918491823719",
                allowed_departments=["TRADE_FINANCE", "CREDIT_RISK"],
            ),
            "expected_dept": "CREDIT_RISK",
            "expected_tools_min": 1,
        },
        {
            "spec": PORTAL_SPEC,
            "agent_id": "agent_customer_trade_portal",
            "handler": mock_portal_handler,
            "cred": VaultCredential(
                app_id="agent_customer_trade_portal",
                base_url="https://portal-api.internal.bank.example",
                auth_type="API_KEY",
                token="demo_api_portal_key_381290381023",
                allowed_departments=["TRADE_FINANCE", "CLIENT_ENGAGEMENT"],
            ),
            "expected_dept": "CLIENT_ENGAGEMENT",
            "expected_tools_min": 1,
        },
        {
            "spec": MOODYS_SPEC,
            "agent_id": "agent_moodys_risk_analytics",
            "handler": mock_moodys_handler,
            "cred": VaultCredential(
                app_id="agent_moodys_risk_analytics",
                base_url="https://api.moodys.com/v2",
                auth_type="API_KEY",
                token="moodys_api_key_prod_9918",
                allowed_departments=["CREDIT_RISK", "TRADE_FINANCE"],
            ),
            "expected_dept": "CREDIT_RISK",
            "expected_tools_min": 1,
        },
        {
            "spec": REFINITIV_SPEC,
            "agent_id": "agent_refinitiv_sanctions",
            "handler": mock_refinitiv_handler,
            "cred": VaultCredential(
                app_id="agent_refinitiv_sanctions",
                base_url="https://api.refinitiv.com/world-check/v1",
                auth_type="Bearer",
                token="refinitiv_oauth_tok_live_8812",
                allowed_departments=["TRADE_FINANCE", "COMPLIANCE_AML"],
            ),
            "expected_dept": "COMPLIANCE_AML",
            "expected_tools_min": 1,
        },
        {
            "spec": OUTLOOK_TEAMS_SPEC,
            "agent_id": "agent_microsoft_outlook",
            "handler": mock_outlook_teams_handler,
            "cred": VaultCredential(
                app_id="agent_microsoft_outlook",
                base_url="https://graph.microsoft.com/v1.0",
                auth_type="Bearer",
                token="ms_graph_tok_live_7719",
                allowed_departments=["*"],
            ),
            "expected_dept": "OFFICE_COMMUNICATION",
            "expected_tools_min": 2,
        },
        {
            "spec": OFFICE_EXCEL_PPT_SPEC,
            "agent_id": "agent_office_excel_ppt",
            "handler": mock_office_handler,
            "cred": VaultCredential(
                app_id="agent_office_excel_ppt",
                base_url="https://office-engine.internal.bank.example",
                auth_type="Bearer",
                token="office_sec_tok_5521",
                allowed_departments=["*"],
            ),
            "expected_dept": "FINANCIAL_REPORTING",
            "expected_tools_min": 2,
        },
    ]

    onboard_table = Table(title="Auto-Discovery & Agent Spawning", box=box.ROUNDED, show_lines=True)
    onboard_table.add_column("#", style="dim", width=3)
    onboard_table.add_column("Application", style="cyan", width=38)
    onboard_table.add_column("Agent ID", style="magenta", width=30)
    onboard_table.add_column("Department", style="yellow", width=22)
    onboard_table.add_column("Tools", style="green", width=5)
    onboard_table.add_column("Status", style="bold green", width=8)

    for i, app in enumerate(apps_to_onboard, start=1):
        # Step A: IT Admin provisions credentials in Vault
        vault.store_credential(app["cred"])

        # Step B: IT Admin registers mock handler for test execution
        executor.register_in_process_handler(app["agent_id"], app["handler"])

        # Step C: THE BRAIN AUTO-ANALYZES THE RAW SPEC
        # This is the key moment – the Brain reads the raw OpenAPI spec,
        # determines the banking domain, extracts all tools, and creates
        # a complete A2A Agent Card with issue-resolution capabilities.
        card = brain.analyze_and_assimilate_application(app["spec"], app["agent_id"])

        # Verify: correct agent_id assigned
        assert card.agent_id == app["agent_id"], \
            f"Expected agent_id '{app['agent_id']}', got '{card.agent_id}'"

        # Verify: correct banking department classification
        assert card.banking_department == app["expected_dept"], \
            f"Expected dept '{app['expected_dept']}' for {card.application_name}, got '{card.banking_department}'"

        # Verify: minimum number of tools discovered
        assert len(card.discovered_tools) >= app["expected_tools_min"], \
            f"Expected >= {app['expected_tools_min']} tools for {card.application_name}, got {len(card.discovered_tools)}"

        # Verify: agent is now in the mesh
        assert mesh.get_agent(app["agent_id"]) is not None, \
            f"Agent {app['agent_id']} not found in mesh after assimilation"

        onboard_table.add_row(
            str(i),
            card.application_name,
            card.agent_id,
            card.banking_department,
            str(len(card.discovered_tools)),
            "✓ OK",
        )

    # Step D: Assimilate legacy IBM 3270 Mainframe (zero-API)
    mainframe_card = brain.assimilate_legacy_screen(MAINFRAME_3270_SCREEN, "agent_mainframe_core_104")
    assert mainframe_card.banking_department == "CORE_BANKING"
    assert mainframe_card.discovered_tools[0].http_method == "UI_ACTION"

    onboard_table.add_row(
        "8",
        mainframe_card.application_name,
        mainframe_card.agent_id,
        "CORE_BANKING",
        str(len(mainframe_card.discovered_tools)),
        "✓ OK",
    )

    console.print(onboard_table)

    total_agents = len(mesh.list_agents())
    total_tools = len(mesh.export_mcp_tool_definitions())
    total_creds = len(vault.list_credentials())

    assert total_agents == 8, f"Expected 8 agents, got {total_agents}"
    assert total_tools >= 11, f"Expected >= 11 MCP tools, got {total_tools}"
    assert total_creds >= 8, f"Expected >= 8 credentials, got {total_creds}"

    phase_pass("PHASE2", f"8 apps auto-discovered: {total_agents} agents, {total_tools} MCP tools, {total_creds} vault entries")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 3: VERIFY MESH WIRING – EVERY AGENT DISCOVERABLE
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 3: VERIFY MESH WIRING & ISSUE ROUTING ═══[/bold yellow]")

    issue_routing_checks = [
        ("UCP600_DISCREPANCY_CHECK",  "agent_trade_finance_doka"),
        ("CREDIT_LIMIT_SHORTFALL",    "agent_credit_collateral_sap"),
        ("MISSING_TRADE_DOCUMENT",    "agent_customer_trade_portal"),
        ("SANCTIONS_CHECK_HOLD",      "agent_refinitiv_sanctions"),
        ("UNRATED_COUNTERPARTY",      "agent_moodys_risk_analytics"),
        ("EXECUTIVE_SLIDE_DECK",      "agent_office_excel_ppt"),
        ("EMAIL_SUMMARY_DISPATCH",    "agent_microsoft_outlook"),
        ("MAINFRAME_ACCOUNT_POSTING", "agent_mainframe_core_104"),
    ]

    routing_pass = True
    for issue_tag, expected_agent_id in issue_routing_checks:
        matches = mesh.find_agents_for_issue(issue_tag)
        if not matches:
            phase_fail("PHASE3", f"Issue '{issue_tag}' found NO resolving agent")
            routing_pass = False
            continue
        matched_agent_id = matches[0][0].agent_id
        if matched_agent_id != expected_agent_id:
            phase_fail("PHASE3", f"Issue '{issue_tag}' routed to '{matched_agent_id}', expected '{expected_agent_id}'")
            routing_pass = False
        else:
            console.print(f"  [green]✓[/green] {issue_tag:38s} → [cyan]{matched_agent_id}[/cyan]")

    if routing_pass:
        phase_pass("PHASE3", "All 8 issue types correctly route to the right specialist agent")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 4: EXECUTE INDIVIDUAL TOOL CALLS VIA UNIVERSAL EXECUTOR
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 4: TEST INDIVIDUAL DYNAMIC TOOL CALLS ═══[/bold yellow]")

    # Test DOKA UCP 600 examination
    doka_res = await executor.execute_dynamic_call(
        app_id="agent_trade_finance_doka",
        endpoint_path="/api/v1/lc/examine-ucp600",
        http_method="POST",
        parameters={"dossier_id": "TEST-001", "documents_json": "{}"},
        requester_department="TRADE_FINANCE",
    )
    assert doka_res["status_code"] == 200, f"DOKA call failed: {doka_res}"
    assert "UCP 600" in str(doka_res["data"])
    phase_pass("PHASE4", f"DOKA UCP 600 exam: HTTP {doka_res['status_code']} ({doka_res['latency_ms']:.2f}ms)")

    # Test Moody's credit rating
    moodys_res = await executor.execute_dynamic_call(
        app_id="agent_moodys_risk_analytics",
        endpoint_path="/api/v2/ratings/counterparty",
        http_method="POST",
        parameters={"customer_id": "TEST-CORP", "country_code": "DE", "financial_statement_year": "2025"},
        requester_department="CREDIT_RISK",
    )
    assert moodys_res["status_code"] == 200
    assert "Aa2" in str(moodys_res["data"])
    phase_pass("PHASE4", f"Moody's CreditEdge: Rating Aa2, PD 0.08% ({moodys_res['latency_ms']:.2f}ms)")

    # Test Refinitiv sanctions screening
    ref_res = await executor.execute_dynamic_call(
        app_id="agent_refinitiv_sanctions",
        endpoint_path="/api/v1/sanctions/screen-entity",
        http_method="POST",
        parameters={"company_name": "Test GmbH", "country_code": "DE", "vessel_imo": "IMO-001"},
        requester_department="COMPLIANCE_AML",
    )
    assert ref_res["status_code"] == 200
    assert ref_res["data"]["ofac_status"] == "CLEAR_NO_MATCH"
    phase_pass("PHASE4", f"Refinitiv World-Check: OFAC={ref_res['data']['ofac_status']} ({ref_res['latency_ms']:.2f}ms)")

    # Test IBM 3270 Mainframe (UI_ACTION keystroke emulation)
    mf_res = await executor.execute_dynamic_call(
        app_id="agent_mainframe_core_104",
        endpoint_path="SCREEN_104/F3",
        http_method="UI_ACTION",
        parameters={"applicant_acc": "DE89370400440532013000", "beneficiary": "IT60X054", "amount_eur": "100.00"},
        requester_department="CORE_BANKING",
    )
    assert mf_res["status_code"] == 200
    assert mf_res["data"]["terminal_status"] == "COMMITTED_IN_MAINFRAME"
    phase_pass("PHASE4", f"IBM 3270 Mainframe: {mf_res['data']['mainframe_response'][:50]} ({mf_res['latency_ms']:.2f}ms)")

    # Test RBAC enforcement – Credit Risk agent should NOT access portal
    rbac_blocked = False
    try:
        await executor.execute_dynamic_call(
            app_id="agent_customer_trade_portal",
            endpoint_path="/portal/v2/client/request-missing-document",
            http_method="POST",
            parameters={},
            requester_department="CORE_BANKING",  # Not allowed!
        )
    except PermissionError:
        rbac_blocked = True
    assert rbac_blocked, "RBAC should have blocked CORE_BANKING from accessing portal"
    phase_pass("PHASE4", "RBAC enforcement: CORE_BANKING correctly DENIED access to Client Portal")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 5: AUTONOMOUS MULTI-AGENT ORCHESTRATION
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 5: AUTONOMOUS MULTI-AGENT ORCHESTRATION ═══[/bold yellow]")

    problem = (
        "Complex Banking Event: Corporate Client DE-CORP-99214 presented import "
        "documents for Letter of Credit dossier DOS-2026-8831 (Value: EUR 450,000). "
        "Examine documents against UCP 600 rules, autonomously resolve any "
        "discrepancies, transfer EUR 50k credit sublimit, screen sanctions on "
        "Refinitiv, assess default probability on Moody's, generate executive "
        "presentation slides, and send email summary."
    )

    console.print(Panel(problem, title="Incoming Business Problem", border_style="yellow", box=box.ROUNDED))
    console.print("  [dim]Orchestrating resolution across all 8 agents...[/dim]")

    trace = await brain.orchestrate_dynamic_ecosystem(problem)

    # Verify resolution
    assert trace.is_fully_resolved is True, "Orchestration failed to resolve"
    assert len(trace.steps) >= 8, f"Expected >= 8 steps (all 8 apps), got {len(trace.steps)}"
    assert len(mesh.message_ledger) >= 7, f"Expected >= 7 A2A messages, got {len(mesh.message_ledger)}"

    # Verify every application participated
    executing_agents = set(s.executing_agent_id for s in trace.steps)
    expected_agents = {
        "agent_trade_finance_doka", "agent_customer_trade_portal",
        "agent_credit_collateral_sap", "agent_refinitiv_sanctions",
        "agent_moodys_risk_analytics", "agent_office_excel_ppt",
        "agent_microsoft_outlook", "agent_mainframe_core_104",
    }
    missing_agents = expected_agents - executing_agents
    assert len(missing_agents) == 0, f"Agents that did NOT execute: {missing_agents}"

    # Render execution trace
    trace_table = Table(title="Autonomous Multi-Agent Execution Trace", box=box.ROUNDED, show_lines=True)
    trace_table.add_column("Step", style="bold", width=4)
    trace_table.add_column("Agent", style="cyan", width=28)
    trace_table.add_column("Department", style="yellow", width=22)
    trace_table.add_column("Tool Called", style="magenta", width=25)
    trace_table.add_column("Status", style="bold green", width=8)

    for step in trace.steps:
        trace_table.add_row(
            str(step.step_number),
            step.executing_agent_id,
            step.department,
            step.tool_called,
            step.status,
        )
    console.print(trace_table)

    # Render A2A messages
    msg_table = Table(title="A2A Inter-Department Messages", box=box.ROUNDED)
    msg_table.add_column("ID", style="dim", width=10)
    msg_table.add_column("From", style="cyan", width=22)
    msg_table.add_column("To", style="magenta", width=22)
    msg_table.add_column("Intent", style="yellow", width=25)
    msg_table.add_column("Status", style="bold green", width=10)

    for msg in mesh.message_ledger:
        msg_table.add_row(
            msg.message_id[:8] + "…",
            msg.sender_agent_id[:22],
            msg.target_agent_id[:22],
            msg.intent[:25],
            msg.status,
        )
    console.print(msg_table)

    phase_pass("PHASE5",
        f"ALL 8 AGENTS COLLABORATED: {len(trace.steps)} steps, "
        f"{len(mesh.message_ledger)} A2A messages, resolution COMPLETE"
    )

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 6: TEST REST API DASHBOARD (FastAPI in-process)
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 6: TEST REST API DASHBOARD ENDPOINTS ═══[/bold yellow]")

    from auto_connect.dashboard import create_app
    import httpx

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        # 6a: GET /api/v1/ecosystem
        res = await client.get("/api/v1/ecosystem")
        assert res.status_code == 200
        eco = res.json()
        assert eco["total_agents"] >= 4
        phase_pass("PHASE6", f"GET /api/v1/ecosystem → {eco['total_agents']} agents, {len(eco['mcp_tools'])} tools")

        # 6b: GET /.well-known/agent.json (RFC A2A directory)
        res_a2a = await client.get("/.well-known/agent.json")
        assert res_a2a.status_code == 200
        a2a = res_a2a.json()
        assert len(a2a["agents"]) >= 4
        phase_pass("PHASE6", f"GET /.well-known/agent.json → {len(a2a['agents'])} agents published")

        # 6c: GET / (HTML dashboard)
        res_html = await client.get("/")
        assert res_html.status_code == 200
        assert "Auto-Connect" in res_html.text
        assert "Zero-Code Onboarding" in res_html.text or "connect-api-form" in res_html.text
        phase_pass("PHASE6", f"GET / → HTML dashboard loaded ({len(res_html.text)} bytes)")

        # 6d: POST /api/v1/apps/connect – hot-connect Moody's via dashboard API
        connect_res = await client.post("/api/v1/apps/connect", json={
            "app_name": "Moody's Analytics CreditEdge",
            "base_url": "https://api.moodys.com/v2",
            "auth_type": "API_KEY",
            "token": "moodys_test_key_9918",
            "preset_type": "moodys",
        })
        assert connect_res.status_code == 200
        connect_data = connect_res.json()
        assert connect_data["status"] == "SUCCESS"
        assert len(connect_data["pipeline"]) == 5
        # Verify pipeline step names
        pipeline_steps = [p["step"] for p in connect_data["pipeline"]]
        assert "Zero-Trust Security Gate" in pipeline_steps
        assert "A2A Agent Card Generation" in pipeline_steps
        assert "Autonomous Mesh Integration" in pipeline_steps
        new_agent_id = connect_data["agent_card"]["agent_id"]
        phase_pass("PHASE6", f"POST /api/v1/apps/connect → '{new_agent_id}' onboarded via 5-step pipeline")

        # 6e: POST /api/v1/apps/{agent_id}/test – ping the newly connected agent
        test_res = await client.post(f"/api/v1/apps/{new_agent_id}/test")
        assert test_res.status_code == 200
        test_data = test_res.json()
        assert test_data["status"] == "SUCCESS"
        assert test_data["execution_result"]["status_code"] == 200
        phase_pass("PHASE6", f"POST /api/v1/apps/{new_agent_id}/test → Test Ping SUCCESS, HTTP 200")

        # 6f: POST /api/v1/orchestrate – run orchestration via REST
        orch_res = await client.post("/api/v1/orchestrate", json={
            "problem": "Examine L/C dossier DOS-2026-8831 and assess counterparty default probability."
        })
        assert orch_res.status_code == 200
        orch_data = orch_res.json()
        assert orch_data["is_fully_resolved"] is True
        assert len(orch_data["steps"]) >= 3
        phase_pass("PHASE6", f"POST /api/v1/orchestrate → {len(orch_data['steps'])} steps, fully resolved")

        # 6g: DELETE /api/v1/apps/{agent_id} – disconnect
        del_res = await client.delete(f"/api/v1/apps/{new_agent_id}")
        assert del_res.status_code == 200
        phase_pass("PHASE6", f"DELETE /api/v1/apps/{new_agent_id} → Disconnected from mesh")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 7: HOT-CONNECT A BRAND NEW CUSTOM API
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n[bold yellow]═══ PHASE 7: HOT-CONNECT A BRAND NEW CUSTOM API ═══[/bold yellow]")

    # Simulate a completely new bank API that the Brain has never seen before
    custom_spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "Treasury FX Rate Engine",
            "version": "1.0",
            "description": "Real-time foreign exchange rate service for corporate treasury operations",
        },
        "paths": {
            "/api/v1/fx/spot-rate": {
                "get": {
                    "summary": "Retrieves live FX spot rate for a currency pair",
                    "parameters": ["base_currency", "quote_currency", "amount"],
                }
            },
            "/api/v1/fx/execute-conversion": {
                "post": {
                    "summary": "Executes a real-time FX conversion at the current spot rate",
                    "parameters": ["base_currency", "quote_currency", "amount", "customer_id"],
                }
            },
        },
    }

    # Provision credentials
    vault.store_credential(VaultCredential(
        app_id="agent_treasury_fx",
        base_url="https://fx-engine.internal.bank.example",
        auth_type="Bearer",
        token="treasury_fx_tok_7788",
        allowed_departments=["*"],
    ))

    # Register a simple mock handler
    async def mock_fx_handler(method, path, params, headers):
        return {
            "status_code": 200,
            "data": {
                "service": "Treasury FX Rate Engine",
                "pair": f"{params.get('base_currency', 'EUR')}/{params.get('quote_currency', 'USD')}",
                "spot_rate": 1.0847,
                "amount_converted": float(params.get("amount", 0)) * 1.0847,
                "status": "EXECUTED",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    executor.register_in_process_handler("agent_treasury_fx", mock_fx_handler)

    # THE BRAIN auto-analyzes the brand new spec
    fx_card = brain.analyze_and_assimilate_application(custom_spec, "agent_treasury_fx")

    assert fx_card.agent_id == "agent_treasury_fx"
    assert len(fx_card.discovered_tools) == 2
    assert mesh.get_agent("agent_treasury_fx") is not None
    console.print(f"  [green]✓[/green] Brain auto-discovered: [cyan]{fx_card.application_name}[/cyan]")
    console.print(f"    Department: [yellow]{fx_card.banking_department}[/yellow]")
    console.print(f"    Tools: {len(fx_card.discovered_tools)} ({', '.join(t.tool_name for t in fx_card.discovered_tools)})")

    # Execute a live FX call through the new agent
    fx_res = await executor.execute_dynamic_call(
        app_id="agent_treasury_fx",
        endpoint_path="/api/v1/fx/execute-conversion",
        http_method="POST",
        parameters={"base_currency": "EUR", "quote_currency": "USD", "amount": "450000", "customer_id": "DE-CORP-99214"},
        requester_department="TRADE_FINANCE",
    )
    assert fx_res["status_code"] == 200
    assert fx_res["data"]["status"] == "EXECUTED"
    phase_pass("PHASE7",
        f"Hot-connected '{fx_card.application_name}': "
        f"EUR→USD conversion = {fx_res['data']['amount_converted']:,.2f} USD"
    )

    total_final = len(mesh.list_agents())
    phase_pass("PHASE7", f"Mesh now has {total_final} active agents (was 8, added 1 live)")

    # ═══════════════════════════════════════════════════════════════════
    # FINAL SCORECARD
    # ═══════════════════════════════════════════════════════════════════
    console.print("\n")

    passed = sum(1 for r in phase_results if r[0] == "PASS")
    failed = sum(1 for r in phase_results if r[0] == "FAIL")

    score_table = Table(title="Final Integration Test Scorecard", box=box.DOUBLE, show_lines=True)
    score_table.add_column("Result", style="bold", width=8)
    score_table.add_column("Phase", style="cyan", width=10)
    score_table.add_column("Detail", width=70)

    for status, phase, detail in phase_results:
        color = "green" if status == "PASS" else "red"
        score_table.add_row(f"[{color}]{status}[/{color}]", phase, detail)

    console.print(score_table)

    verdict = "ALL TESTS PASSED" if failed == 0 else f"{failed} TEST(S) FAILED"
    verdict_color = "green" if failed == 0 else "red"

    console.print(Panel(
        f"[bold {verdict_color}]{verdict}: {passed}/{passed + failed} checks passed[/bold {verdict_color}]\n\n"
        f"• Total Applications Auto-Discovered: 9 (8 bank + 1 custom FX engine)\n"
        f"• Total Agents in Active Mesh: {total_final}\n"
        f"• Total MCP Tools Registered: {len(mesh.export_mcp_tool_definitions())}\n"
        f"• Total Vault Credentials Sealed: {len(vault.list_credentials())}\n"
        f"• Total A2A Messages Exchanged: {len(mesh.message_ledger)}\n"
        f"• Zero-Trust RBAC Enforcement: VERIFIED\n"
        f"• Legacy IBM 3270 Mainframe: ASSIMILATED (UI_ACTION)\n"
        f"• Dashboard REST API: ALL ENDPOINTS VERIFIED\n"
        f"• Hot-Connect Mid-Flight: WORKING",
        title="🏦 FINAL INTEGRATION VERDICT",
        border_style=verdict_color,
        box=box.DOUBLE,
    ))

    assert failed == 0, f"{failed} test(s) failed – see scorecard above"


if __name__ == "__main__":
    asyncio.run(run_full_start_to_finish())
