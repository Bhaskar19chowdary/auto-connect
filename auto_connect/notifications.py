"""
Outbound email channel shared by every workflow.

Every message is always written as an RFC 822 .eml file to the output folder
(the bank's evidence copy). When SMTP credentials are configured and
SEND_LIVE_EMAIL is not "false", the same message is also relayed via SMTP.
"""

import os
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Dict, Any, List, Optional

from . import settings

logger = logging.getLogger("auto_connect.notifications")


def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def send_email(
    to: List[str],
    subject: str,
    plain_text: str,
    html: Optional[str] = None,
    file_stem: Optional[str] = None,
    cc: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Builds, archives and (optionally) relays an email. Returns delivery details."""
    sender = settings.bank_sender_email()
    msg = EmailMessage()
    msg["From"] = f"Trade Finance Operations <{sender}>"
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain="tradefinance.local")
    msg.set_content(plain_text)
    if html:
        msg.add_alternative(html, subtype="html")

    stem = file_stem or f"EMAIL_{safe_name(subject)[:60]}"
    eml_paths = []
    for out_dir in settings.output_dirs():
        path = os.path.join(out_dir, f"{stem}.eml")
        with open(path, "wb") as f:
            f.write(bytes(msg))
        eml_paths.append(path)

    relayed = False
    relay_error = None
    if settings.send_live_email():
        try:
            import smtplib

            host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
            port = int(os.environ.get("SMTP_PORT", 587))
            user = os.environ["SMTP_USER"].strip()
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(user, os.environ["SMTP_PASSWORD"].strip())
                s.send_message(msg, from_addr=user, to_addrs=list(to) + list(cc or []))
            relayed = True
        except Exception as err:  # delivery failure must not stop the workflow, but is reported
            relay_error = str(err)
            logger.warning(f"SMTP relay failed: {err}")

    return {
        "to": to,
        "cc": cc or [],
        "subject": subject,
        "eml_file": eml_paths[0],
        "relayed_via_smtp": relayed,
        "relay_error": relay_error,
    }
