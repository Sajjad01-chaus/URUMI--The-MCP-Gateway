import asyncio
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mcp import types

SEPARATOR = "__"
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
ROLE_RANK = {"member": 1, "admin": 2}
TOKEN_RE = re.compile(r"[a-z0-9]+")


class ToolUnavailable(Exception):
    """A tools/call can't be routed; `outcome` is what the audit log records."""

    def __init__(self, message: str, outcome: str, slug: str = "", tool: str = ""):
        super().__init__(message)
        self.outcome = outcome
        self.slug = slug
        self.tool = tool


@dataclass
class UpstreamState:
    """In-memory view of one registered upstream: config, health and its last known tools."""

    id: uuid.UUID
    slug: str
    name: str
    url: str
    enabled: bool
    auth_type: str
    auth_header_name: str | None
    credential_encrypted: bytes | None
    timeout_seconds: int
    is_managed: bool = False
    managed_kind: str | None = None
    version: str = ""
    status: str = "unknown"
    status_since: datetime | None = None
    last_error: str | None = None
    last_checked_at: datetime | None = None
    consecutive_failures: int = 0
    tools: list[types.Tool] = field(default_factory=list)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(10))

    @property
    def display_status(self) -> str:
        return "disabled" if not self.enabled else self.status

    @property
    def is_routable(self) -> bool:
        return self.enabled and self.status == "connected"


def exposed_name(slug: str, tool_name: str) -> str:
    """`<slug>__<tool>`: always prefixed, so names never collide and show which server they hit."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name)
    return f"{slug}{SEPARATOR}{safe}"[:64]


def default_min_role(tool: types.Tool) -> str:
    """Read-only tools (per upstream annotations) are open to members; anything else is admin-only."""
    annotations = tool.annotations
    return "member" if annotations is not None and annotations.read_only_hint is True else "admin"


class Catalog:
    """Aggregated tool catalog. Served from memory so a slow/dead upstream never blocks tools/list."""

    def __init__(self, max_concurrent_calls: int = 10):
        self.servers: dict[str, UpstreamState] = {}
        self.policies: dict[tuple[str, str], str] = {}
        self.max_concurrent_calls = max_concurrent_calls

    def upsert(self, state: UpstreamState) -> None:
        previous = self.servers.get(state.slug)
        if previous is not None:
            state.status, state.tools = previous.status, previous.tools
            state.version = previous.version
            state.status_since = previous.status_since
            state.last_error, state.last_checked_at = previous.last_error, previous.last_checked_at
            state.consecutive_failures, state.semaphore = previous.consecutive_failures, previous.semaphore
        else:
            state.semaphore = asyncio.Semaphore(self.max_concurrent_calls)
        self.servers[state.slug] = state

    def remove(self, slug: str) -> None:
        self.servers.pop(slug, None)
        self.policies = {k: v for k, v in self.policies.items() if k[0] != slug}

    def min_role(self, slug: str, tool: types.Tool) -> str:
        return self.policies.get((slug, tool.name), default_min_role(tool))

    def allowed(self, role: str, slug: str, tool: types.Tool) -> bool:
        return ROLE_RANK.get(role, 0) >= ROLE_RANK[self.min_role(slug, tool)]

    def visible_tools(self, role: str) -> list[types.Tool]:
        result = []
        for state in sorted(self.servers.values(), key=lambda s: s.slug):
            if not state.is_routable:
                continue
            for tool in state.tools:
                if self.allowed(role, state.slug, tool):
                    result.append(
                        tool.model_copy(
                            update={
                                "name": exposed_name(state.slug, tool.name),
                                "description": f"[{state.name}] {tool.description or ''}".strip(),
                                "meta": None,
                            }
                        )
                    )
        return result

    def search_tools(self, role: str, query: str, server: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Rank visible tools with BM25 over names, descriptions and parameter names."""
        query_terms = set(TOKEN_RE.findall(query.lower()))
        if not query_terms:
            return []
        documents: list[tuple[UpstreamState, types.Tool, list[str]]] = []
        for state in sorted(self.servers.values(), key=lambda item: item.slug):
            if not state.is_routable or (server and state.slug != server):
                continue
            for tool in state.tools:
                if self.allowed(role, state.slug, tool):
                    properties = tool.input_schema.get("properties", {})
                    text = " ".join((state.slug, tool.name, tool.description or "", " ".join(properties)))
                    documents.append((state, tool, TOKEN_RE.findall(text.lower())))
        if not documents:
            return []
        average_length = sum(len(tokens) for _, _, tokens in documents) / len(documents)
        frequencies = {term: sum(term in tokens for _, _, tokens in documents) for term in query_terms}

        def score(tokens: list[str]) -> float:
            counts = Counter(tokens)
            length_factor = 1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1)
            total = 0.0
            for term in query_terms:
                frequency = counts[term]
                if not frequency:
                    continue
                inverse = math.log(1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                total += inverse * frequency * 2.2 / (frequency + 1.2 * length_factor)
            return total

        ranked = sorted(
            ((score(tokens), state, tool) for state, tool, tokens in documents),
            key=lambda item: (-item[0], exposed_name(item[1].slug, item[2].name)),
        )
        return [
            {
                "name": exposed_name(state.slug, tool.name),
                "server": state.slug,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }
            for relevance, state, tool in ranked[:limit]
            if relevance > 0
        ]

    def visibility_signature(self) -> frozenset[tuple[str, str]]:
        """Changes whenever the set of routable tools changes (used to decide when to notify clients)."""
        return frozenset(
            (s.slug, t.name) for s in self.servers.values() if s.is_routable for t in s.tools
        )

    def resolve(self, name: str, role: str) -> tuple[UpstreamState, types.Tool]:
        slug, sep, _ = name.partition(SEPARATOR)
        state = self.servers.get(slug) if sep else None
        if state is None:
            raise ToolUnavailable(f"Unknown tool '{name}'", "denied", slug=slug)
        tool = next((t for t in state.tools if exposed_name(slug, t.name) == name), None)
        if tool is None:
            raise ToolUnavailable(f"Unknown tool '{name}'", "denied", slug=slug)
        if not state.enabled:
            raise ToolUnavailable(f"Server '{slug}' is disabled", "denied", slug, tool.name)
        if not self.allowed(role, slug, tool):
            raise ToolUnavailable(f"Your role is not allowed to call '{name}'", "denied", slug, tool.name)
        if state.status != "connected":
            reason = f": {state.last_error}" if state.last_error else ""
            raise ToolUnavailable(f"Server '{slug}' is unreachable{reason}", "unavailable", slug, tool.name)
        return state, tool
