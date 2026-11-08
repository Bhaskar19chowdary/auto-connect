"""
Mock Banking Applications
Simulates Tier-1 bank core engines:
1. Surecomp DOKA Trade Finance Engine (UCP 600 / URDG 758)
2. SAP S/4HANA Credit & Collateral Management (Sub-limit reallocation)
3. Customer Trade Portal & Webhook Messenger (Client notifications & waivers)
"""

import os
from typing import Dict, Any, List
import json
from datetime import datetime, timezone


def _num(value, default: float) -> float:
    """Numeric request field; probes and console users may send placeholders or blanks."""
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


# =====================================================================
# 1. RAW OPENAPI SPECIFICATIONS REGISTERED IN BANK API GATEWAY
# =====================================================================

DOKA_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Surecomp DOKA Trade Finance Engine",
        "version": "2026.4",
        "description": "Enterprise Letter of Credit Examination and Bank Guarantee Processing Engine",
    },
    "paths": {
        "/api/v1/lc/examine-ucp600": {
            "post": {
                "summary": "Examines L/C, Bill of Lading, Commercial Invoice, and Insurance against UCP 600 rules",
                "parameters": ["dossier_id", "documents_json"],
            }
        },
        "/api/v1/guarantees/issue-urdg758": {
            "post": {
                "summary": "Vets Bank Guarantee clauses and triggers SWIFT MT760 issuance",
                "parameters": ["customer_id", "amount_eur", "beneficiary", "guarantee_text"],
            }
        },
    },
}

SAP_CREDIT_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "SAP S/4HANA Credit & Collateral Management",
        "version": "2026.1",
        "description": "Enterprise Credit Limit Monitoring, Collateral Allocation, and Facility Sub-limit Transfers",
    },
    "paths": {
        "/sap/opu/odata/credit/reallocate_sublimit": {
            "post": {
                "summary": "Transfers unused credit capacity from Working Capital to Trade Finance sub-limits",
                "parameters": ["customer_id", "source_facility", "target_facility", "amount_eur"],
            }
        }
    },
}

PORTAL_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Customer Trade Portal & Webhook Messenger",
        "version": "4.2",
        "description": "Multi-channel Corporate Portal Alerts and Discrepancy Waiver Acceptance Engine",
    },
    "paths": {
        "/portal/v2/client/request-missing-document": {
            "post": {
                "summary": "Sends real-time alert to client portal login asking client to upload missing trade documents or click discrepancy waiver",
                "parameters": ["customer_id", "dossier_id", "missing_item_description", "allow_one_click_waiver"],
            }
        }
    },
}

# Legacy Mainframe Screen (Zero API, 30-year-old IBM 3270 terminal green-screen)
MAINFRAME_3270_SCREEN: str = """
+------------------------------------------------+
| [CORE BANKING SYSTEM - SCREEN 104]                 |
| APPLICANT ACC: [ DE89370400440532013000 ]      |
| BENEFICIARY  : [ IT60X0542811101000000123456 ] |
| AMOUNT (EUR) : [ 450000.00 ]                   |
| PRESS [F3] TO EXECUTE                          |
+------------------------------------------------+
"""

# =====================================================================
# POPULAR ENTERPRISE BANKING API PRESETS
# =====================================================================

MOODYS_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Moody's Analytics Credit & Risk Scoring",
        "version": "2.4",
        "description": "Calculates Probability of Default (PD), Loss Given Default (LGD), and Corporate Counterparty Ratings",
    },
    "paths": {
        "/api/v2/ratings/counterparty": {
            "post": {
                "summary": "Assesses counterparty risk and outputs Basel III Probability of Default and Credit Rating",
                "parameters": ["customer_id", "country_code", "financial_statement_year"],
            }
        }
    },
}

REFINITIV_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Refinitiv World-Check Sanctions & AML",
        "version": "1.8",
        "description": "Real-time Sanctions Screening, PEP Verification, and Watchlist Enforcement Engine",
    },
    "paths": {
        "/api/v1/sanctions/screen-entity": {
            "post": {
                "summary": "Screens company names, owners, and cargo vessels against OFAC, EU, UN, and Politically Exposed Persons (PEP) lists",
                "parameters": ["company_name", "country_code", "vessel_imo"],
            }
        }
    },
}

