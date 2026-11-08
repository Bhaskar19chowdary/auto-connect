"""Live reference data parsing (ECB, EU, OFAC) and the document reader (IDR) - offline, with small sample files."""
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from auto_connect.document_reader import DocumentReader, classify, extract_fields, parse_amount, parse_date
from auto_connect.trade.reference_feeds import SanctionsIndex, parse_ecb, parse_eu, parse_ofac

ECB = """<?xml version="1.0" encoding="UTF-8"?><gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube><Cube time='2026-11-06'>
<Cube currency='USD' rate='1.1165'/><Cube currency='GBP' rate='0.83415'/></Cube></Cube></gesmes:Envelope>"""
EU = ('"id","schema","name","aliases","birth_date","countries","addresses","identifiers","sanctions","phones","emails",'
      '"program_ids","dataset","first_seen","last_seen","last_change"\n'
      '"NK-1","Organization","Volkov Maritime Trading LLC","Volkov Maritime","","ru","","","","","","","eu_fsf","","",""\n'
      '"NK-2","Vessel","SEA SHADOW","","","","","IMO 9123457","","","","","eu_fsf","","",""\n')
SDN = '306,"BANCO NACIONAL DE CUBA",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"a.k.a. \'BNC\'."\n' \
      '9001,"DARK STAR",vessel,"IRAN",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"Vessel Registration Identification IMO 9187629."\n'
ALT = '306,220,"aka","NATIONAL BANK OF CUBA",-0- \n'
ADD = '306,199,"Zweierstrasse 35","Zurich CH-8022","Switzerland",-0- \n306,25,-0- ,"Havana","Cuba",-0- \n'


def test_ecb_rates_are_inverted_to_eur_per_unit():
    day, rates = parse_ecb(ECB)
    assert day == date(2026, 11, 6)
    assert abs(rates["USD"] - Decimal("0.8956561")) < Decimal("0.000001")


def test_eu_and_ofac_lists_build_a_searchable_index():
    idx = SanctionsIndex()
    parse_eu(EU, idx)
    parse_ofac(SDN, ALT, ADD, idx)
    assert len(idx.entries) == 4
    hit = idx.search("National Bank of Cuba", 0.8)[0][1]
    assert hit.entry_id == "OFAC-306" and hit.countries == {"CH", "CU"}
    assert idx.search("Volkova Maritime Trading", 0.8)[0][1].entry_id == "NK-1"  # spelling variant
    assert not idx.search("Hanse Solartechnik GmbH", 0.8)
    assert "9187629" in idx.vessels and "9123457" in idx.vessels


def test_amount_and_date_formats():
    assert parse_amount("EUR 300,000.00") == (Decimal("300000.00"), "EUR")
    assert parse_amount("50.000,00 EUR") == (Decimal("50000.00"), "EUR")
    assert parse_amount("USD 1,234") == (Decimal("1234"), "USD")
    assert parse_date("08.11.2026") == parse_date("8 November 2026") == parse_date("Nov 8, 2026") == date(2026, 11, 8)


def test_builtin_reader_extracts_bill_of_lading():
    text = ("BILL OF LADING\nVessel: MSC AURORA IMO 9839131\nPort of loading: Genoa, Italy\nPort of discharge: Hamburg\n"
            "Shipped on board: 08.11.2026\nNumber of original B/L: THREE (3)\nSigned by: MSC as carrier\n")
    assert classify(text) == "BILL_OF_LADING"
    f = extract_fields("BILL_OF_LADING", text)
    assert f["on_board_date"] == "2026-11-08" and f["signed_by"] == "CARRIER" and f["clean"] is True
    assert f["vessel_imo"] == "IMO9839131" and f["originals_issued"] == 3
    r = DocumentReader(None).read([("bl.txt", text.encode())])
    assert r["documents"][0]["needs_review"] is False


def test_gemini_reader_used_when_available_and_fallback_on_error():
    reply = {"documents": [{"doc_type": "COMMERCIAL_INVOICE", "issue_date": "2026-11-08", "originals": 1, "copies": 2,
                            "confidence": 0.95, "fields": {"amount": "300,000.00", "currency": "EUR",
                                                           "beneficiary_name": "Milano Meccanica S.p.A.",
                                                           "applicant_name": "Bayerische Maschinenbau AG",
                                                           "goods_description": "CNC milling machines"}}]}
    brain = SimpleNamespace(preferred_model="gemini-test", client=SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda model, contents, config: SimpleNamespace(text=json.dumps(reply)))))
    r = DocumentReader(brain).read([("invoice.pdf", b"%PDF-1.4 scanned")])
    d = r["documents"][0]
    assert d["engine"] == "Gemini (gemini-test)" and d["fields"]["amount"] == "300000.00" and not d["needs_review"]

    def quota(model, contents, config):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")
    brain.client.models.generate_content = quota
    r = DocumentReader(brain).read([("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)])
    assert r["documents"][0]["needs_review"] and "RESOURCE_EXHAUSTED" in r["notes"][0]
