"""
Trade Finance Excel Generator
Generates native, multi-tab Microsoft Excel (.xlsx) workbooks for trade finance applications,
facility limit allocations, compliance audits, and multi-agent execution traces.
Built in 100% pure standard Python (zero external dependencies).
"""

import os
import zipfile
import io
from typing import Dict, Any, List
from datetime import datetime, timezone


def _escape_xml(text: str) -> str:
    """Escapes special characters for XML strings."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def generate_trade_finance_excel(
    dossier_id: str,
    customer_id: str,
    amount_eur: float,
    applicant_name: str = "DE-CORP-99214 Export GmbH",
    beneficiary_name: str = "Milano Meccanica S.p.A. (Italy)",
    output_path: str = None,
    trace_steps: List[Any] = None,
    applicant_email: str = "",
) -> str:
    """
    Creates a professional, multi-tab Microsoft Excel (.xlsx) banking dossier.
    Tabs:
      1. Deal_Summary: Application details, UCP 600 compliance, approval verdict
      2. Facility_Limits: SAP credit line utilization, intra-facility reallocation
      3. Compliance_Risk: Refinitiv OFAC screening, Moody's Basel III PD/LGD
      4. Multi_Agent_Audit: Full A2A inter-department transaction ledger
    """
    if not output_path:
        output_path = f"Trade_Finance_Dossier_{dossier_id}.xlsx"

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ══════════ SHEET 1: Deal Executive Summary ══════════
    s1_rows = [
        ["TRADE FINANCING — APPLICATION DOSSIER", ""],
        ["Documentary Credit & Guarantee Examination Record (UCP 600 / URDG 758)", ""],
        ["Generated At", now_iso],
        ["Status", "APPROVED & COMMITTED IN HOST LEDGER"],
        ["", ""],
        ["TRANSACTION PARAMETERS", "DETAILS"],
        ["Dossier Reference ID", dossier_id],
        ["Applicant Customer ID", customer_id],
        ["Applicant Name", applicant_name],
        ["Applicant Client Email", applicant_email],
        ["Applicant IBAN", "DE89370400440532013000"],
        ["Beneficiary Name", beneficiary_name],
        ["Beneficiary IBAN", "IT60X0542811101000000123456"],
        ["Issuance Value (EUR)", f"€{amount_eur:,.2f}"],
        ["Governing Standard", "ICC Uniform Customs and Practice for Documentary Credits (UCP 600)"],
        ["Discrepancy Email Notification", f"DISPATCHED TO INBOX ({applicant_email}) - RFC 822 EML"],
        ["Discrepancy Resolution", "UCP-DISC-028 (Insurance Endorsement) Accepted via Client Portal One-Click Waiver"],
        ["Core Ledger Host Status", "TRANSACTION POSTED (IBM 3270 SCREEN 104 - HOST RETURN CODE 00)"],
    ]

    # ══════════ SHEET 2: Facility Limits & Collateral (SAP S/4HANA) ══════════
    s2_rows = [
        ["SAP S/4HANA CREDIT & COLLATERAL MANAGEMENT LEDGER", "", "", ""],
        ["Intra-Facility Sub-limit Reallocation Record (OData Ledger)", "", "", ""],
        ["", "", "", ""],
        ["Facility Account", "Initial Capacity (EUR)", "Adjustment (EUR)", "New Capacity (EUR)"],
        ["FAC_WORKING_CAPITAL_EUR", "€1,450,000.00", "-€50,000.00", "€1,400,000.00"],
        ["FAC_TRADE_FINANCE_LC", "€500,000.00", "+€50,000.00", "€550,000.00"],
        ["TOTAL CORPORATE LINE", "€1,950,000.00", "€0.00", "€1,950,000.00"],
        ["", "", "", ""],
        ["Transaction ID", "SAP-S4-TRX-2026-90412", "", ""],
        ["Collateral Coverage", "110% Hypothek / Commercial Real Estate Lien", "", ""],
        ["Execution Status", "COMMITTED_IN_ODATA_LEDGER", "", ""],
    ]

    # ══════════ SHEET 3: Compliance & Risk Analytics ══════════
    s3_rows = [
        ["TRADE FINANCING — COMPLIANCE, SANCTIONS & RISK SCORING", ""],
        ["Refinitiv World-Check, Moody's Analytics & Regulatory Capital Impact", ""],
        ["", ""],
        ["RISK ASSESSMENT CATEGORY", "VERDICT / METRIC"],
        ["Refinitiv OFAC Sanctions Status", "CLEAR_NO_MATCH (Certificate: WC-2026-EU-991204)"],
        ["EU / UN Sanctions Match", "FALSE (No match on EU Consolidated List)"],
        ["Politically Exposed Person (PEP)", "NONE (Zero PEP exposure detected)"],
        ["Moody's Credit Rating", "Aa2 (High Grade - Superior creditworthiness)"],
        ["1-Year Probability of Default (PD)", "0.08%"],
        ["Loss Given Default (LGD)", "35.0%"],
        ["Basel III / CRR II Capital Impact", "RWA Impact: €337,500.00 (CET1 Ratio: 14.82% vs Min: 10.56%)"],
        ["Treasury Liquidity Buffer", "ADEQUATE (Cash Pool: €2.87B with Deutsche Bundesbank)"],
    ]

    # ══════════ SHEET 4: Multi-Agent Execution Audit Trail ══════════
    s4_rows = [
        ["A2A (AGENT-TO-AGENT) AUTONOMOUS EXECUTION AUDIT TRAIL", "", "", ""],
        ["Immutable Zero-Trust Execution Steps across Bank Departments", "", "", ""],
        ["", "", "", ""],
        ["Step #", "Specialist Agent", "Banking Department", "Autonomous Action Executed"],
    ]

    if trace_steps:
        for s in trace_steps:
            if isinstance(s, dict):
                agent_id = str(s.get("executing_agent_id", ""))
                dept = str(s.get("department", ""))
                action = str(s.get("action", ""))
                num = str(s.get("step_number", ""))
            else:
                agent_id = getattr(s, "executing_agent_id", "")
                dept = getattr(s, "department", "")
                action = getattr(s, "action", "")
                num = getattr(s, "step_number", "")
            s4_rows.append([str(num), str(agent_id), str(dept), str(action)])
    else:
        # Default typical 8-agent steps
        defaults = [
            ("1", "agent_trade_finance_doka", "TRADE_FINANCE", "Called lc_examine_ucp600 to examine L/C dossier DOS-2026-8831"),
            ("2", "agent_customer_trade_portal", "CLIENT_ENGAGEMENT", "A2A Collaboration: Resolved MISSING_TRADE_DOCUMENT by invoking portal waiver"),
            ("3", "agent_credit_collateral_sap", "CREDIT_RISK", "A2A Collaboration: Resolved CREDIT_LIMIT_SHORTFALL by reallocating €50k sublimit"),
            ("4", "agent_refinitiv_sanctions", "COMPLIANCE_AML", "A2A Collaboration: Resolved SANCTIONS_CHECK_HOLD by invoking World-Check One"),
            ("5", "agent_moodys_risk_analytics", "CREDIT_RISK", "A2A Collaboration: Resolved UNRATED_COUNTERPARTY by computing Aa2 PD/LGD"),
            ("6", "agent_office_excel_ppt", "FINANCIAL_REPORTING", "A2A Collaboration: Generated 5-slide Executive PowerPoint presentation"),
            ("7", "agent_microsoft_outlook", "OFFICE_COMMUNICATION", "A2A Collaboration: Dispatched executive summary email to credit committee"),
            ("8", "agent_mainframe_core_104", "CORE_BANKING", "A2A Collaboration: Posted core settlement to IBM 3270 Mainframe Screen 104"),
        ]
        for row in defaults:
            s4_rows.append(list(row))

    sheets = [
        ("Deal_Summary", s1_rows),
        ("Facility_Limits", s2_rows),
        ("Compliance_Risk", s3_rows),
        ("Multi_Agent_Audit", s4_rows),
    ]

    # Build pure XML for each sheet
    sheet_xmls = []
    for sheet_idx, (name, rows) in enumerate(sheets, start=1):
        lines = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
            "  <sheetData>",
        ]
        col_letters = ["A", "B", "C", "D", "E", "F", "G"]
        for row_idx, row in enumerate(rows, start=1):
            lines.append(f'    <row r="{row_idx}">')
            for c_idx, val in enumerate(row):
                if c_idx >= len(col_letters):
                    break
                col_ref = f"{col_letters[c_idx]}{row_idx}"
                escaped = _escape_xml(val)
                lines.append(f'      <c r="{col_ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
            lines.append("    </row>")
        lines.append("  </sheetData>")
        lines.append("</worksheet>")
        sheet_xmls.append("\n".join(lines))

    # Build XLSX Zip Archive
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        # [Content_Types].xml
        zf.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet4.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )

        # _rels/.rels
        zf.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )

        # xl/workbook.xml
        zf.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Deal_Summary" sheetId="1" r:id="rId1"/>
    <sheet name="Facility_Limits" sheetId="2" r:id="rId2"/>
    <sheet name="Compliance_Risk" sheetId="3" r:id="rId3"/>
    <sheet name="Multi_Agent_Audit" sheetId="4" r:id="rId4"/>
  </sheets>
</workbook>""",
        )

        # xl/_rels/workbook.xml.rels
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/>
</Relationships>""",
        )

        # Write each sheet
        for idx, xml_content in enumerate(sheet_xmls, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", xml_content)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(zbuf.getvalue())

    return os.path.abspath(output_path)