OUTLOOK_TEAMS_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Microsoft Outlook & Teams Communicator",
        "version": "3.0",
        "description": "Automated Corporate Email Dispatch, Meeting Transcription, and Executive Summaries",
    },
    "paths": {
        "/api/v1/mail/dispatch-executive-summary": {
            "post": {
                "summary": "Composes and sends an executive transaction summary email with sign-off details to committee members and clients",
                "parameters": ["recipient_emails", "subject", "summary_body", "dossier_id"],
            }
        },
        "/api/v1/teams/meeting-transcription-summary": {
            "post": {
                "summary": "Pulls live meeting audio transcript and generates key decisions and action items for credit committee",
                "parameters": ["meeting_id", "participants"],
            }
        },
    },
}

OFFICE_EXCEL_PPT_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Office Excel & PowerPoint Automation Engine",
        "version": "2.1",
        "description": "Generates Financial Models (.xlsx) and Board-Ready Executive Slide Decks (.pptx)",
    },
    "paths": {
        "/api/v1/office/build-financial-model": {
            "post": {
                "summary": "Builds/updates an Excel financial model with facility breakdown and sensitivity analysis",
                "parameters": ["dossier_id", "financial_metrics", "output_filename"],
            }
        },
        "/api/v1/office/generate-executive-presentation": {
            "post": {
                "summary": "Generates a 5-slide executive presentation deck (.pptx) summarizing the transaction for the board",
                "parameters": ["dossier_id", "presentation_title", "slides_outline"],
            }
        },
    },
}


# =====================================================================
# 2. IN-PROCESS DISPATCH HANDLERS
# =====================================================================

