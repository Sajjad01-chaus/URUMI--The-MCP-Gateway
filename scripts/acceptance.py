"""Phase 7 acceptance run: the design §15 demo script as code, plus the unauthorized cases.

Runs against a freshly installed local cluster (scripts/up.sh). Step 14 (NetworkPolicies) is
scripts/netpol-test.sh; scripts/acceptance.sh runs both. By-hand demo steps (storefront purchase
in a browser, Claude Code as bob) are replaced by the same API calls those clients make.

Usage: uv run --project services/gateway python scripts/acceptance.py [domain]
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
# URUMI_ENV=prod (set by acceptance.sh --prod) switches to HTTPS and the prod secrets on the VM.
ENV = os.environ.get("URUMI_ENV", "local")
SECRETS = ROOT / "deploy" / "secrets" / f"{ENV}.secrets.yaml"
DEMO_USERS = ROOT / "deploy" / "secrets" / f"{ENV}.demo-users.yaml"
DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "127-0-0-1.nip.io"
SCHEME = "https" if ENV == "prod" else "http"
APP, MCP_URL, STORE = f"{SCHEME}://app.{DOMAIN}", f"{SCHEME}://mcp.{DOMAIN}/mcp", f"{SCHEME}://store.{DOMAIN}"
ALICE, BOB = "alice@urumi.test", "bob@urumi.test"
XRW = {"X-Requested-With": "acceptance"}
PRODUCT = "Urumi Mug"


class AcceptanceError(Exception):
    """A precondition failed, so the remaining checks of a step can't run."""


@dataclass
class Report:
    rows: list[tuple[str, str, bool, str]] = field(default_factory=list)
    step: str = ""

    def check(self, label: str, ok: bool, detail: Any = "") -> bool:
        self.rows.append((self.step, label, ok, str(detail)))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}  {str(detail)[:160]}")
        return ok

    def failed(self) -> list[tuple[str, str, bool, str]]:
        return [r for r in self.rows if not r[2]]


@dataclass
class World:
    """State the steps hand to each other."""

    admin: httpx2.AsyncClient
    alice: httpx2.AsyncClient
    bob: httpx2.AsyncClient
    alice_key: str = ""
    bob_key: str = ""
    bob_key_id: str = ""
    first_order: int = 0
    mug_order: int = 0
    server_ids: dict[str, str] = field(default_factory=dict)


def secret_value(text: str, key: str) -> str:
    match = re.search(rf'^\s*{key}:\s*"?([^"\r\n]+)', text, re.MULTILINE)
    if not match:
        raise AcceptanceError(f"'{key}' missing in {SECRETS.name}")
    return match.group(1).strip()


def demo_passwords() -> dict[str, str]:
    """alice/bob passwords live in a gitignored file, created once, never printed."""
    if not DEMO_USERS.exists():
        lines = ["# Acceptance/demo users on the local cluster only. DO NOT COMMIT."]
        lines += [f'{email}: "{secrets.token_urlsafe(18)}"' for email in (ALICE, BOB)]
        DEMO_USERS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    text = DEMO_USERS.read_text(encoding="utf-8")
    return {email: secret_value(text, re.escape(email)) for email in (ALICE, BOB)}


def kubectl(*args: str, timeout: int = 300) -> str:
    done = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise AcceptanceError(f"kubectl {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


async def rollout_settled(deployment: str, namespace: str = "platform", seconds: int = 120) -> None:
    """`rollout status` returns once old pods are only *marked* for deletion; wait until they are gone,
    so traffic can no longer reach the previous version (build-log #37)."""
    kubectl("-n", namespace, "rollout", "status", f"deploy/{deployment}", f"--timeout={seconds}s", timeout=seconds + 10)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pods = kubectl("-n", namespace, "get", "pods", "-l", f"app.kubernetes.io/name={deployment}",
                       "-o", "jsonpath={range .items[*]}{.metadata.deletionTimestamp}{' '}{end}")
        if not pods.strip():
            return
        await asyncio.sleep(2)
    raise AcceptanceError(f"old {deployment} pods still terminating after {seconds} s")


@asynccontextmanager
async def mcp_session(key: str, mode: str = "auto", handler: Callable[..., Awaitable[None]] | None = None) -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as http,
        Client(streamable_http_client(MCP_URL, http_client=http), mode=mode, message_handler=handler) as client,
    ):
        yield client


