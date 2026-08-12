from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet

from app.saas.auth import SupabaseAuth
from app.saas.config import Settings, SettingsError
from app.saas.crypto import TokenCipher, TokenCipherError
from app.saas.errors import ApiError
from app.saas.store import SupabaseStore


def test_settings_validate_urls_and_never_publish_secrets() -> None:
    key = Fernet.generate_key().decode()
    settings = Settings.from_env(
        {
            "SUPABASE_URL": "https://project.supabase.co/",
            "SUPABASE_PUBLISHABLE_KEY": "public-key",
            "SUPABASE_SECRET_KEY": "server-secret",
            "SITE_URL": "https://app.example.test/",
            "TOKEN_ENCRYPTION_KEY": key,
            "GOOGLE_CLIENT_ID": "client",
            "GOOGLE_CLIENT_SECRET": "google-secret",
            "OAUTH_STATE_TTL_SECONDS": "300",
        }
    )
    assert settings.supabase_url == "https://project.supabase.co"
    assert settings.google_redirect_uri == "https://app.example.test/api/v1/oauth/google/callback"
    assert settings.oauth_state_ttl_seconds == 300
    assert settings.google_configured is True
    serialized = str(settings.public_config()) + repr(settings)
    assert "server-secret" not in serialized
    assert "google-secret" not in serialized
    assert key not in serialized

    with pytest.raises(SettingsError):
        Settings.from_env({"SUPABASE_URL": "http://project.supabase.co"})
    with pytest.raises(SettingsError):
        Settings.from_env({"OAUTH_STATE_TTL_SECONDS": "30"})


def test_gmail_readiness_distinguishes_platform_and_user_managed_oauth() -> None:
    key = Fernet.generate_key().decode()
    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-secret",
        site_url="https://app.example.test",
        token_encryption_key=key,
        google_redirect_uri="https://app.example.test/api/v1/oauth/google/callback",
    )

    assert settings.google_configured is False
    assert settings.google_byoc_ready is True
    assert settings.gmail_connection_available is True
    flags = settings.public_config()["feature_flags"]
    assert flags["gmail"] is True
    assert flags["gmail_platform_oauth"] is False
    assert flags["gmail_user_oauth_clients"] is True
    assert "server-secret" not in str(settings.public_config())


def test_connection_only_allowlist_does_not_enable_application_review_flag() -> None:
    connection_only = Settings(
        browserbase_api_key="browserbase-key",
        browserbase_project_id="project",
        allowed_browser_providers=("yc", "cutshort", "instahyre"),
    )
    with_form_flow = Settings(
        browserbase_api_key="browserbase-key",
        browserbase_project_id="project",
        allowed_browser_providers=("greenhouse", "yc"),
    )

    assert connection_only.public_config()["feature_flags"]["managed_browser"] is True
    assert (
        connection_only.public_config()["feature_flags"]["managed_application_review"]
        is False
    )
    assert (
        with_form_flow.public_config()["feature_flags"]["managed_application_review"]
        is True
    )


def test_token_cipher_round_trip_rotation_and_redaction() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_ciphertext = TokenCipher(old_key).encrypt("refresh-token-secret")
    rotating = TokenCipher(new_key, [old_key])
    assert rotating.decrypt(old_ciphertext) == "refresh-token-secret"
    new_ciphertext = rotating.rotate(old_ciphertext)
    assert TokenCipher(new_key).decrypt(new_ciphertext) == "refresh-token-secret"
    with pytest.raises(TokenCipherError) as error:
        TokenCipher(Fernet.generate_key().decode()).decrypt(new_ciphertext)
    assert "refresh-token-secret" not in str(error.value)


def test_supabase_auth_verifies_with_auth_server_and_redacts_token() -> None:
    token = "verified.jwt.value"

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/v1/user"
        assert request.headers["authorization"] == f"Bearer {token}"
        assert request.headers["apikey"] == "publishable"
        return httpx.Response(
            200,
            json={
                "id": "11111111-1111-4111-8111-111111111111",
                "email": "owner@example.test",
                "last_sign_in_at": "2026-08-09T10:30:00Z",
                "user_metadata": {"full_name": "Owner"},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            auth = SupabaseAuth(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="publishable",
                ),
                http_client=http,
            )
            user = await auth.authenticate(token)
            assert user.user_id == UUID("11111111-1111-4111-8111-111111111111")
            assert user.email == "owner@example.test"
            assert user.last_sign_in_at == datetime(2026, 8, 9, 10, 30, tzinfo=UTC)
            assert token not in repr(user)

    asyncio.run(run())


def test_supabase_auth_rejects_invalid_token_without_reflection() -> None:
    secret_token = "rejected.jwt.secret"

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, json={"message": secret_token})
            )
        ) as http:
            auth = SupabaseAuth(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="publishable",
                ),
                http_client=http,
            )
            with pytest.raises(ApiError) as error:
                await auth.authenticate(secret_token)
            assert error.value.code == "auth_invalid"
            assert secret_token not in error.value.message

    asyncio.run(run())