async def mock_doka_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Surecomp DOKA engine."""
    if "/api/v1/lc/examine-ucp600" in path:
        dossier = params.get("dossier_id", "DOS-UNKNOWN")
        return {
            "status_code": 200,
            "data": {
                "dossier_id": dossier,
                "examination_status": "DISCREPANCIES_DETECTED",
                "standard": "ICC UCP 600 (2007 Revision)",
                "discrepancies": [
                    {
                        "article": "Article 28 (Insurance Document and Coverage)",
                        "code": "UCP-DISC-028",
                        "severity": "BLOCKING",
                        "message": "Insurance Certificate missing endorsement in blank; waiver or updated doc required.",
                    },
                    {
                        "article": "Facility Assessment",
                        "code": "CREDIT-SHORTFALL-EUR-50K",
                        "severity": "WARNING",
                        "message": "Credit facility capacity requires EUR 50,000 reallocation to satisfy L/C presentation tolerance margin.",
                    },
                ],
                "action_required": "NOTIFY_APPLICANT_AND_ADJUST_SUBLIMIT",
                "examined_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/guarantees/issue-urdg758" in path:
        return {
            "status_code": 200,
            "data": {
                "guarantee_reference": "URDG758-2026-DMB-9812",
                "status": "APPROVED",
                "swift_message_type": "MT760",
                "beneficiary": params.get("beneficiary"),
                "amount_eur": params.get("amount_eur", 0.0),
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in DOKA"}}


async def mock_sap_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates SAP S/4HANA Credit Management."""
    if "/sap/opu/odata/credit/reallocate_sublimit" in path:
        amount = _num(params.get("amount_eur"), 50000.0)
        return {
            "status_code": 200,
            "data": {
                "transaction_id": "SAP-S4-TRX-2026-90412",
                "customer_id": params.get("customer_id", "DE-CORP-99214"),
                "source_facility": params.get("source_facility", "FAC_WORKING_CAPITAL_EUR"),
                "target_facility": params.get("target_facility", "FAC_TRADE_FINANCE_LC"),
                "reallocated_amount_eur": amount,
                "source_remaining_capacity_eur": 1450000.0 - amount,
                "target_new_capacity_eur": 500000.0 + amount,
                "execution_status": "COMMITTED_IN_ODATA_LEDGER",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in SAP"}}


async def mock_portal_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Customer Trade Portal & Webhook Messenger."""
    if "/portal/v2/client/request-missing-document" in path:
        return {
            "status_code": 200,
            "data": {
                "notification_ticket": "ALERT-CP-2026-7731",
                "customer_id": params.get("customer_id", "DE-CORP-99214"),
                "dossier_id": params.get("dossier_id", "DOS-2026-8831"),
                "channels_notified": ["WEB_PORTAL_POPUP", "MOBILE_PUSH", "WEBHOOK_EVENT"],
                "missing_item": params.get("missing_item_description", "Insurance Certificate"),
                "one_click_waiver_enabled": params.get("allow_one_click_waiver", True),
                "waiver_action_status": "AUTOMATICALLY_ACCEPTED_BY_CORPORATE_TREASURY_POLICY",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in Portal"}}


async def mock_moodys_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Moody's Analytics credit rating engine."""
    return {
        "status_code": 200,
        "data": {
            "service": "Moody's Analytics CreditEdge",
            "customer_id": params.get("customer_id", "DE-CORP-99214"),
            "credit_rating": "Aa2 (High Grade)",
            "probability_of_default_1yr": "0.08%",
            "loss_given_default": "35.0%",
            "esg_risk_score": "LOW",
            "regulatory_status": "APPROVED_FOR_TRADE_LENDING",
            "calculated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def mock_refinitiv_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Refinitiv World-Check sanctions screening engine."""
    return {
        "status_code": 200,
        "data": {
            "service": "Refinitiv World-Check One",
            "query_entity": params.get("company_name", "DE-CORP-99214 Export GmbH"),
            "ofac_status": "CLEAR_NO_MATCH",
            "eu_sanctions_match": False,
            "un_sanctions_match": False,
            "pep_exposure": "NONE",
            "compliance_verdict": "UNCONDITIONAL_PASS",
            "certificate_id": "WC-2026-EU-991204",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


async def mock_outlook_teams_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Microsoft Outlook email and Teams meeting intelligence."""
    if "mail" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Microsoft Graph Mail API",
                "message_id": "OUTLOOK-MSG-2026-991823",
                "delivery_status": "SENT_TO_INBOX",
                "recipients": params.get("recipient_emails", ["credit-committee@bank.example", "client-treasury@decorp.de"]),
                "subject": params.get("subject", "Transaction Cleared: L/C Dossier DOS-2026-8831"),
                "attachments_included": ["Resolution_Audit_Trace.pdf", "Executive_Summary.docx"],
                "dispatched_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {
        "status_code": 200,
        "data": {
            "service": "Microsoft Teams Meeting Intelligence",
            "meeting_id": params.get("meeting_id", "MEETING-CREDIT-COMMITTEE-2026"),
            "transcription_status": "PROCESSED",
            "unanimous_decision": "APPROVED",
            "speakers_detected": ["Head of Credit Risk", "Senior Trade Finance Officer", "Relationship Manager"],
            "minutes_summary": "All members voted in favor of approving EUR 50k sublimit reallocation and accepting waiver.",
        },
    }


async def mock_office_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates automated generation of Excel models and PowerPoint presentations."""
    if "presentation" in path:
        return {
            "status_code": 200,
            "data": {
                "file_type": "POWERPOINT_SLIDES",
                "filename": f"Credit_Committee_Presentation_{params.get('dossier_id', 'DOS-2026-8831')}.pptx",
                "slides_count": 5,
                "slides": [
                    "Slide 1: Transaction Executive Brief",
                    "Slide 2: Facility Limit Utilization & Capital Assessment",
                    "Slide 3: UCP 600 Discrepancy Resolution & Customer Waiver",
                    "Slide 4: SAP Intra-Facility Reallocation Details (€50,000)",
                    "Slide 5: Committee Sign-off & Audit Signature Log",
                ],
                "status": "GENERATED_AND_SAVED",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    # Excel financial model branch (previously unreachable because it was
    # indented under the presentation branch, so this handler returned None).
    dossier = params.get("dossier_id", "DOS-2026-8831")
    filename = f"Financial_Model_{dossier}.xlsx"
    try:
        from .excel_exporter import generate_trade_finance_excel
        from .settings import output_dirs
        for out_dir in output_dirs():
            generate_trade_finance_excel(dossier_id=dossier, customer_id="DE-CORP-99214", amount_eur=450000.0,
                                         output_path=os.path.join(out_dir, filename))
    except Exception:
        pass

    return {
        "status_code": 200,
        "data": {
            "file_type": "EXCEL_WORKBOOK",
            "filename": filename,
            "sheets_created": ["Deal_Summary", "Facility_Limits", "Compliance_Risk", "Multi_Agent_Audit"],
            "status": "SAVED_TO_DOCUMENT_REPOSITORY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }


# =====================================================================
# ENTERPRISE BANKING API PRESETS – BATCH 2
# =====================================================================

SWIFT_ALLIANCE_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "SWIFT Alliance Lite2 Payment Gateway",
        "version": "2026.2",
        "description": "Cross-border SWIFT payment processing for MT103 (Single Customer Credit Transfer), MT202 (General Financial Institution Transfer), and gpi end-to-end tracking",
    },
    "paths": {
        "/api/v1/swift/send-mt103": {
            "post": {
                "summary": "Initiates a cross-border MT103 Single Customer Credit Transfer via SWIFT Alliance Lite2 with gpi tracking",
                "parameters": ["sender_bic", "receiver_bic", "amount_eur", "currency", "value_date", "reference"],
            }
        },
        "/api/v1/swift/send-mt202": {
            "post": {
                "summary": "Sends an interbank MT202 General Financial Institution Transfer for cover payments and nostro funding",
                "parameters": ["sender_bic", "intermediary_bic", "receiver_bic", "amount_eur", "currency", "reference"],
            }
        },
        "/api/v1/swift/gpi-track": {
            "get": {
                "summary": "Retrieves SWIFT gpi end-to-end payment tracker status including correspondent bank legs",
                "parameters": ["uetr", "reference"],
            }
        },
    },
}

BLOOMBERG_MARKET_DATA_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Bloomberg Terminal Market Data API",
        "version": "3.5",
        "description": "Real-time and historical FX spot/forward rates, government bond prices, yield curves, and credit spread data for Treasury and Risk departments",
    },
    "paths": {
        "/api/v2/market-data/fx-spot": {
            "get": {
                "summary": "Retrieves real-time FX spot rate and bid/ask spread for a given currency pair from Bloomberg Terminal",
                "parameters": ["instrument_type", "base_currency", "quote_currency", "notional_eur"],
            }
        },
        "/api/v2/market-data/yield-curve": {
            "get": {
                "summary": "Returns interpolated government bond yield curve (EUR, USD, GBP) with tenor points and basis point spreads",
                "parameters": ["curve_currency", "curve_date", "tenor_points"],
            }
        },
        "/api/v2/market-data/bond-valuation": {
            "post": {
                "summary": "Calculates mark-to-market valuation for a fixed income instrument by ISIN using Bloomberg pricing",
                "parameters": ["isin", "nominal_amount", "valuation_date"],
            }
        },
    },
}

WOLTERS_KLUWER_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Wolters Kluwer OneSumX Regulatory Reporting",
        "version": "2026.1",
        "description": "Basel III/IV capital adequacy calculations, CRR/CRD regulatory reporting, CET1 ratio monitoring, and Risk-Weighted Asset (RWA) computation engine",
    },
    "paths": {
        "/api/v1/regulatory/capital-adequacy": {
            "post": {
                "summary": "Calculates Basel III/IV capital adequacy ratios including CET1, Tier 1, Total Capital, and Leverage Ratio for a reporting entity",
                "parameters": ["entity_lei", "reporting_date", "exposure_amount_eur"],
            }
        },
        "/api/v1/regulatory/rwa-calculation": {
            "post": {
                "summary": "Computes Risk-Weighted Assets (RWA) under CRR II standardised and IRB approaches for credit, market, and operational risk",
                "parameters": ["entity_lei", "asset_class", "exposure_amount_eur", "pd_percent", "lgd_percent"],
            }
        },
    },
}

FINASTRA_FUSION_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Finastra Fusion Treasury Management",
        "version": "2026.3",
        "description": "Enterprise cash positioning, intraday liquidity forecasting, FX hedging strategy execution, and cash pool management for corporate treasury operations",
    },
    "paths": {
        "/api/v1/treasury/liquidity-position": {
            "get": {
                "summary": "Returns real-time cash and liquidity position across all nostro accounts, cash pools, and money market instruments",
                "parameters": ["cash_pool_id", "currency", "forecast_horizon_days"],
            }
        },
        "/api/v1/treasury/fx-hedge": {
            "post": {
                "summary": "Executes an FX forward or swap hedge to mitigate currency exposure on a projected cash flow or trade finance obligation",
                "parameters": ["base_currency", "quote_currency", "notional_amount", "hedge_type", "maturity_date"],
            }
        },
    },
}

AVALOQ_WEALTH_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Avaloq Wealth Management Platform",
        "version": "5.2",
        "description": "Private banking client portfolio management, KYC/AML enhanced due diligence for High Net Worth (HNW) and Ultra-HNW clients, FATCA/CRS compliance, and investment suitability",
    },
    "paths": {
        "/api/v1/wealth/portfolio-summary": {
            "get": {
                "summary": "Retrieves consolidated portfolio summary for a private banking client including asset allocation, performance, and risk metrics",
                "parameters": ["client_id", "portfolio_id", "reporting_currency"],
            }
        },
        "/api/v1/wealth/kyc-aml-check": {
            "post": {
                "summary": "Performs enhanced KYC/AML due diligence check for HNW private banking clients including FATCA/CRS classification and PEP screening",
                "parameters": ["client_id", "kyc_type", "fatca_check"],
            }
        },
    },
}

