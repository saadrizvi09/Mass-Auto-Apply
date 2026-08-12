"""Offline tests for the supported external-platform layer.
The browser-driving is validated live on first run; here we cover the pure logic:
the persisted dedupe set, the role mapping, and unknown-platform handling."""
import json
import os

os.environ.setdefault("DRY_RUN", "true")

from app.integrations import platforms
from app.services import platform_apply


def test_dedupe_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "APPLIED_PATH", tmp_path / "applied.json")
    assert not platforms._already_applied("yc:https://x/jobs/1")
    platforms._mark_applied("yc", "yc:https://x/jobs/1", "Acme")
    assert platforms._already_applied("yc:https://x/jobs/1")
    saved = json.loads((tmp_path / "applied.json").read_text(encoding="utf-8"))
    assert saved["yc:https://x/jobs/1"]["company"] == "Acme"


def test_yc_role_mapping_defaults_to_eng():
    assert platforms._YC_ROLE.get("ai ml engineer") == "eng"
    assert platforms._YC_ROLE.get("backend engineer") == "eng"
    # unknown role falls back to "eng" in the service
    assert platforms._YC_ROLE.get("astronaut", "eng") == "eng"


def test_autoapply_rejects_unknown_platform():
    res = platform_apply.autoapply("myspace")
    assert res["ok"] is False
    assert "unknown" in res["message"].lower()


def test_caps_are_bounded():
    assert set(platform_apply._CAPS) == {"yc", "cutshort", "wellfound", "instahyre"}
    assert all(1 <= c <= 50 for c in platform_apply._CAPS.values())
