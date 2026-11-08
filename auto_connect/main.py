"""
Auto-Connect Main CLI Runner & Demonstration
Executes the Autonomous Discovery & Ecosystem Bootstrapper loop.
"""

import os
import sys
import json
import argparse
import asyncio
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
    DOKA_SPEC,
    SAP_CREDIT_SPEC,
    PORTAL_SPEC,
    mock_doka_handler,
    mock_sap_handler,
    mock_portal_handler,
)

console = Console()


async def run_cli_demo():
    console.print(Panel.fit(
        "[bold cyan]⚡ AUTO-CONNECT: AUTONOMOUS ECOSYSTEM BOOTSTRAPPER[/bold cyan]\n"
        "[dim]Zero-Config Application Introspection & A2A Mesh Synthesis for Tier-1 Banks (Demo Bank)[/dim]",
        border_style="cyan",
        box=box.DOUBLE,
    ))

    # Phase 1: Zero-Trust Security Gate Provisioning
    console.print("\n[bold yellow]🔒 PHASE 1: Zero-Trust Security Gate & Vault Provisioning[/bold yellow]")
    vault = ZeroTrustVault()
    mesh = A2AEcosystemMesh()
    executor = UniversalAPIExecutor(vault)
    brain = AutonomousEcosystemBrain(vault=vault, mesh=mesh, executor=executor)

    # Register in-process mock dispatchers
    executor.register_in_process_handler("agent_trade_finance_doka", mock_doka_handler)
    executor.register_in_process_handler("agent_credit_collateral_sap", mock_sap_handler)
    executor.register_in_process_handler("agent_customer_trade_portal", mock_portal_handler)

    # IT Admin stores credentials once into HashiCorp Vault / Secret Manager
    vault_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vault_credentials.json")
    if os.path.exists(vault_file):
        vault.load_from_json(vault_file)
    else:
        credentials = [
            VaultCredential(
                app_id="agent_trade_finance_doka",
                base_url="https://doka.internal.bank.example",
                auth_type="mTLS",
                client_cert="sha256_doka_prod_gateway_cert_9901",
                allowed_departments=["TRADE_FINANCE", "RISK_AUDIT"],
            ),
            VaultCredential(
                app_id="agent_credit_collateral_sap",
                base_url="https://sap-s4.internal.bank.example",
                auth_type="Bearer",
                token="demo_vault_s4_sec_tok_9918491823719",
                allowed_departments=["TRADE_FINANCE", "CREDIT_RISK"],
            ),
            VaultCredential(
                app_id="agent_customer_trade_portal",
                base_url="https://portal-api.internal.bank.example",
                auth_type="API_KEY",
                token="demo_api_portal_key_381290381023",
                allowed_departments=["TRADE_FINANCE", "CLIENT_ENGAGEMENT"],
            ),
        ]
        for cred in credentials:
            vault.store_credential(cred)

    vault_table = Table(title="Zero-Trust Vault Registry", box=box.ROUNDED)
    vault_table.add_column("App ID", style="cyan")
    vault_table.add_column("Base URL", style="white")
    vault_table.add_column("Auth Type", style="magenta")
    vault_table.add_column("RBAC Depts", style="green")

    for cred_meta in vault.list_credentials():
        vault_table.add_row(
            cred_meta["app_id"],
            cred_meta["base_url"],
            cred_meta["auth_type"],
            ", ".join(cred_meta["allowed_departments"]),
        )
    console.print(vault_table)

    # Phase 2 & 3: Application Discovery & Dynamic Agent Card Generation
    console.print("\n[bold green]🤖 PHASE 2 & 3: Autonomous Introspection & A2A Agent Spawning[/bold green]")
    raw_apps = [
        (DOKA_SPEC, "agent_trade_finance_doka"),
        (SAP_CREDIT_SPEC, "agent_credit_collateral_sap"),
        (PORTAL_SPEC, "agent_customer_trade_portal"),
    ]

    for raw_spec, app_hint in raw_apps:
        title = raw_spec["info"]["title"]
        console.print(f"  [dim]Scanning schema for:[/dim] [bold white]{title}[/bold white]...")
        card = brain.analyze_and_assimilate_application(raw_spec, app_hint)
        console.print(
            f"  [bold green]✓ Spawned Agent:[/bold green] [cyan]{card.agent_id}[/cyan] "
            f"([bold]{card.banking_department}[/bold]) with [yellow]{len(card.discovered_tools)} executable tools[/yellow]"
        )

    # Assimilate Legacy Mainframe Screen (Zero API, 30-year-old IBM 3270 Terminal)
    from auto_connect.mock_apps import MAINFRAME_3270_SCREEN
    console.print("  [dim]Scanning legacy terminal screen (Computer Use / Vision):[/dim] [bold white]IBM 3270 Core Banking[/bold white]...")
    mainframe_card = brain.assimilate_legacy_screen(MAINFRAME_3270_SCREEN, "agent_mainframe_core_104")
    console.print(
        f"  [bold green]✓ Spawned Legacy Agent:[/bold green] [cyan]{mainframe_card.agent_id}[/cyan] "
        f"([bold]{mainframe_card.banking_department}[/bold]) with [yellow]{len(mainframe_card.discovered_tools)} UI Action tool[/yellow]"
    )

    # Tree of Auto-Discovered Tools & Issue Resolution Capabilities
    tree = Tree("[bold cyan]Bank Mesh: Auto-Generated A2A Agent Cards & Capabilities[/bold cyan]")
    for agent in mesh.list_agents():
        agent_branch = tree.add(f"[bold white]{agent.application_name}[/bold white] ([magenta]{agent.banking_department}[/magenta]) - [dim]{agent.agent_id}[/dim]")
        for tool in agent.discovered_tools:
            tool_branch = agent_branch.add(f"[green]{tool.http_method}[/green] [white]{tool.endpoint_path}[/white] -> [yellow]{tool.tool_name}[/yellow]")
            tool_branch.add(f"[dim]Description:[/dim] {tool.description}")
            if tool.can_resolve_issues:
                issues = ", ".join(tool.can_resolve_issues)
                tool_branch.add(f"[bold red]Resolves Issues:[/bold red] [red]{issues}[/red]")
    console.print(tree)

    # Phase 4 & 5: Autonomous Multi-Agent Event Execution
    console.print("\n[bold magenta]⚡ PHASE 4 & 5: Autonomous Mesh Communication & Workflow Execution[/bold magenta]")
    problem = (
        "Live Banking Event: Corporate Client DE-CORP-99214 presented import documents under L/C dossier DOS-2026-8831. "
        "Examine documents against UCP 600 rules, and autonomously resolve any discrepancies and credit facility shortfalls."
    )
    console.print(Panel(problem, title="Incoming Business Problem", border_style="magenta", box=box.ROUNDED))

    console.print("[dim]Orchestrating resolution across discovered agents...[/dim]")
    trace = await brain.orchestrate_dynamic_ecosystem(problem)

    trace_table = Table(title="Autonomous Execution Trace (Zero-Code Dynamic Dispatch)", box=box.ROUNDED)
    trace_table.add_column("Step", style="dim", width=6)
    trace_table.add_column("Executing Agent", style="cyan", width=28)
    trace_table.add_column("Department", style="magenta", width=18)
    trace_table.add_column("Action & Resolution", style="white")
    trace_table.add_column("Status", style="bold green", width=10)

    for step in trace.steps:
        trace_table.add_row(
            str(step.step_number),
            step.executing_agent_id,
            step.department,
            step.action,
            step.status,
        )
    console.print(trace_table)

    # Display A2A Message Ledger
    msg_table = Table(title="Live A2A (Agent-to-Agent) Inter-Department Ledger", box=box.ROUNDED)
    msg_table.add_column("Message ID", style="dim", width=12)
    msg_table.add_column("Sender", style="cyan")
    msg_table.add_column("Target", style="magenta")
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

    console.print(Panel(
        f"[bold green]✓ ALL BANKING BLOCKERS RESOLVED AUTONOMOUSLY[/bold green]\n\n{trace.final_outcome}",
        title="Final Outcome",
        border_style="green",
        box=box.ROUNDED,
    ))


