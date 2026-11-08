"""
Read-only Excel report of the trade finance books for staff, managers and auditors (the database stays the official
record). One sheet each: Deals, Bookings, Fees, Decisions, SWIFT messages, Documents.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List


def _num(v: Any):
    if v in (None, ""):
        return None
    try:
        return float(Decimal(str(v)))
    except Exception:
        return v


def build_report(state, bank_name: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    header_font, header_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3A5F")

    def sheet(title: str, headers: List[str], rows: List[List[Any]], money_cols=(), first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = header_font, header_fill
            c.alignment = Alignment(vertical="center")
        for r in rows:
            ws.append(r)
        for col in money_cols:
            for cell in ws.iter_cols(min_col=col, max_col=col, min_row=2):
                for c in cell:
                    c.number_format = "#,##0.00"
        for i, h in enumerate(headers, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:500] if i - 1 < len(r) and r[i - 1] is not None])
            ws.column_dimensions[get_column_letter(i)].width = min(max(10, width + 2), 60)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        return ws

    deals = list(state.deals.values())
    sheet("Deals", ["Reference", "Product", "Customer", "Status", "Currency", "Amount", "Outstanding", "Issue date",
                    "Entered by", "Created", "Open decisions", "Documents stored"],
          [[d.ref, d.product, d.customer_id, d.status, d.currency, _num(d.amount), _num(d.outstanding),
            d.issue_date.isoformat() if d.issue_date else None, d.created_by, d.created_at[:19], len(d.open_exceptions),
            len(getattr(d, "attachments", []) or [])] for d in deals], money_cols=(6, 7), first=True)
    sheet("Bookings", ["Booking date", "Value date", "Deal", "Debit account", "Credit account", "Amount", "Currency", "Text"],
          [[e.booking_date.isoformat(), e.value_date.isoformat(), e.deal_ref, e.debit_account, e.credit_account, _num(e.amount),
            e.currency, e.narrative] for e in state.ledger], money_cols=(6,))
    sheet("Fees", ["Deal", "Customer", "Date", "Fee", "Amount (EUR)", "Period end"],
          [[d.ref, d.customer_id, f.date.isoformat(), f.fee_type, _num(f.amount), f.period_end.isoformat() if f.period_end else None]
           for d in deals for f in d.fees], money_cols=(5,))
    sheet("Decisions", ["Id", "Deal", "Category", "Status", "Reason", "Decided by", "Approvers", "Note", "Raised"],
          [[e.exception_id, e.deal_ref, e.category, e.status, e.reason, e.resolved_by, ", ".join(e.approvers),
            e.resolution_note, e.created_at[:19]] for e in state.exceptions.values()])
    sheet("SWIFT messages", ["Id", "Deal", "Type", "Receiver", "Status", "Created"],
          [[m.message_id, m.deal_ref, m.mt_type, m.receiver_bic, m.status, m.created_at[:19]] for m in state.swift_messages.values()])
    sheet("Documents", ["File id", "Deal", "Presentation / demand / claim", "File name", "Type", "Size (bytes)", "SHA-256",
                        "Uploaded", "Uploaded by"],
          [[a["file_id"], a["deal_ref"], a["item_id"], a["filename"], a["mime"], a["size"], a["sha256"], a["uploaded_at"][:19],
            a.get("uploaded_by")] for d in deals for a in (getattr(d, "attachments", []) or [])])
    info = wb.create_sheet("About")
    for row in [["Report", "Trade finance books - read-only extract"], ["Bank", bank_name],
                ["Business date", state.business_date.isoformat()],
                ["Generated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
                ["Books balanced", "YES" if state.ledger_balanced() else "NO"],
                ["Note", "The database is the official record; this file is a copy for reporting."]]:
        info.append(row)
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 18, 70
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
