"""
Central runtime settings for Auto-Connect.

All values come from environment variables (loaded from `.env` when python-dotenv
is installed) so nothing environment-specific is hardcoded in the modules.
"""

import os
from typing import List

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def gemini_model() -> str:
    """Model used for live spec analysis (GEMINI_MODEL in .env)."""
    return os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"


def output_dir() -> str:
    """Folder where generated dossiers, SWIFT files and emails are written."""
    path = os.environ.get("AUTO_CONNECT_OUTPUT_DIR", "").strip() or os.path.join(PROJECT_DIR, "output")
    os.makedirs(path, exist_ok=True)
    return path


def output_dirs() -> List[str]:
    """Output folder plus ~/Desktop when AUTO_CONNECT_COPY_TO_DESKTOP=true."""
    dirs = [output_dir()]
    if _flag("AUTO_CONNECT_COPY_TO_DESKTOP"):
        desktop = os.path.expanduser("~/Desktop")
        if os.path.isdir(desktop):
            dirs.append(desktop)
    return dirs


def send_live_email() -> bool:
    """
    Real SMTP sending happens when SMTP_USER/SMTP_PASSWORD are set, unless
    SEND_LIVE_EMAIL=false (the test suite sets this so tests never send mail).
    """
    has_smtp = bool(os.environ.get("SMTP_USER", "").strip() and os.environ.get("SMTP_PASSWORD", "").strip())
    return has_smtp and _flag("SEND_LIVE_EMAIL", default=True)


def bank_sender_email() -> str:
    return (os.environ.get("BANK_SENDER_EMAIL", "").strip() or os.environ.get("SMTP_USER", "").strip()
            or "trade-finance@bank.local")


def default_client_email() -> str:
    """Email used for the seeded demo customers (defaults to your own SMTP mailbox)."""
    return (os.environ.get("DEFAULT_CLIENT_EMAIL", "").strip() or os.environ.get("SMTP_USER", "").strip()
            or "client@example.com")


def client_email(customer_id: str) -> str:
    """Mailbox of one client, e.g. CLIENT_EMAIL_DE_CORP_99214 in .env (else DEFAULT_CLIENT_EMAIL)."""
    key = "CLIENT_EMAIL_" + "".join(c if c.isalnum() else "_" for c in customer_id.upper())
    return os.environ.get(key, "").strip() or default_client_email()


def live_reference_data() -> bool:
    """Download ECB rates and the EU / OFAC sanctions lists (TF_LIVE_REFERENCE_DATA=false keeps the samples)."""
    return _flag("TF_LIVE_REFERENCE_DATA", default=True)


def reference_data_dir() -> str:
    return os.environ.get("REFERENCE_DATA_DIR", "").strip() or os.path.join(PROJECT_DIR, "state", "reference")


def payment_message_format() -> str:
    """ISO20022 (default: pacs.008 / pacs.009, SWIFT CBPR+ since Nov 2025) or MT (legacy MT103 / MT202)."""
    return "MT" if os.environ.get("PAYMENT_MESSAGE_FORMAT", "").strip().upper() == "MT" else "ISO20022"


def api_key() -> str:
    """Optional key protecting the REST API (header X-API-Key). Empty = no check."""
    return os.environ.get("AUTO_CONNECT_API_KEY", "").strip()


def state_file() -> str:
    """JSON file holding the bank's books between restarts ('' = in-memory only)."""
    val = os.environ.get("AUTO_CONNECT_STATE_FILE")
    if val is None:
        return os.path.join(PROJECT_DIR, "state", "bank_state.json")
    val = val.strip()
    if val and not os.path.isabs(val):
        val = os.path.join(PROJECT_DIR, val)
    return val


def database_url() -> str:
    """Where the bank's books live. Default: SQLite file state/bank.db (an existing JSON state file is migrated
    into it once). PostgreSQL: postgresql://user:password@host:5432/db. 'json' = legacy JSON file / memory."""
    val = os.environ.get("AUTO_CONNECT_DATABASE_URL", "").strip()
    if val.lower() == "json":
        return ""
    if not val:
        if not state_file():  # explicitly in-memory (tests, demos)
            return ""
        val = "sqlite:///" + os.path.join(PROJECT_DIR, "state", "bank.db")
    if val.startswith("sqlite:///") and not os.path.isabs(val[len("sqlite:///"):]):
        val = "sqlite:///" + os.path.join(PROJECT_DIR, val[len("sqlite:///"):])
    return val


def bank_bic() -> str:
    return os.environ.get("BANK_BIC", "DEMODEMMXXX").strip() or "DEMODEMMXXX"


def bank_name() -> str:
    return os.environ.get("BANK_NAME", "Demo Bank AG").strip()