DTCC_TRADE_REPOSITORY_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "DTCC Global Trade Repository",
        "version": "4.1",
        "description": "EMIR and MiFID II/MiFIR regulatory trade reporting, Unique Transaction Identifier (UTI) generation, derivative position management, and LEI-based counterparty validation",
    },
    "paths": {
        "/api/v1/trade-repository/emir-report": {
            "post": {
                "summary": "Submits an EMIR/MiFID II regulatory trade report to the DTCC Global Trade Repository with UTI and LEI-based counterparty identification",
                "parameters": ["uti", "counterparty_lei", "notional_amount_eur", "asset_class"],
            }
        },
        "/api/v1/trade-repository/position-reconciliation": {
            "post": {
                "summary": "Reconciles outstanding derivative positions against DTCC repository records by counterparty LEI and asset class",
                "parameters": ["counterparty_lei", "asset_class", "valuation_date"],
            }
        },
    },
}

EBICS_SPEC: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Demo Bank EBICS Multi-Banking Gateway",
        "version": "3.0",
        "description": "Electronic Banking Internet Communication Standard (EBICS 3.0) for German & European Corporate Payments, SEPA pain.001 file dispatch, CAMT.053 intraday account statements, and A006 Digital Signatures",
    },
    "paths": {
        "/api/v3/ebics/upload-pain001": {
            "post": {
                "summary": "Submits a corporate SEPA credit transfer payment file (pain.001.001.09) with German A006 electronic signature",
                "parameters": ["customer_id", "order_type", "total_amount_eur", "number_of_transactions"],
            }
        },
        "/api/v3/ebics/fetch-camt053": {
            "get": {
                "summary": "Downloads end-of-day electronic account statement in ISO 20022 CAMT.053 XML format",
                "parameters": ["account_iban", "statement_date"],
            }
        },
        "/api/v3/ebics/verify-subscriber-keys": {
            "post": {
                "summary": "Validates EBICS 3.0 INI/HIA cryptographic public keys (A006 signature, E002 encryption, X002 authentication)",
                "parameters": ["partner_id", "user_id", "key_version"],
            }
        },
    },
}