def test_store_uses_user_jwt_and_encodes_owned_filters() -> None:
    requests: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            client = SupabaseStore(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="publishable",
                    supabase_secret_key="server-secret",
                ),
                http_client=http,
            ).user("user-jwt")
            await client.fetch_many(
                "jobs",
                filters={
                    "user_id": "11111111-1111-4111-8111-111111111111",
                    "status": "neq.archived",
                },
                limit=25,
            )
            assert "user-jwt" not in repr(client)

    asyncio.run(run())
    request = requests[0]
    assert request.headers["authorization"] == "Bearer user-jwt"
    assert request.headers["apikey"] == "publishable"
    assert request.url.params["user_id"] == "eq.11111111-1111-4111-8111-111111111111"
    assert request.url.params["status"] == "eq.neq.archived"
    assert request.url.params["limit"] == "25"


def test_store_opaque_secret_uses_apikey_without_invalid_bearer_header() -> None:
    captured: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    async def run(secret_key: str) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
            client = SupabaseStore(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_secret_key=secret_key,
                ),
                http_client=http,
            ).secret()
            await client.rpc("consume_oauth_state", {})

    asyncio.run(run("sb_secret_opaque-server-key"))
    assert captured[0].headers["apikey"] == "sb_secret_opaque-server-key"
    assert "authorization" not in captured[0].headers

    captured.clear()
    legacy_jwt = "eyJhbGciOiJIUzI1NiJ9.legacy-service-role.signature"
    asyncio.run(run(legacy_jwt))
    assert captured[0].headers["apikey"] == legacy_jwt
    assert captured[0].headers["authorization"] == f"Bearer {legacy_jwt}"


def test_store_maps_known_database_errors_and_redacts_unknown_messages() -> None:
    async def known() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400, json={"message": "duplicate_recipient_window"}
                )
            )
        ) as http:
            client = SupabaseStore(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="public",
                ),
                http_client=http,
            ).user("jwt")
            with pytest.raises(ApiError) as error:
                await client.rpc("reserve_application_send", {})
            assert error.value.status_code == 409
            assert error.value.code == "duplicate_recipient_window"

    async def unknown() -> None:
        provider_secret = "raw database detail with secret-token"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(400, json={"message": provider_secret})
            )
        ) as http:
            client = SupabaseStore(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="public",
                ),
                http_client=http,
            ).user("jwt")
            with pytest.raises(ApiError) as error:
                await client.rpc("some_function", {})
            assert error.value.code == "data_invalid"
            assert provider_secret not in error.value.message

    asyncio.run(known())
    asyncio.run(unknown())


@pytest.mark.parametrize(
    ("database_message", "status_code", "public_code"),
    [
        ("form_revision_not_found", 404, "form_revision_not_found"),
        ("form_revision_stale", 409, "form_revision_stale"),
        ("form_approval_required", 409, "form_approval_required"),
        ("provider_connection_required", 409, "provider_connection_required"),
        ("application_already_submitted", 409, "application_already_submitted"),
        ("form_revision_limit_reached", 429, "form_revision_limit_reached"),
    ],
)
def test_store_maps_managed_application_races_to_actionable_errors(
    database_message: str, status_code: int, public_code: str
) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400, json={"message": database_message}
                )
            )
        ) as http:
            client = SupabaseStore(
                Settings(
                    supabase_url="https://project.supabase.co",
                    supabase_publishable_key="public",
                ),
                http_client=http,
            ).user("jwt")
            with pytest.raises(ApiError) as error:
                await client.rpc("managed_application_operation", {})
            assert error.value.status_code == status_code
            assert error.value.code == public_code

    asyncio.run(run())


def test_storage_rejects_path_traversal_before_network() -> None:
    async def run() -> None:
        client = SupabaseStore(
            Settings(
                supabase_url="https://project.supabase.co",
                supabase_publishable_key="public",
            )
        ).user("jwt")
        with pytest.raises(ValueError):
            await client.download_object("resumes", "owner/../victim.pdf")

    asyncio.run(run())
