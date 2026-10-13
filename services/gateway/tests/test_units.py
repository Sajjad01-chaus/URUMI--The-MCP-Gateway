import pytest
from mcp import types

from gateway.catalog import Catalog, ToolUnavailable, UpstreamState, default_min_role, exposed_name
from gateway.security import CredentialBox, generate_api_key, hash_token
from gateway.ssrf import UnsafeUrlError, check_upstream_url
from gateway.upstream import Target, auth_headers

INTERNAL = ["*.store-*.svc.cluster.local", "*-mcp.platform.svc.cluster.local"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8000/mcp",
        "http://localhost/mcp",
        "http://10.43.0.1/mcp",
        "http://192.168.1.10/mcp",
        "ftp://example.com/mcp",
        "http://user:pass@example.com/mcp",
        "http://[::1]/mcp",
    ],
)
async def test_ssrf_guard_blocks_dangerous_urls(url):
    with pytest.raises(UnsafeUrlError):
        await check_upstream_url(url, INTERNAL)


async def test_ssrf_guard_allows_public_ip():
    await check_upstream_url("https://93.184.216.34/mcp", INTERNAL)


async def test_allow_list_never_unlocks_loopback():
    with pytest.raises(UnsafeUrlError):
        await check_upstream_url("http://localhost/mcp", ["localhost"])


def test_exposed_names_are_prefixed_and_sanitised():
    assert exposed_name("woo", "list_orders") == "woo__list_orders"
    assert exposed_name("x", "weird.name/v2") == "x__weird_name_v2"
    assert len(exposed_name("s", "a" * 100)) == 64


def test_default_role_comes_from_read_only_annotation():
    read = types.Tool(name="r", input_schema={"type": "object"}, annotations=types.ToolAnnotations(read_only_hint=True))
    write = types.Tool(name="w", input_schema={"type": "object"}, annotations=types.ToolAnnotations(read_only_hint=False))
    bare = types.Tool(name="b", input_schema={"type": "object"})
    assert (default_min_role(read), default_min_role(write), default_min_role(bare)) == ("member", "admin", "admin")


def test_resolve_explains_why_a_tool_is_unavailable():
    import uuid

    catalog = Catalog()
    tool = types.Tool(name="w", input_schema={"type": "object"})
    catalog.upsert(UpstreamState(uuid.uuid4(), "s", "S", "http://x", True, "none", None, None, 5, status="connected", tools=[tool]))
    with pytest.raises(ToolUnavailable, match="role"):
        catalog.resolve("s__w", "member")
    assert catalog.resolve("s__w", "admin")[1].name == "w"
    catalog.servers["s"].status = "unreachable"
    with pytest.raises(ToolUnavailable, match="unreachable") as exc:
        catalog.resolve("s__w", "admin")
    assert exc.value.outcome == "unavailable"
    with pytest.raises(ToolUnavailable, match="Unknown"):
        catalog.resolve("nope__w", "admin")


def test_api_keys_are_random_and_only_hash_is_kept():
    key, prefix, key_hash = generate_api_key()
    assert key.startswith("mcpgw_") and key.startswith(prefix) and key_hash == hash_token(key) and key not in key_hash
    assert generate_api_key()[0] != key


def test_credentials_encrypted_at_rest_and_rotatable():
    from cryptography.fernet import Fernet

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = CredentialBox(old).encrypt("s3cret")
    assert b"s3cret" not in token
    assert CredentialBox(f"{new},{old}").decrypt(token) == "s3cret"


def test_gateway_sends_its_own_credential_never_the_callers():
    assert auth_headers(Target("n", "u", "bearer", None, "tok", 5)) == {"Authorization": "Bearer tok"}
    assert auth_headers(Target("f", "u", "header", "X-API-Key", "k", 5)) == {"X-API-Key": "k"}
    assert auth_headers(Target("p", "u", "none", None, None, 5)) == {}