async def tool_names(client: Client) -> set[str]:
    return {t.name for t in (await client.list_tools()).tools}


async def call(key: str, tool: str, args: dict[str, Any]) -> tuple[types.CallToolResult, float]:
    async with mcp_session(key) as client:
        start = time.perf_counter()
        result = await client.call_tool(tool, args)
        return result, time.perf_counter() - start


def text_of(result: types.CallToolResult) -> str:
    return result.content[0].text if result.content else ""


async def raw_mcp_status(token: str | None) -> httpx2.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "woo__list_orders", "arguments": {}}}
    async with httpx2.AsyncClient(timeout=30) as http:
        return await http.post(MCP_URL, json=body, headers=headers)


async def store_checkout(product_name: str) -> int:
    """Guest cart + Cash-on-Delivery checkout through the Store API (what the checkout page calls)."""
    base = f"{STORE}/wp-json/wc/store/v1"
    async with httpx2.AsyncClient(timeout=60) as http:
        products = (await http.get(f"{base}/products", params={"search": product_name})).json()
        product = next((p for p in products if p["name"] == product_name), None)
        if product is None:
            raise AcceptanceError(f"product '{product_name}' not on the storefront")
        cart = await http.get(f"{base}/cart")
        headers = {"Cart-Token": cart.headers["Cart-Token"], "Nonce": cart.headers["Nonce"]}
        (await http.post(f"{base}/cart/add-item", json={"id": product["id"], "quantity": 1}, headers=headers)).raise_for_status()
        address = {
            "first_name": "Demo", "last_name": "Shopper", "address_1": "1 Demo Street", "city": "Pune", "state": "MH",
            "postcode": "411001", "country": "IN", "email": "shopper@urumi.test", "phone": "9000000000",
        }
        response = await http.post(
            f"{base}/checkout",
            json={"billing_address": address, "shipping_address": address, "payment_method": "cod"},
            headers=headers,
        )
        if response.status_code != 200:
            raise AcceptanceError(f"checkout failed: HTTP {response.status_code} {response.text[:200]}")
        return int(response.json()["order_id"])


async def chat(client: httpx2.AsyncClient, prompt: str) -> dict[str, Any]:
    response = await client.post("/api/chat", json={"messages": [{"role": "user", "content": prompt}]}, headers=XRW, timeout=150)
    if response.status_code != 200:
        raise AcceptanceError(f"chat HTTP {response.status_code}: {response.text[:200]}")
    body = response.json()
    for c in body["tool_calls"]:
        print(f"      tool {c['name']} {json.dumps(c['arguments'])[:90]} -> {'ERROR' if c['is_error'] else 'ok'} {c['duration_ms']} ms")
    print(f"      answer: {' '.join(body['answer'].split())[:200]}")
    return body


