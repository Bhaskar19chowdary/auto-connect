"""A broken vault entry (e.g. an unresolvable secret reference) is skipped with an error, never stops the platform;
bank-system credentials are not written to the vault file."""
import json

from auto_connect.models import VaultCredential
from auto_connect.vault import ZeroTrustVault


def test_broken_entry_is_skipped_and_bank_systems_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOD_SECRET", "s3cret-value")
    v = ZeroTrustVault()
    v.store_credential(VaultCredential(app_id="agent_good", base_url="https://a", auth_type="Bearer", token="env:GOOD_SECRET"))
    v.store_credential(VaultCredential(app_id="tf_swift_gateway", base_url="sftp://x", auth_type="Bearer", token="env:GOOD_SECRET"))
    path = tmp_path / "vault.json"
    v.save_to_json(str(path))
    saved = json.load(open(path))
    assert "tf_swift_gateway" not in saved and saved["agent_good"]["token"] == "env:GOOD_SECRET"  # reference, not value
    saved["agent_broken"] = {"base_url": "https://b", "auth_type": "Bearer", "token": "env:MISSING_SECRET"}
    path.write_text(json.dumps(saved))
    monkeypatch.delenv("MISSING_SECRET", raising=False)
    v2 = ZeroTrustVault()
    assert v2.load_from_json(str(path)) == 1
    assert "agent_good" in v2._credentials and "agent_broken" in v2.load_errors
