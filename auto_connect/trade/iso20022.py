"""
ISO 20022 payment messages (SWIFT CBPR+ / SEPA): since November 2025 cross-border payments on SWIFT are
exchanged as ISO 20022 MX messages, MT103 / MT202 are retired. Trade messages (MT7xx) remain MT.

* pacs.008.001.08 - FI to FI customer credit transfer (payment of a guarantee / surety claim to the beneficiary)
* pacs.009.001.08 - financial institution credit transfer (reimbursement of the presenting bank under an LC)

Each message is wrapped with its Business Application Header (head.001.001.02) as on the SWIFT network.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from xml.sax.saxutils import escape

NS_HEAD = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"
NS = {"pacs.008": "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
      "pacs.009": "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08"}


def _amt(a: Decimal) -> str:
    return f"{Decimal(a).quantize(Decimal('0.01'))}"


def _id(text: str, n: int = 35) -> str:
    return escape("".join(c for c in (text or "") if c.isalnum() or c in "-/")[:n] or "NOTPROVIDED")


def _bah(msg_def: str, ref: str, sender_bic: str, receiver_bic: str, now: str) -> str:
    return (f'<AppHdr xmlns="{NS_HEAD}"><Fr><FIId><FinInstnId><BICFI>{sender_bic}</BICFI></FinInstnId></FIId></Fr>'
            f'<To><FIId><FinInstnId><BICFI>{receiver_bic}</BICFI></FinInstnId></FIId></To>'
            f'<BizMsgIdr>{_id(ref)}</BizMsgIdr><MsgDefIdr>{msg_def}</MsgDefIdr><BizSvc>swift.cbprplus.02</BizSvc>'
            f'<CreDt>{now}</CreDt></AppHdr>')


def pacs008(ref: str, end_to_end: str, value_date: date, currency: str, amount: Decimal, debtor_name: str,
            debtor_iban: str, creditor_name: str, creditor_iban: str, creditor_country: str, remittance: str,
            instructed_agent_bic: str, sender_bic: str, creditor_agent_bic: Optional[str] = None) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cdtr_agt = (f"<CdtrAgt><FinInstnId><BICFI>{creditor_agent_bic}</BICFI></FinInstnId></CdtrAgt>" if creditor_agent_bic
                else "<CdtrAgt><FinInstnId><Othr><Id>NOTPROVIDED</Id></Othr></FinInstnId></CdtrAgt>")
    doc = (f'<Document xmlns="{NS["pacs.008"]}"><FIToFICstmrCdtTrf><GrpHdr><MsgId>{_id(ref)}</MsgId>'
           f'<CreDtTm>{now}</CreDtTm><NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr>'
           f'<CdtTrfTxInf><PmtId><InstrId>{_id(ref)}</InstrId><EndToEndId>{_id(end_to_end)}</EndToEndId>'
           f'<UETR>{_uetr(ref)}</UETR></PmtId>'
           f'<IntrBkSttlmAmt Ccy="{currency}">{_amt(amount)}</IntrBkSttlmAmt><IntrBkSttlmDt>{value_date.isoformat()}</IntrBkSttlmDt>'
           f'<ChrgBr>SHAR</ChrgBr>'
           f'<InstgAgt><FinInstnId><BICFI>{sender_bic}</BICFI></FinInstnId></InstgAgt>'
           f'<InstdAgt><FinInstnId><BICFI>{instructed_agent_bic}</BICFI></FinInstnId></InstdAgt>'
           f'<Dbtr><Nm>{escape(debtor_name[:140])}</Nm></Dbtr><DbtrAcct><Id><IBAN>{debtor_iban}</IBAN></Id></DbtrAcct>'
           f'<DbtrAgt><FinInstnId><BICFI>{sender_bic}</BICFI></FinInstnId></DbtrAgt>{cdtr_agt}'
           f'<Cdtr><Nm>{escape(creditor_name[:140])}</Nm><PstlAdr><Ctry>{creditor_country}</Ctry></PstlAdr></Cdtr>'
           f'<CdtrAcct><Id><IBAN>{creditor_iban}</IBAN></Id></CdtrAcct>'
           f'<RmtInf><Ustrd>{escape(remittance[:140])}</Ustrd></RmtInf></CdtTrfTxInf></FIToFICstmrCdtTrf></Document>')
    return _envelope(_bah("pacs.008.001.08", ref, sender_bic, instructed_agent_bic, now), doc)


def pacs009(ref: str, related_ref: str, value_date: date, currency: str, amount: Decimal, creditor_bic: str,
            instructed_agent_bic: str, sender_bic: str) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    doc = (f'<Document xmlns="{NS["pacs.009"]}"><FICdtTrf><GrpHdr><MsgId>{_id(ref)}</MsgId><CreDtTm>{now}</CreDtTm>'
           f'<NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr>'
           f'<CdtTrfTxInf><PmtId><InstrId>{_id(ref)}</InstrId><EndToEndId>{_id(related_ref)}</EndToEndId>'
           f'<UETR>{_uetr(ref)}</UETR></PmtId>'
           f'<IntrBkSttlmAmt Ccy="{currency}">{_amt(amount)}</IntrBkSttlmAmt><IntrBkSttlmDt>{value_date.isoformat()}</IntrBkSttlmDt>'
           f'<InstgAgt><FinInstnId><BICFI>{sender_bic}</BICFI></FinInstnId></InstgAgt>'
           f'<InstdAgt><FinInstnId><BICFI>{instructed_agent_bic}</BICFI></FinInstnId></InstdAgt>'
           f'<Dbtr><FinInstnId><BICFI>{sender_bic}</BICFI></FinInstnId></Dbtr>'
           f'<Cdtr><FinInstnId><BICFI>{creditor_bic}</BICFI></FinInstnId></Cdtr>'
           f'</CdtTrfTxInf></FICdtTrf></Document>')
    return _envelope(_bah("pacs.009.001.08", ref, sender_bic, instructed_agent_bic, now), doc)


def _uetr(ref: str) -> str:
    """Unique end-to-end transaction reference (UUID v4 format, required for SWIFT gpi tracking)."""
    import uuid

    return str(uuid.uuid4())


def _envelope(header: str, document: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Envelope>{header}{document}</Envelope>\n'
