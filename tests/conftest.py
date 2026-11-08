"""Test configuration: never send real email and keep generated files in a temp folder."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["SEND_LIVE_EMAIL"] = "false"
os.environ["AUTO_CONNECT_COPY_TO_DESKTOP"] = "false"
os.environ.setdefault("AUTO_CONNECT_OUTPUT_DIR", tempfile.mkdtemp(prefix="auto_connect_test_"))
os.environ["AUTO_CONNECT_STATE_FILE"] = ""  # in-memory state during tests
os.environ["TF_AUTO_BATCH_MINUTES"] = "0"  # no background scheduler during tests
os.environ["TF_LIVE_REFERENCE_DATA"] = "false"  # deterministic sample FX rates / sanctions lists
os.environ["AUTO_CONNECT_USERS_FILE"] = os.path.join(tempfile.mkdtemp(prefix="auto_connect_users_"), "users.json")
os.environ["AUTO_CONNECT_AUDIT_FILE"] = os.path.join(tempfile.mkdtemp(prefix="auto_connect_audit_"), "audit.jsonl")
os.environ["AUTO_CONNECT_VAULT_FILE"] = os.path.join(tempfile.mkdtemp(prefix="auto_connect_vault_"), "vault.json")
os.environ["GEMINI_API_KEY"] = ""  # every create_app() would spend live Gemini quota; Gemini tests use a fake client

# Fallback runner for `async def` tests when pytest-asyncio is not installed.
try:
    import pytest_asyncio  # noqa: F401
except ImportError:
    import asyncio
    import inspect

    import pytest

    def pytest_configure(config):
        config.addinivalue_line("markers", "asyncio: run test in an event loop")

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem):
        if inspect.iscoroutinefunction(pyfuncitem.obj):
            args = {k: pyfuncitem.funcargs[k] for k in pyfuncitem._fixtureinfo.argnames}
            asyncio.run(pyfuncitem.obj(**args))
            return True
        return None