# =====================================================================
# IN-PROCESS DISPATCH HANDLERS – BATCH 2
# =====================================================================

async def mock_swift_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates SWIFT Alliance Lite2 cross-border payment gateway."""
    if "/api/v1/swift/send-mt103" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "SWIFT Alliance Lite2",
                "message_type": "MT103",
                "transaction_reference": f"SWFT-MT103-{params.get('reference', 'REF-UNKNOWN')}",
                "uetr": "eb6305c2-1a7f-4e4e-b7a3-ef0d2886c6a1",
                "sender_bic": params.get("sender_bic", "DEMODEMM"),
                "receiver_bic": params.get("receiver_bic", "BNCAITMM"),
                "amount": params.get("amount_eur", "450000.00"),
                "currency": params.get("currency", "EUR"),
                "value_date": params.get("value_date", "2026-11-07"),
                "gpi_status": "ACSP",
                "gpi_status_description": "Accepted Settlement in Progress",
                "correspondent_chain": [
                    {"leg": 1, "bic": "DEMODEMM", "status": "COMPLETED", "timestamp": datetime.now(timezone.utc).isoformat()},
                    {"leg": 2, "bic": "DEUTDEFF", "status": "IN_PROGRESS", "timestamp": datetime.now(timezone.utc).isoformat()},
                ],
                "charges": {"sender_charges_eur": "15.00", "charge_type": "SHA"},
                "execution_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/swift/send-mt202" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "SWIFT Alliance Lite2",
                "message_type": "MT202",
                "transaction_reference": f"SWFT-MT202-{params.get('reference', 'REF-UNKNOWN')}",
                "sender_bic": params.get("sender_bic", "DEMODEMM"),
                "intermediary_bic": params.get("intermediary_bic", "DEUTDEFF"),
                "receiver_bic": params.get("receiver_bic", "BNCAITMM"),
                "amount": params.get("amount_eur", "450000.00"),
                "currency": params.get("currency", "EUR"),
                "cover_payment_status": "SETTLED",
                "nostro_account_debited": "DE44700202700012345678",
                "execution_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/swift/gpi-track" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "SWIFT gpi Tracker",
                "uetr": params.get("uetr", "eb6305c2-1a7f-4e4e-b7a3-ef0d2886c6a1"),
                "reference": params.get("reference", "REF-UNKNOWN"),
                "gpi_status": "ACCC",
                "gpi_status_description": "Accepted Settlement Completed",
                "total_legs": 3,
                "completed_legs": 3,
                "end_to_end_time_seconds": 847,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in SWIFT Alliance"}}


async def mock_bloomberg_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Bloomberg Terminal market data service."""
    if "/api/v2/market-data/fx-spot" in path:
        base = params.get("base_currency", "EUR")
        quote = params.get("quote_currency", "USD")
        return {
            "status_code": 200,
            "data": {
                "service": "Bloomberg Market Data",
                "instrument": f"{base}/{quote} SPOT",
                "ticker": f"{base}{quote} Curncy",
                "bid": 1.08452,
                "ask": 1.08468,
                "mid": 1.08460,
                "spread_pips": 1.6,
                "notional_eur": params.get("notional_eur", "450000.00"),
                "converted_amount": f"{_num(params.get('notional_eur'), 450000.0) * 1.08460:.2f}",
                "source": "Bloomberg B-PIPE",
                "snapshot_time": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v2/market-data/yield-curve" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Bloomberg Yield Curve",
                "curve_currency": params.get("curve_currency", "EUR"),
                "curve_date": params.get("curve_date", "2026-11-07"),
                "tenor_points": {
                    "3M": -0.15, "6M": 0.02, "1Y": 0.28, "2Y": 0.65,
                    "5Y": 1.12, "10Y": 1.78, "30Y": 2.15,
                },
                "curve_type": "Government Bond Yield",
                "interpolation": "Nelson-Siegel-Svensson",
                "source": "Bloomberg BVAL",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v2/market-data/bond-valuation" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Bloomberg Bond Valuation",
                "isin": params.get("isin", "DE0001102580"),
                "instrument_name": "Bundesrepublik Deutschland 0.25% 2026",
                "nominal_amount": params.get("nominal_amount", "1000000.00"),
                "clean_price": 99.847,
                "dirty_price": 100.012,
                "accrued_interest": 0.165,
                "yield_to_maturity_bps": 32.5,
                "modified_duration": 0.87,
                "valuation_date": params.get("valuation_date", "2026-11-07"),
                "source": "Bloomberg BVAL",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in Bloomberg"}}


async def mock_wolters_kluwer_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Wolters Kluwer OneSumX regulatory reporting engine."""
    if "/api/v1/regulatory/capital-adequacy" in path:
        exposure = _num(params.get("exposure_amount_eur"), 450000.0)
        return {
            "status_code": 200,
            "data": {
                "service": "Wolters Kluwer OneSumX",
                "report_type": "CAPITAL_ADEQUACY_ASSESSMENT",
                "entity_lei": params.get("entity_lei", "529900DEMOBANKAG0034"),
                "reporting_date": params.get("reporting_date", "2026-11-07"),
                "capital_ratios": {
                    "cet1_ratio_percent": 14.82,
                    "tier1_ratio_percent": 16.15,
                    "total_capital_ratio_percent": 19.43,
                    "leverage_ratio_percent": 5.21,
                },
                "risk_weighted_assets_eur": "128450000000.00",
                "exposure_rwa_impact_eur": f"{exposure * 0.75:.2f}",
                "lcr_percent": 142.7,
                "nsfr_percent": 118.3,
                "regulatory_framework": "CRR II / CRD V / Basel III.1",
                "compliance_status": "WITHIN_REGULATORY_LIMITS",
                "minimum_cet1_required_percent": 10.56,
                "buffer_available_bps": 426,
                "calculated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/regulatory/rwa-calculation" in path:
        exposure = _num(params.get("exposure_amount_eur"), 450000.0)
        return {
            "status_code": 200,
            "data": {
                "service": "Wolters Kluwer OneSumX RWA Engine",
                "entity_lei": params.get("entity_lei", "529900DEMOBANKAG0034"),
                "asset_class": params.get("asset_class", "Corporates"),
                "approach": "IRB_ADVANCED",
                "exposure_at_default_eur": f"{exposure:.2f}",
                "pd_percent": params.get("pd_percent", "0.08"),
                "lgd_percent": params.get("lgd_percent", "35.0"),
                "risk_weight_percent": 52.3,
                "rwa_amount_eur": f"{exposure * 0.523:.2f}",
                "expected_loss_eur": f"{exposure * 0.0008 * 0.35:.2f}",
                "calculated_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in OneSumX"}}


async def mock_finastra_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Finastra Fusion Treasury Management system."""
    if "/api/v1/treasury/liquidity-position" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Finastra Fusion Treasury",
                "cash_pool_id": params.get("cash_pool_id", "DMB-EUR-MAIN-POOL"),
                "currency": params.get("currency", "EUR"),
                "position_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "opening_balance_eur": "2847500000.00",
                "intraday_inflows_eur": "412300000.00",
                "intraday_outflows_eur": "389100000.00",
                "current_position_eur": "2870700000.00",
                "forecast_horizon_days": params.get("forecast_horizon_days", 30),
                "forecast_min_position_eur": "2015400000.00",
                "forecast_max_position_eur": "3125800000.00",
                "liquidity_buffer_status": "ADEQUATE",
                "ecb_minimum_reserve_eur": "1250000000.00",
                "surplus_available_eur": "1620700000.00",
                "nostro_accounts": [
                    {"correspondent": "Deutsche Bundesbank", "bic": "MARKDEFF", "balance_eur": "1450000000.00"},
                    {"correspondent": "BNP Paribas", "bic": "BNPAFRPP", "balance_eur": "892500000.00"},
                    {"correspondent": "Citibank NA", "bic": "CITIUS33", "balance_eur": "528200000.00"},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/treasury/fx-hedge" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Finastra Fusion FX Hedging",
                "hedge_reference": "FX-HEDGE-2026-DMB-44219",
                "hedge_type": params.get("hedge_type", "FORWARD"),
                "base_currency": params.get("base_currency", "EUR"),
                "quote_currency": params.get("quote_currency", "USD"),
                "notional_amount": params.get("notional_amount", "450000.00"),
                "forward_rate": 1.08892,
                "spot_reference_rate": 1.08460,
                "forward_points_pips": 43.2,
                "maturity_date": params.get("maturity_date", "2027-02-07"),
                "hedge_status": "CONFIRMED_AND_BOOKED",
                "counterparty": "Banca Esempio S.p.A. Milan",
                "booking_timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in Finastra Fusion"}}


async def mock_avaloq_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Avaloq Wealth Management Platform for private banking."""
    if "/api/v1/wealth/portfolio-summary" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Avaloq Wealth Management",
                "client_id": params.get("client_id", "DE-CORP-99214"),
                "portfolio_id": params.get("portfolio_id", "PB-DMB-WM-88412"),
                "reporting_currency": params.get("reporting_currency", "EUR"),
                "total_aum_eur": "18750000.00",
                "asset_allocation": {
                    "equities_percent": 35.2,
                    "fixed_income_percent": 28.7,
                    "alternatives_percent": 15.4,
                    "real_estate_percent": 12.1,
                    "cash_equivalents_percent": 8.6,
                },
                "ytd_performance_percent": 7.84,
                "risk_profile": "BALANCED_GROWTH",
                "var_99_1d_eur": "187500.00",
                "client_segment": "UHNW",
                "relationship_manager": "Private Banking Munich – Desk III",
                "last_review_date": "2026-10-15",
                "next_review_date": "2027-01-15",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/wealth/kyc-aml-check" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Avaloq KYC/AML Engine",
                "client_id": params.get("client_id", "DE-CORP-99214"),
                "kyc_type": params.get("kyc_type", "ENHANCED_DUE_DILIGENCE"),
                "kyc_status": "COMPLETED_CLEAR",
                "risk_classification": "MEDIUM",
                "pep_screening_result": "NO_MATCH",
                "adverse_media_result": "NO_MATCH",
                "fatca_status": "COMPLIANT" if params.get("fatca_check") else "NOT_APPLICABLE",
                "crs_classification": "REPORTING_FI",
                "source_of_wealth_verified": True,
                "beneficial_owner_identified": True,
                "edd_trigger_reasons": [],
                "next_periodic_review": "2027-05-07",
                "certificate_id": "KYC-EDD-2026-DMB-55891",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in Avaloq"}}


async def mock_dtcc_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates DTCC Global Trade Repository for EMIR/MiFID trade reporting."""
    if "/api/v1/trade-repository/emir-report" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "DTCC Global Trade Repository",
                "report_type": "EMIR_TRADE_REPORT",
                "uti": params.get("uti", "DEMODEMM202611070001"),
                "reporting_counterparty_lei": "529900DEMOBANKAG0034",
                "other_counterparty_lei": params.get("counterparty_lei", "549300TRUWO2CD2G5692"),
                "asset_class": params.get("asset_class", "ForeignExchange"),
                "notional_amount_eur": params.get("notional_amount_eur", "450000.00"),
                "action_type": "NEWT",
                "level": "TRADE",
                "execution_venue": "XOFF",
                "confirmation_status": "CONFIRMED",
                "submission_status": "ACCEPTED",
                "tra_submission_id": "DTCC-GTR-2026-EU-771204",
                "regulatory_regime": "EMIR REFIT / MiFIR RTS 25",
                "reporting_deadline_met": True,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v1/trade-repository/position-reconciliation" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "DTCC Position Reconciliation",
                "counterparty_lei": params.get("counterparty_lei", "549300TRUWO2CD2G5692"),
                "asset_class": params.get("asset_class", "ForeignExchange"),
                "valuation_date": params.get("valuation_date", "2026-11-07"),
                "positions_matched": 47,
                "positions_unmatched": 0,
                "positions_disputed": 0,
                "total_notional_matched_eur": "2845000000.00",
                "reconciliation_status": "FULLY_RECONCILED",
                "pairing_rate_percent": 100.0,
                "report_id": "DTCC-RECON-2026-991407",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {"status_code": 404, "data": {"error": f"Endpoint {path} not found in DTCC GTR"}}


async def mock_ebics_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """Simulates Demo Bank EBICS 3.0 Multi-Banking Gateway."""
    auth_header = headers.get("Authorization", "") or headers.get("X-API-KEY", "")
    token = auth_header.replace("Bearer ", "").strip()

    # Real EBICS requires an authorized subscriber electronic key (A005 / A006)
    # Rejects made up or dummy strings like 'sha256_ebics_demo_signa' or 'wrong'
    token_lower = token.lower()
    is_authorized = token.startswith("EBICS_A006_") or token.startswith("DMB_EBICS_KEY_")
    has_dummy_marker = any(m in token_lower for m in ["wrong", "fake", "invalid", "dummy", "signa", "sample"])

    if not is_authorized or has_dummy_marker:
        return {
            "status_code": 401,
            "data": {
                "service": "Demo Bank EBICS Multi-Banking Gateway",
                "status": "REJECTED_BY_BANK_GATEWAY",
                "error": "401 UNAUTHORIZED: EBICS_AUTHENTICATION_ERROR",
                "detail": f"Subscriber key '{token[:16]}...' rejected by the bank's EBICS Security Module. A006 digital signature key is not registered in the bank's Partner Registry for PartnerId DEMO-DE-0029.",
                "ebics_return_code": "091008 (EBICS_AUTHENTICATION_ERROR)",
                "remedy": "Authorized corporate signatories must print and submit a physically signed INI Letter (Initialisierungsbrief) to the bank's branch.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    if "/api/v3/ebics/upload-pain001" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Demo Bank EBICS Multi-Banking Gateway",
                "ebics_version": "3.0",
                "order_type": "CCT (Corporate Credit Transfer)",
                "order_id": "DMB-EBICS-ORD-2026-9921",
                "partner_id": "DEMO-DE-0029",
                "user_id": "CORP-TREASURER-01",
                "execution_status": "ORDER_ACCEPTED_AND_PENDING_SETTLEMENT",
                "total_amount_eur": params.get("total_amount_eur", "450000.00"),
                "number_of_transactions": params.get("number_of_transactions", 1),
                "electronic_signature_status": "A006_SIGNATURE_VERIFIED_EU_EIDAS",
                "sepa_message_id": "SEPA-DMB-20261107-009921",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v3/ebics/fetch-camt053" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Demo Bank EBICS Multi-Banking Gateway",
                "statement_format": "CAMT.053.001.08",
                "iban": params.get("account_iban", "DE89370400440532013000"),
                "closing_booked_balance_eur": "12845200.50",
                "statement_number": "2026-311",
                "currency": "EUR",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    elif "/api/v3/ebics/verify-subscriber-keys" in path:
        return {
            "status_code": 200,
            "data": {
                "service": "Demo Bank EBICS Multi-Banking Gateway",
                "partner_id": "DEMO-DE-0029",
                "status": "KEYS_ACTIVE_AND_AUTHORIZED",
                "signature_type": "A006 (RSA-4096)",
                "encryption_type": "E002 (RSA-2048)",
                "authentication_type": "X002 (RSA-2048)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
    return {
        "status_code": 200,
        "data": {
            "service": "Demo Bank EBICS Multi-Banking Gateway",
            "endpoint_called": path,
            "status": "PROCESSED_SUCCESSFULLY",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }




async def mock_mainframe_handler(method: str, path: str, params: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    """SIMULATOR of the legacy 3270 core-banking screen (demo mode only): nothing is booked anywhere."""
    if method != "UI_ACTION":
        return {"status_code": 405, "data": {"error": "The simulated mainframe only accepts screen actions"}}
    return {"status_code": 200, "data": {
        "simulated": True, "terminal_status": "SIMULATED_COMMIT", "screen_action": path, "fields_typed": params,
        "mainframe_response": "SIMULATED HOST RESPONSE - RC 00 (demo simulator, no real booking)"}}
