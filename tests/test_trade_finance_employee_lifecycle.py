"""
End-to-End Verification: Human Trade Finance Operations Desk Simulation
Tests clean vs defective application processing, autonomous discrepancy resolution,
email dispatch to the client, and BaFin Excel generation.
"""

import pytest
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from auto_connect.dashboard import create_app


@pytest.mark.asyncio
async def test_human_trade_finance_employee_lifecycle():
    app = create_app()
    client_email = "client@example.com"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # =====================================================================
        # CASE 1: CLEAN APPLICATION (Zero Mistakes, Straight-Through Processing)
        # =====================================================================
        res_clean = await client.post("/api/v1/trade/examine-application", json={
            "dossier_id": "DOS-2026-8831",
            "applicant_name": "DE-CORP-99214 Export GmbH",
            "client_email": client_email,
            "beneficiary_name": "Milano Meccanica S.p.A. (Italy)",
            "lc_amount_eur": 450000.0,
            "available_trade_line_eur": 500000.0,
            "scenario_type": "clean",
            "documents": {
                "commercial_invoice": True,
                "bill_of_lading": True,
                "insurance_certificate": True,
                "packing_list": True,
                "certificate_of_origin": True,
            },
        })
        assert res_clean.status_code == 200
        clean_data = res_clean.json()
        assert clean_data["status"] == "SUCCESS"
        assert clean_data["total_discrepancies_found"] == 0
        assert clean_data["final_verdict"] == "CLEAN_APPROVAL_STP_PASSED"
        assert len(clean_data["resolution_steps"]) >= 3

        # =====================================================================
        # CASE 2: DEFECTIVE APPLICATION (Realistic Human Mistakes & Discrepancies)
        # =====================================================================
        res_defective = await client.post("/api/v1/trade/examine-application", json={
            "dossier_id": "DOS-2026-8831",
            "applicant_name": "DE-CORP-99214 Export GmbH",
            "client_email": client_email,
            "beneficiary_name": "Milano Meccanica S.p.A. (Italy)",
            "lc_amount_eur": 450000.0,
            "available_trade_line_eur": 400000.0,  # €50k shortfall!
            "scenario_type": "defective",
            "documents": {
                "commercial_invoice": True,
                "bill_of_lading": True,
                "insurance_certificate": False,  # Missing under CIF!
                "packing_list": True,
                "certificate_of_origin": True,
            },
        })
        assert res_defective.status_code == 200
        def_data = res_defective.json()
        assert def_data["status"] == "SUCCESS"
        assert def_data["total_discrepancies_found"] >= 3

        # 1. Verify exact real-world discrepancy findings
        disc_codes = [d["code"] for d in def_data["discrepancies"]]
        assert "UCP600-ART-28" in disc_codes  # Missing insurance
        assert "FACILITY-LIMIT-SHORTFALL" in disc_codes  # €50k limit shortfall
        assert "UCP600-ART-14J" in disc_codes  # Typo in beneficiary address

        # 2. Verify Email was dispatched to the client
        assert def_data["email_notice_dispatched"] is True
        assert def_data["email_recipient"] == client_email
        assert def_data["eml_file"] is not None

        # 3. Verify RFC 822 .eml file exists in the output folder
        from email import message_from_binary_file, policy
        from auto_connect.settings import output_dir, bank_sender_email

        eml_file = os.path.join(output_dir(), def_data["eml_file"])
        assert os.path.exists(eml_file)
        with open(eml_file, "rb") as f:
            msg = message_from_binary_file(f, policy=policy.default)
        assert msg["To"] == client_email
        assert bank_sender_email() in msg["From"]
        body = msg.get_body(preferencelist=("plain",)).get_content()
        assert "50,000" in body

        # 4. Verify Multi-Agent Resolution Actions
        actions = [s["action"] for s in def_data["resolution_steps"]]
        assert "EXAMINE_UCP600" in actions
        assert "DISPATCH_CLIENT_DISCREPANCY_NOTICE" in actions
        assert "REALLOCATE_SUBLIMIT_ODATA" in actions
        assert "EXECUTE_ONE_CLICK_WAIVER" in actions
        assert "SCREEN_VESSEL_AND_PORTS" in actions
        assert "COMMIT_SCREEN_104" in actions

        # 5. Verify Excel Dossier was regenerated with client email
        excel_path = os.path.join(output_dir(), def_data["excel_file"])  # generated files go to the output folder
        assert os.path.exists(excel_path)
        assert os.path.getsize(excel_path) > 3000

        # 6. Verify Email Download Endpoint works
        res_eml_dl = await client.get("/api/v1/dossier/email")
        assert res_eml_dl.status_code == 200
        assert "message/rfc822" in res_eml_dl.headers.get("content-type", "")

        print("\n✓ ALL HUMAN TRADE FINANCE OPERATIONS CHECKS PASSED PERFECTLY!")


if __name__ == "__main__":
    asyncio.run(test_human_trade_finance_employee_lifecycle())