def successful(body: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    return [c for c in body["tool_calls"] if c["name"] == tool and not c["is_error"]]


async def servers(client: httpx2.AsyncClient) -> dict[str, dict[str, Any]]:
    return {s["slug"]: s for s in (await client.get("/api/servers")).json()}


async def wait_status(world: World, slug: str, want: str, seconds: int) -> tuple[bool, float, dict[str, Any]]:
    start = time.monotonic()
    while True:
        server = (await servers(world.alice))[slug]
        if server["status"] == want or time.monotonic() - start > seconds:
            return server["status"] == want, time.monotonic() - start, server
        await asyncio.sleep(2)


async def login(client: httpx2.AsyncClient, email: str, password: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    if response.status_code != 200:
        raise AcceptanceError(f"login {email}: HTTP {response.status_code}")


async def ensure_user(world: World, inviter: httpx2.AsyncClient, client: httpx2.AsyncClient, email: str, role: str, password: str, name: str, r: Report) -> None:
    """Invite → accept (fresh cluster). On a re-run the user already exists, so just log in."""
    invite = await inviter.post("/api/invites", json={"email": email, "role": role}, headers=XRW)
    if invite.status_code == 409:
        await login(client, email, password)
        r.check(f"{email} already exists (re-run) -> logged in", True)
        return
    r.check(f"invite {email} as {role}", invite.status_code == 201 and invite.json()["link"].startswith(f"{APP}/invite/"), invite.status_code)
    token = invite.json()["link"].rsplit("/", 1)[1]
    accepted = await client.post(f"/api/invites/{token}/accept", json={"name": name, "password": password})
    r.check(f"{email} accepts -> {role}, logged in", accepted.status_code == 201 and accepted.json()["role"] == role, accepted.status_code)


async def new_key(client: httpx2.AsyncClient, name: str) -> tuple[str, str]:
    created = await client.post("/api/keys", json={"name": name}, headers=XRW)
    if created.status_code != 201:
        raise AcceptanceError(f"create key: HTTP {created.status_code}")
    return created.json()["key"], created.json()["id"]


# ---------------------------------------------------------------- the §15 steps

async def setup(w: World, r: Report) -> None:
    text = SECRETS.read_text(encoding="utf-8")
    await login(w.admin, secret_value(text, "bootstrapAdminEmail"), secret_value(text, "bootstrapAdminPassword"))
    r.check("bootstrap admin login", True)
    passwords = demo_passwords()
    await ensure_user(w, w.admin, w.alice, ALICE, "admin", passwords[ALICE], "Alice", r)
    w.alice_key, _ = await new_key(w.alice, "acceptance-alice")
    r.check("alice created an API key (dashboard session)", w.alice_key.startswith("mcpgw_"), w.alice_key[:12] + "…")


async def step1_store(w: World, r: Report) -> None:
    for path in ("/", "/shop/", "/cart/"):
        async with httpx2.AsyncClient(timeout=30) as http:
            code = (await http.get(f"{STORE}{path}")).status_code
        r.check(f"storefront {path}", code == 200, code)
    w.first_order = await store_checkout("Kubernetes Mug")
    result, _ = await call(w.alice_key, "woo__get_order", {"order_id": w.first_order})
    order = result.structured_content or {}
    r.check("COD checkout -> order in WooCommerce (processing, COD)", not result.is_error and order.get("status") == "processing"
            and order.get("payment_method") == "Cash on delivery", f"order #{w.first_order} {order.get('total')} {order.get('status')}")


async def step2_servers(w: World, r: Report) -> None:
    found = await servers(w.alice)
    w.server_ids = {slug: s["id"] for slug, s in found.items()}
    summary = {slug: (s["status"], len(s["tools"])) for slug, s in found.items()}
    r.check("alice: woo, notes, fx Connected with 6/4/3 tools", summary == {"fx": ("connected", 3), "notes": ("connected", 4), "woo": ("connected", 6)}, summary)


async def step3_chat_create(w: World, r: Report) -> None:
    body = await chat(w.alice, f"Create a product called {PRODUCT} priced 12.50")
    calls = successful(body, "woo__create_product")
    r.check("chat called woo__create_product", bool(calls), [c["name"] for c in body["tool_calls"]])
    if calls:
        product = json.loads(calls[-1]["result"])
        r.check("product created at 12.50 and published", product["regular_price"] == "12.50" and product["status"] == "publish", product["id"])
        async with httpx2.AsyncClient(timeout=30, follow_redirects=True) as http:
            code = (await http.get(product["permalink"])).status_code
        r.check("product page live on the storefront", code == 200, f"{product['permalink']} -> {code}")


async def step4_buy(w: World, r: Report) -> None:
    w.mug_order = await store_checkout(PRODUCT)
    r.check(f"bought {PRODUCT} on the storefront", w.mug_order > w.first_order, f"order #{w.mug_order}")


async def step5_chat_orders(w: World, r: Report) -> None:
    await asyncio.sleep(5)
    body = await chat(w.alice, "What orders came in today?")
    calls = [c for c in successful(body, "woo__list_orders") if (c["arguments"] or {}).get("period") == "today"]
    r.check("chat called woo__list_orders {period: today}", bool(calls), [(c["name"], c["arguments"]) for c in body["tool_calls"]])
    r.check("today's orders include the Urumi Mug order", any(f'"id": {w.mug_order}' in c["result"] for c in calls), f"#{w.mug_order}")


async def step6_invite_bob(w: World, r: Report) -> None:
    await ensure_user(w, w.alice, w.bob, BOB, "member", demo_passwords()[BOB], "Bob", r)
    w.bob_key, w.bob_key_id = await new_key(w.bob, "bob-claude-code")
    r.check("bob created an API key", w.bob_key.startswith("mcpgw_"), w.bob_key[:12] + "…")


async def step7_bob_client(w: World, r: Report) -> None:
    async with mcp_session(w.bob_key) as bob:
        names = await tool_names(bob)
        prefixes = {n.split("__")[0] for n in names}
        r.check("bob's MCP client sees woo__*, notes__*, fx__* (11 tools)", prefixes == {"woo", "notes", "fx"} and len(names) == 11, len(names))
        result = await bob.call_tool("woo__list_orders", {"period": "today"})
        ids = [o["id"] for o in (result.structured_content or {}).get("orders", [])]
        r.check("bob: today's orders", not result.is_error and w.mug_order in ids, ids)


async def step8_cross_server(w: World, r: Report) -> None:
    await asyncio.sleep(5)
    body = await chat(w.bob, "Summarise today's orders, convert the total revenue to EUR and save the summary as a note")
    for tool in ("woo__list_orders", "fx__convert_currency", "notes__create_note"):
        r.check(f"cross-server: {tool}", bool(successful(body, tool)))


async def step9_revoke(w: World, r: Report) -> None:
    revoked = await w.alice.delete(f"/api/keys/{w.bob_key_id}", headers=XRW)
    r.check("alice revokes bob's key", revoked.status_code == 204, revoked.status_code)
    response = await raw_mcp_status(w.bob_key)
    r.check("bob's next call -> 401", response.status_code == 401, response.status_code)
    w.bob_key, w.bob_key_id = await new_key(w.bob, "bob-after-revoke")
    r.check("bob creates a new key for the next steps", w.bob_key.startswith("mcpgw_"))


async def step10_disable(w: World, r: Report) -> None:
    fx_id = w.server_ids["fx"]
    pushed = asyncio.Event()

    async def on_message(message: Any) -> None:
        if isinstance(message, types.ToolListChangedNotification):
            pushed.set()

    async with mcp_session(w.bob_key, mode="legacy", handler=on_message) as legacy, mcp_session(w.bob_key) as modern:
        await tool_names(legacy)
        await asyncio.sleep(0.5)
        async with modern.listen(tools_list_changed=True) as subscription:
            response = await w.alice.patch(f"/api/servers/{fx_id}", json={"enabled": False}, headers=XRW)
            r.check("alice disables fx", response.status_code == 200 and response.json()["status"] == "disabled", response.status_code)
            try:
                await asyncio.wait_for(anext(aiter(subscription)), timeout=10)
                r.check("list_changed pushed (2026-07-28 protocol)", True)
            except TimeoutError:
                r.check("list_changed pushed (2026-07-28 protocol)", False, "no notification in 10 s")
        try:
            await asyncio.wait_for(pushed.wait(), timeout=10)
            r.check("list_changed pushed (2025-11-25 session protocol)", True)
        except TimeoutError:
            r.check("list_changed pushed (2025-11-25 session protocol)", False, "no notification in 10 s")
        r.check("fx tools gone after re-list (bob)", not any(n.startswith("fx__") for n in await tool_names(legacy)))
        chat_tools = {t["exposed_name"] for t in (await w.alice.get("/api/tools")).json()}
        r.check("fx tools gone from the chat tool list (alice)", not any(n.startswith("fx__") for n in chat_tools), len(chat_tools))
        blocked = await legacy.call_tool("fx__convert_currency", {"amount": 1, "from_currency": "USD", "to_currency": "EUR"})
        r.check("direct call to fx rejected", blocked.is_error and "disabled" in text_of(blocked), text_of(blocked))
    response = await w.alice.patch(f"/api/servers/{fx_id}", json={"enabled": True}, headers=XRW)
    r.check("alice re-enables fx -> Connected", response.status_code == 200 and response.json()["status"] == "connected", response.json().get("status"))


async def step11_kill(w: World, r: Report) -> None:
    kubectl("-n", "platform", "scale", "deploy/notes-mcp", "--replicas=0")
    try:
        ok, waited, server = await wait_status(w, "notes", "unreachable", 90)
        r.check("notes Unreachable with a reason", ok and bool(server["last_error"]), f"{waited:.0f} s: {server['last_error']}")
        result, seconds = await call(w.bob_key, "woo__get_store_info", {})
        r.check("woo still works", not result.is_error, f"{seconds * 1000:.0f} ms")
        result, seconds = await call(w.bob_key, "notes__list_notes", {})
        r.check("notes call fails fast and clearly", result.is_error and seconds < 3, f"{seconds * 1000:.0f} ms: {text_of(result)}")
    finally:
        kubectl("-n", "platform", "scale", "deploy/notes-mcp", "--replicas=1")
    await rollout_settled("notes-mcp")
    ok, waited, _ = await wait_status(w, "notes", "connected", 120)
    r.check("notes back to Connected", ok, f"{waited:.0f} s after scale-up")


async def step12_slow(w: World, r: Report) -> None:
    kubectl("-n", "platform", "set", "env", "deploy/notes-mcp", "FAULT_DELAY_MS=30000")
    try:
        await rollout_settled("notes-mcp")
        ok, _, _ = await wait_status(w, "notes", "connected", 120)
        r.check("slow notes pod Connected (health checks don't call tools)", ok)

        async def woo_while_waiting() -> list[float]:
            await asyncio.sleep(2)
            timings = []
            for _ in range(3):
                result, seconds = await call(w.bob_key, "woo__get_store_info", {})
                timings.append(seconds if not result.is_error else 99.0)
                await asyncio.sleep(2)
            return timings

        (slow, slow_seconds), woo_timings = await asyncio.gather(call(w.bob_key, "notes__list_notes", {}), woo_while_waiting())
        r.check("notes call cut at ~20 s with a clear timeout", slow.is_error and "did not answer within 20s" in text_of(slow)
                and 19 < slow_seconds < 26, f"{slow_seconds:.1f} s: {text_of(slow)[:90]}")
        r.check("woo stays fast meanwhile", all(t < 2 for t in woo_timings), [f"{t * 1000:.0f} ms" for t in woo_timings])
    finally:
        kubectl("-n", "platform", "set", "env", "deploy/notes-mcp", "FAULT_DELAY_MS-")
    await rollout_settled("notes-mcp")
    ok, _, _ = await wait_status(w, "notes", "connected", 120)
    result, seconds = await call(w.bob_key, "notes__list_notes", {})
    r.check("delay removed: notes fast again", ok and not result.is_error and seconds < 2, f"{seconds * 1000:.0f} ms")


async def step13_permissions(w: World, r: Report) -> None:
    async with mcp_session(w.bob_key) as bob:
        names = await tool_names(bob)
        r.check("bob doesn't see woo__update_order_status / woo__create_product",
                not {"woo__update_order_status", "woo__create_product"} & names)
        result = await bob.call_tool("woo__update_order_status", {"order_id": w.mug_order, "status": "cancelled"})
        r.check("calling it by name -> denied", result.is_error and "not allowed" in text_of(result), text_of(result))
    order, _ = await call(w.alice_key, "woo__get_order", {"order_id": w.mug_order})
    r.check("order untouched", (order.structured_content or {}).get("status") == "processing", (order.structured_content or {}).get("status"))


async def unauthorized(w: World, r: Report) -> None:
    response = await raw_mcp_status(None)
    r.check("no key -> 401 with header hint", response.status_code == 401 and "Bearer" in response.headers.get("www-authenticate", ""), response.status_code)
    response = await raw_mcp_status("mcpgw_not-a-real-key")
    r.check("unknown key -> 401", response.status_code == 401, response.status_code)
    r.check("member can't list members -> 403", (await w.bob.get("/api/members")).status_code == 403)
    created = await w.bob.post("/api/servers", json={"name": "x", "slug": "x", "url": "https://example.com/mcp"}, headers=XRW)
    r.check("member can't register servers -> 403", created.status_code == 403, created.status_code)
    no_csrf = await w.alice.post("/api/keys", json={"name": "csrf"})
    r.check("cookie write without X-Requested-With -> 403", no_csrf.status_code == 403, no_csrf.status_code)
    ssrf = await w.alice.post("/api/servers", json={"name": "m", "slug": "meta", "url": "http://169.254.169.254/"}, headers=XRW)
    r.check("SSRF: cloud metadata URL -> 422", ssrf.status_code == 422, ssrf.json().get("detail"))


async def step15_audit(w: World, r: Report) -> None:
    rows = (await w.alice.get("/api/audit", params={"limit": 200})).json()["items"]

    def has(user: str, tool: str, outcome: str, source: str | None = None, error: str = "") -> bool:
        return any(x["user_email"] == user and x["exposed_name"] == tool and x["outcome"] == outcome
                   and (source is None or x["source"] == source) and error in (x["error_message"] or "") for x in rows)

    expected = [
        ("alice chat create_product", has(ALICE, "woo__create_product", "success", "chat")),
        ("alice chat list_orders", has(ALICE, "woo__list_orders", "success", "chat")),
        ("bob MCP list_orders", has(BOB, "woo__list_orders", "success", "mcp")),
        ("bob chat fx + notes", has(BOB, "fx__convert_currency", "success", "chat") and has(BOB, "notes__create_note", "success", "chat")),
        ("bob revoked key -> denied", has(BOB, "woo__list_orders", "denied", error="API key revoked")),
        ("bob call to disabled fx", has(BOB, "fx__convert_currency", "denied", error="disabled")),
        ("bob notes while killed -> unavailable", has(BOB, "notes__list_notes", "unavailable")),
        ("bob notes while slow -> timeout", has(BOB, "notes__list_notes", "timeout")),
        ("bob update_order_status -> denied", has(BOB, "woo__update_order_status", "denied")),
    ]
    for label, ok in expected:
        r.check(f"audit: {label}", ok)
    print("\n      time      user                tool                      outcome      ms  source")
    for x in reversed([x for x in rows if x["user_email"] in (ALICE, BOB)][:25]):
        print(f"      {x['ts'][11:19]}  {x['user_email']:<18}  {x['exposed_name']:<24}  {x['outcome']:<11} {x['duration_ms']:>5}  {x['source']}")


STEPS: list[tuple[str, Callable[[World, Report], Awaitable[None]]]] = [
    ("setup: admin login, alice (admin) via invite", setup),
    ("1  store: storefront + COD checkout", step1_store),
    ("2  dashboard: servers Connected", step2_servers),
    ("3  chat (alice): create Urumi Mug", step3_chat_create),
    ("4  buy Urumi Mug on the storefront", step4_buy),
    ("5  chat (alice): orders today", step5_chat_orders),
    ("6  members: invite bob, bob creates a key", step6_invite_bob),
    ("7  bob's MCP client (stands in for Claude Code)", step7_bob_client),
    ("8  cross-server: woo + fx + notes", step8_cross_server),
    ("9  revoke bob's key", step9_revoke),
    ("10 disable fx", step10_disable),
    ("11 kill notes", step11_kill),
    ("12 slow notes", step12_slow),
    ("13 permissions", step13_permissions),
    ("unauthorized cases", unauthorized),
    ("15 audit", step15_audit),
]


async def main() -> int:
    report = Report()
    started = time.monotonic()
    async with (
        httpx2.AsyncClient(base_url=APP, timeout=60) as admin,
        httpx2.AsyncClient(base_url=APP, timeout=60) as alice,
        httpx2.AsyncClient(base_url=APP, timeout=60) as bob,
    ):
        world = World(admin=admin, alice=alice, bob=bob)
        for name, step in STEPS:
            report.step = name
            print(f"\n=== {name}")
            try:
                await step(world, report)
            except (AcceptanceError, httpx2.HTTPError, KeyError, ValueError, subprocess.TimeoutExpired) as exc:
                report.check("step aborted", False, f"{type(exc).__name__}: {exc}")
    failed = report.failed()
    print(f"\n{len(report.rows) - len(failed)}/{len(report.rows)} checks passed in {time.monotonic() - started:.0f} s")
    for step, label, _, detail in failed:
        print(f"  FAILED [{step}] {label}: {detail[:200]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