async def run_trade_demo():
    """Full lifecycle demo of the Trade Finance Operations Engine (books kept in memory)."""
    from datetime import timedelta
    from auto_connect.trade.engine import TradeFinanceEngine
    from auto_connect.trade.state import StateStore
    from auto_connect.trade import samples

    vault, mesh = ZeroTrustVault(), A2AEcosystemMesh()
    engine = TradeFinanceEngine(StateStore(path=""), vault, mesh, UniversalAPIExecutor(vault))
    today = engine.today
    console.print(Panel.fit("[bold cyan]Trade Finance Operations Engine - end-to-end demo[/bold cyan]", border_style="cyan"))

    lc = await engine.apply_lc(samples.sample_lc(today))
    name = engine.state.customers[lc.customer_id].name
    await engine.present_documents(lc.ref, samples.sample_presentation(lc.terms, today, today, name, with_insurance=False))
    pid = lc.presentations[0].presentation_id
    if lc.presentations[0].status == "DISCREPANT_AWAITING_APPLICANT":
        await engine.applicant_waiver_decision(lc.ref, pid, waive=True)
    g = await engine.apply_guarantee(samples.sample_guarantee(today))
    await engine.present_demand(g.ref, samples.sample_demand(today))
    b = await engine.apply_surety(samples.sample_surety(today))
    await engine.present_surety_claim(b.ref, samples.sample_surety_claim(today))
    await engine.run_daily_batch(today + timedelta(days=14))

    for deal in (lc, g, b):
        table = Table(title=f"{deal.product} {deal.ref} -> {deal.status}", box=box.ROUNDED, show_lines=False)
        table.add_column("Date", style="dim", width=10)
        table.add_column("Step", style="cyan")
        table.add_column("System", style="magenta")
        table.add_column("Outcome", style="bold")
        table.add_column("Detail")
        for e in deal.history:
            if e.step in ("FX", "ARCHIVE"):
                continue
            table.add_row(str(e.business_date), e.step, e.system, e.outcome, e.detail[:110])
        console.print(table)
    st = engine.state
    console.print(f"[green]SWIFT messages:[/green] {len(st.swift_messages)}  [green]Ledger entries:[/green] {len(st.ledger)}  "
                  f"[green]A2A messages:[/green] {len(mesh.message_ledger)}  [green]Books balanced:[/green] {st.ledger_balanced()}")


def main():
    parser = argparse.ArgumentParser(description="Auto-Connect: Autonomous Banking Discovery & A2A Ecosystem")
    parser.add_argument("--server", action="store_true", help="Launch interactive Web Dashboard & REST API server")
    parser.add_argument("--port", type=int, default=8088, help="Port for the Dashboard server (default: 8088)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--trade-demo", action="store_true",
                        help="Run LC / guarantee / surety lifecycles end to end in the terminal (in-memory books)")
    args = parser.parse_args()

    if args.trade_demo:
        asyncio.run(run_trade_demo())
        return

    if args.server:
        from auto_connect.dashboard import start_server
        console.print(f"[bold cyan]Starting Auto-Connect Dashboard on http://{args.host}:{args.port}...[/bold cyan]")
        start_server(host=args.host, port=args.port)
    else:
        asyncio.run(run_cli_demo())


if __name__ == "__main__":
    main()

