"""
Comprehensive End-to-End Banking Lifecycle Verification
Executes the entire multi-app discovery, Zero-Trust vault sealing,
A2A agent spawning, dynamic tool execution, and multi-agent problem solving.
"""

import asyncio
import json
import os
import sys

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich import box

from auto_connect.models import VaultCredential
from auto_connect.vault import ZeroTrustVault
from auto_connect.executor import UniversalAPIExecutor
from auto_connect.mesh import A2AEcosystemMesh
from auto_connect.brain import AutonomousEcosystemBrain
from auto_connect.mock_apps import (
    mock_mainframe_handler,
    DOKA_SPEC,
    SAP_CREDIT_SPEC,
    PORTAL_SPEC,
    MOODYS_SPEC,
    REFINITIV_SPEC,
    OUTLOOK_TEAMS_SPEC,
    OFFICE_EXCEL_PPT_SPEC,
    MAINFRAME_3270_SCREEN,
    mock_doka_handler,
    mock_sap_handler,
    mock_portal_handler,
    mock_moodys_handler,
    mock_refinitiv_handler,
    mock_outlook_teams_handler,
    mock_office_handler,
)

console = Console()


async def run_full_end_to_end_test():
    console.print(Panel.fit(
        "[bold cyan]🏦 COMPREHENSIVE END-TO-END BANKING LIFECYCLE VERIFICATION[/bold cyan]\n"
        "[dim]Testing complete discovery, security sealing, agent spawning, and multi-app problem resolution[/dim]",
        border_style="cyan",
        box=box.DOUBLE,
    ))

    # =========================================================================
    # STEP 1: INITIALIZE ZERO-TRUST VAULT & CORE ENGINES
    # =========================================================================
    console.print("\n[bold yellow]Step 1: Initializing Zero-Trust Security Vault & Ecosystem Mesh[/bold yellow]")
    vault = ZeroTrustVault()
    mesh = A2AEcosystemMesh()
    executor = UniversalAPIExecutor(vault)
    brain = AutonomousEcosystemBrain(vault=vault, mesh=mesh, executor=executor)

    # Register in-process handlers for all simulated engines
    executor.register_in_process_handler("agent_trade_finance_doka", mock_doka_handler)
    executor.register_in_process_handler("agent_credit_collateral_sap", mock_sap_handler)
    executor.register_in_process_handler("agent_customer_trade_portal", mock_portal_handler)
    executor.register_in_process_handler("agent_mainframe_core_104", mock_mainframe_handler)  # simulated 3270 host
    executor.register_in_process_handler("agent_moodys_risk_analytics", mock_moodys_handler)
    executor.register_in_process_handler("agent_refinitiv_sanctions", mock_refinitiv_handler)
    executor.register_in_process_handler("agent_microsoft_outlook", mock_outlook_teams_handler)
    executor.register_in_process_handler("agent_office_excel_ppt", mock_office_handler)

    console.print("  [green]✓[/green] Zero-Trust Vault initialized with audit logging")
    console.print("  [green]✓[/green] Universal Dynamic Executor wired to Vault Gate")
    console.print("  [green]✓[/green] RFC-A2A Mesh Directory active")

    # =========================================================================
    # STEP 2: PROVISION ZERO-TRUST CREDENTIALS (PASSWORDS & TOKENS)
    # =========================================================================
    console.print("\n[bold yellow]Step 2: Provisioning Bank Application Passwords / Tokens in Vault[/bold yellow]")
    vault_items = [
        VaultCredential(app_id="agent_trade_finance_doka", base_url="https://doka.internal.bank.example", auth_type="mTLS", client_cert="doka_cert_sha256_9901", allowed_departments=["TRADE_FINANCE"]),
        VaultCredential(app_id="agent_credit_collateral_sap", base_url="https://sap-s4.internal.bank.example", auth_type="Bearer", token="s4_jwt_token_valid_2026", allowed_departments=["TRADE_FINANCE", "CREDIT_RISK"]),
        VaultCredential(app_id="agent_customer_trade_portal", base_url="https://portal.internal.bank.example", auth_type="API_KEY", token="portal_api_key_8812", allowed_departments=["TRADE_FINANCE", "CLIENT_ENGAGEMENT"]),
        VaultCredential(app_id="agent_moodys_risk_analytics", base_url="https://api.moodys.com/v2", auth_type="API_KEY", token="moodys_secret_token_7719", allowed_departments=["TRADE_FINANCE", "CREDIT_RISK"]),
        VaultCredential(app_id="agent_refinitiv_sanctions", base_url="https://api.refinitiv.com/v1", auth_type="Bearer", token="refinitiv_oauth_token_3310", allowed_departments=["TRADE_FINANCE", "COMPLIANCE_AML"]),
        VaultCredential(app_id="agent_microsoft_outlook", base_url="https://graph.microsoft.com/v1.0", auth_type="Bearer", token="graph_token_live_5512", allowed_departments=["TRADE_FINANCE", "OFFICE_COMMUNICATION"]),
        VaultCredential(app_id="agent_office_excel_ppt", base_url="https://office.internal.bank.example", auth_type="Bearer", token="office_sec_token_2209", allowed_departments=["TRADE_FINANCE", "FINANCIAL_REPORTING"]),
    ]
    for item in vault_items:
        vault.store_credential(item)
    console.print(f"  [green]✓[/green] Successfully sealed [bold cyan]{len(vault_items)} enterprise credentials[/bold cyan] in Zero-Trust Vault")

    # =========================================================================
    # STEP 3: AUTONOMOUS APPLICATION DISCOVERY & A2A SPIDERING
    # =========================================================================
    console.print("\n[bold yellow]Step 3: Autonomous Introspection & Specialist Agent Spawning[/bold yellow]")
    specs_to_assimilate = [
        (DOKA_SPEC, "agent_trade_finance_doka"),
        (SAP_CREDIT_SPEC, "agent_credit_collateral_sap"),
        (PORTAL_SPEC, "agent_customer_trade_portal"),
        (MOODYS_SPEC, "agent_moodys_risk_analytics"),
        (REFINITIV_SPEC, "agent_refinitiv_sanctions"),
        (OUTLOOK_TEAMS_SPEC, "agent_microsoft_outlook"),
        (OFFICE_EXCEL_PPT_SPEC, "agent_office_excel_ppt"),
    ]

    for raw_spec, hint in specs_to_assimilate:
        card = brain.analyze_and_assimilate_application(raw_spec, hint)
        console.print(
            f"  [green]✓ Spawned Agent:[/green] [bold white]{card.application_name}[/bold white] "
            f"([bold cyan]{card.agent_id}[/bold cyan]) | Dept: [magenta]{card.banking_department}[/magenta] "
            f"| Tools: [yellow]{len(card.discovered_tools)}[/yellow]"
        )

    # Ingest 30-year-old Legacy Mainframe Green-Screen (Zero API)
    mainframe_card = brain.assimilate_legacy_screen(MAINFRAME_3270_SCREEN, "agent_mainframe_core_104")
    console.print(
        f"  [green]✓ Spawned Legacy Mainframe Agent:[/green] [bold white]{mainframe_card.application_name}[/bold white] "
        f"([bold cyan]{mainframe_card.agent_id}[/bold cyan]) | Dept: [magenta]{mainframe_card.banking_department}[/magenta] "
        f"| Method: [yellow]UI_ACTION (Keystroke Emulation)[/yellow]"
    )

    # Verify total agents in mesh
    total_agents = len(mesh.list_agents())
    mcp_tools = mesh.export_mcp_tool_definitions()
    console.print(f"\n  [bold green]Total Active Agents in Mesh: {total_agents}[/bold green] | [bold yellow]Total MCP Tools: {len(mcp_tools)}[/bold yellow]")

    # =========================================================================
    # STEP 4: VERIFY DYNAMIC UNIVERSAL EXECUTOR WITH SAMPLE CALL
    # =========================================================================
    console.print("\n[bold yellow]Step 4: Testing Dynamic Universal Executor with Sample Data[/bold yellow]")
    moodys_res = await executor.execute_dynamic_call(
        app_id="agent_moodys_risk_analytics",
        endpoint_path="/api/v2/ratings/counterparty",
        http_method="POST",
        parameters={
            "customer_id": "DE-CORP-99214",
            "country_code": "DE",
            "financial_statement_year": "2025",
        },
        requester_department="TRADE_FINANCE",
    )
    console.print(f"  [green]✓[/green] Moody's Test Call Status Code: [bold green]{moodys_res['status_code']}[/bold green] (Latency: {moodys_res['latency_ms']}ms)")
    console.print(f"  [dim]Data Returned:[/dim] {json.dumps(moodys_res['data'])}")

    # =========================================================================
    # STEP 5: RUN FULL MULTI-AGENT AUTONOMOUS PROBLEM RESOLUTION
    # =========================================================================
    console.print("\n[bold yellow]Step 5: Executing Complex Real-World Banking Event[/bold yellow]")
    sample_problem = (
        "Complex Banking Event: Corporate Client DE-CORP-99214 presented import documents for Letter of Credit dossier DOS-2026-8831 (Value: EUR 450,000). "
        "Examine documents against UCP 600 rules, autonomously resolve any discrepancies, transfer EUR 50k credit sublimit, "
        "screen sanctions on Refinitiv, assess default probability on Moody's, generate executive presentation slides, and send email summary."
    )
    console.print(Panel(sample_problem, title="Incoming Problem Statement", border_style="magenta", box=box.ROUNDED))

    console.print("[dim]Orchestrating resolution across all 8 agents in mesh...[/dim]")
    trace = await brain.orchestrate_dynamic_ecosystem(sample_problem)

    # Render Step-by-Step Execution Table
    trace_table = Table(title="Autonomous Multi-Agent Execution Trace", box=box.ROUNDED)
    trace_table.add_column("Step", style="dim", width=6)
    trace_table.add_column("Agent / System", style="cyan", width=28)
    trace_table.add_column("Department", style="magenta", width=20)
    trace_table.add_column("Action Taken & Data Generated", style="white")
    trace_table.add_column("Status", style="bold green", width=10)

    for step in trace.steps:
        sample_snippet = json.dumps(step.result)[:65] + "..." if step.result else "Success"
        trace_table.add_row(
            str(step.step_number),
            step.executing_agent_id,
            step.department,
            f"{step.action}\n[dim]Payload Result: {sample_snippet}[/dim]",
            step.status,
        )
    console.print(trace_table)

    # Render A2A Inter-Agent Message Bus
    msg_table = Table(title="Live A2A (Agent-to-Agent) Inter-Department Messages", box=box.ROUNDED)
    msg_table.add_column("Message ID", style="dim", width=10)
    msg_table.add_column("Sender Agent", style="cyan")
    msg_table.add_column("Target Agent", style="magenta")
    msg_table.add_column("Intent", style="yellow")
    msg_table.add_column("Status", style="bold green")

    for msg in mesh.message_ledger:
        msg_table.add_row(
            msg.message_id[:8] + "...",
            msg.sender_agent_id,
            msg.target_agent_id,
            msg.intent,
            msg.status,
        )
    console.print(msg_table)

    # Verify Final Outcome
    assert trace.is_fully_resolved is True, "Orchestration failed to fully resolve"
    assert len(trace.steps) >= 6, f"Expected at least 6 steps, got {len(trace.steps)}"
    assert len(mesh.message_ledger) >= 5, f"Expected at least 5 messages, got {len(mesh.message_ledger)}"

    console.print(Panel(
        f"[bold green]✓ VERIFICATION PASSED: ALL 8 APPLICATIONS COLLABORATED AUTONOMOUSLY[/bold green]\n\n"
        f"• Total Applications Connected: {total_agents}\n"
        f"• Total Resolution Steps Executed: {len(trace.steps)}\n"
        f"• Total A2A Messages Routed: {len(mesh.message_ledger)}\n"
        f"• Final Outcome:\n{trace.final_outcome}",
        title="Final Quality Audit Verdict",
        border_style="green",
        box=box.ROUNDED,
    ))


if __name__ == "__main__":
    asyncio.run(run_full_end_to_end_test())
