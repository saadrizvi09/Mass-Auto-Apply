"""Contract tests for the hosted Next.js frontend.

The API remains FastAPI, while the browser is now a statically exported Next app.
These tests intentionally inspect source contracts rather than generated chunk names.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PAGE = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
CSS = (FRONTEND / "app" / "globals.css").read_text(encoding="utf-8")
API = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
CONFIG = (FRONTEND / "next.config.ts").read_text(encoding="utf-8")
PACKAGE = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
VERCEL = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))


def test_frontend_is_next_typescript_static_export() -> None:
    assert PACKAGE["dependencies"]["next"]
    assert PACKAGE["dependencies"]["react"]
    assert PACKAGE["dependencies"]["react-dom"]
    assert PACKAGE["scripts"]["build"] == "next build --webpack"
    assert "output: \"export\"" in CONFIG
    assert (FRONTEND / "app" / "page.tsx").exists()
    assert (FRONTEND / "app" / "layout.tsx").exists()
    assert (FRONTEND / "app" / "globals.css").exists()


def test_old_dom_frontend_entrypoints_are_removed() -> None:
    assert not (ROOT / "public" / "app.js").exists()
    assert not (ROOT / "public" / "styles.css").exists()
    assert not (ROOT / "public" / "vendor" / "supabase.js").exists()
    assert 'src="../public/app.js"' not in PAGE


def test_local_fastapi_serves_the_checked_in_next_export() -> None:
    source = (ROOT / "app" / "saas_main.py").read_text(encoding="utf-8")
    assert '@application.get("/assets/{asset_path:path}"' in source
    assert "PUBLIC_ASSET_DIR" in source
    assert "Asset not found" in source


def test_same_origin_api_and_supabase_auth_contract_is_preserved() -> None:
    assert 'const API_PREFIX = "/api/v1"' in API
    assert 'credentials: "same-origin"' in API
    assert 'Authorization' in API
    assert "refreshSession" in API
    assert "signInWithPassword" in PAGE
    assert "signInWithOAuth" in PAGE
    assert "detectSessionInUrl" in PAGE


def test_core_workspace_views_are_present() -> None:
    for marker in (
        "ProfileView",
        "DiscoveryView",
        "FormPilotView",
        "OutreachView",
        "JobsView",
        "ApplicationsView",
        "ConnectionsView",
        "ActivityView",
        "SettingsView",
    ):
        assert marker in PAGE


def test_compact_plain_language_workspace_refresh_is_present() -> None:
    assert "const simpleCopy" in PAGE
    assert "const simpleTitles" in PAGE
    assert "From résumé to application" in PAGE
    assert "function ServiceBadge" in PAGE
    assert "aa-use-cases" in PAGE
    assert "Compact workspace refresh" in CSS
    assert ".aa-service-logo" in CSS


def test_resume_upload_is_contained_and_uses_private_storage_registration() -> None:
    assert 'accept="application/pdf,.pdf"' in PAGE
    assert "client.storage.from(bucket).upload" in PAGE
    assert '"/resumes/register"' in PAGE
    assert "/parse" in PAGE
    assert "sha256" in PAGE
    assert "safe suggestions" in PAGE


def test_discovery_has_explicit_budget_and_timeout() -> None:
    assert 'max_jobs: Number(maxJobs)' in PAGE
    assert 'timeout_seconds: Number(timeout)' in PAGE
    assert 'max={50}' in PAGE
    assert 'max={120}' in PAGE
    assert '"/discovery/resume-guided"' in PAGE
    assert '"/discovery/public-feeds"' in PAGE
    assert '"/discovery/linkedin"' in PAGE


def test_outreach_preserves_strict_prompt_import_review_and_queue_flow() -> None:
    for marker in (
        "Generate research prompt",
        "distinct roles and contact emails",
        "Only roles with email leads",
        "Draft selected emails",
        "Choose completed CSV or XLSX",
        '"/outreach/research-prompt"',
        '"/discovery/import"',
            "/jobs/${encodeURIComponent(id)}/contacts/public",
            "/jobs/${encodeURIComponent(job.id)}/draft",
        '"/applications/send-batch"',
        "Find more public contacts",
    ):
        assert marker in PAGE
    assert "download" not in PAGE.lower()


def test_email_delivery_is_review_gated_and_persistent() -> None:
    assert 'draft.status !== "approved"' in PAGE
    assert 'draft.status === "approved"' in PAGE
    assert "/applications/${encodeURIComponent(approvalTarget.id)}/approve" in PAGE
    assert "/applications/${encodeURIComponent(draft.id)}/send" in PAGE
    assert "persistent Gmail worker" in PAGE
    assert "daily_send_cap" in PAGE


def test_vercel_serves_the_committed_next_export_with_the_fastapi_function() -> None:
    assert "buildCommand" not in VERCEL
    assert "outputDirectory" not in VERCEL
    assert VERCEL["functions"]["app/saas_main.py"]["maxDuration"] == 60


def test_layout_prevents_the_upload_viewport_regression() -> None:
    assert "overflow-x: hidden" in CSS
    assert ".aa-auth-form label { display: grid" in CSS
    assert ".aa-auth-form input { width: 100%" in CSS
    assert ".aa-dropzone input" in CSS
    assert "clip: rect(0 0 0 0)" in CSS
    assert ".aa-import-card" in CSS
    assert "grid-template-columns: minmax(0, 1fr) auto" in CSS
    assert ".aa-main" in CSS
    assert ".aa-content" in CSS
