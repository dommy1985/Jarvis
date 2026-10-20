# Build dalla root del repo: docker compose build jarvis
FROM python:3.12-slim

ARG CLAUDE_CODE_VERSION=2.1.296
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --create-home jarvis && mkdir /data && chown jarvis /data
USER jarvis
# CLI ufficiale Claude Code, versione fissata; si autentica con CLAUDE_CODE_OAUTH_TOKEN (claude setup-token).
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && /home/jarvis/.local/bin/claude --version
ENV PATH=/home/jarvis/.local/bin:$PATH DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1

COPY jarvis/ jarvis/
COPY db/ db/
COPY prompts/ prompts/
COPY locales/ locales/

ENV JARVIS_CONFIG=/app/config/projects.yaml JARVIS_DB=/data/jarvis.db JARVIS_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["python", "-m", "jarvis.server"]
