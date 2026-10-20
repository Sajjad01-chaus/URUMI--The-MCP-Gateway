# Gateway package and locked dependencies.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY services/gateway/pyproject.toml services/gateway/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY services/gateway/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

# Runtime contains only the provisioner package, pinned Helm and baked charts.
FROM python:3.12-slim
ARG HELM_VERSION=v4.3.0
ARG HELM_SHA256=86584a54def73570558f66f5111cc53dfed56689637ae32c1201205d494f54fb
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    && echo "${HELM_SHA256}  /tmp/helm.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/helm.tar.gz -C /tmp \
    && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64 /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY services/gateway/alembic.ini ./
COPY services/gateway/migrations ./migrations
COPY deploy/charts/woo-store /charts/woo-store
COPY deploy/charts/hosted-mcp /charts/hosted-mcp
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 10001
CMD ["python", "-m", "gateway.provisioner"]
