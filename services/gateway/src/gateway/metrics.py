from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30)


class GatewayMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.tool_calls = Counter(
            "urumi_tool_calls_total",
            "Gateway tool calls.",
            ("server", "tool", "outcome", "source"),
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "urumi_tool_call_duration_seconds",
            "Gateway tool-call duration.",
            ("server", "outcome"),
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.upstream_up = Gauge(
            "urumi_upstream_up",
            "Whether the latest upstream health check succeeded.",
            ("server",),
            registry=self.registry,
        )
        self.health_checks = Counter(
            "urumi_upstream_health_checks_total",
            "Upstream MCP health checks.",
            ("server", "result"),
            registry=self.registry,
        )
        self.sessions = Gauge(
            "urumi_mcp_sessions",
            "Remembered MCP sessions.",
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "urumi_rate_limited_total",
            "Requests rejected by a configured rate limit.",
            ("scope",),
            registry=self.registry,
        )

    def render(self, session_count: int) -> bytes:
        self.sessions.set(session_count)
        return generate_latest(self.registry)


class ProvisionerMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.total = Counter(
            "urumi_provisioning_total",
            "Managed-release operations.",
            ("kind", "result"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "urumi_provisioning_duration_seconds",
            "Managed-release operation duration.",
            ("kind",),
            buckets=(1, 5, 10, 30, 60, 120, 300, 600, 900),
            registry=self.registry,
        )
