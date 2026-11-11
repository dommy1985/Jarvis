# Runner VPS (JARVIS_RUNNER_ID=vps): stesso runner del Mac, in un container isolato, solo per i progetti con
# `target: vps|auto` (Node: test/lint/tsc, niente build native Android/iOS). Build dalla root del repo:
#   docker build -f runner/Dockerfile -t jarvis-runner-vps .
FROM python:3.12-slim

ARG CLAUDE_CODE_VERSION=2.1.296
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git openssh-client gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/gh.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/gh.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && corepack enable && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# /repos: checkout dei progetti (vps_path). ~/.jarvis: esiti non consegnati, log, worktree e hook: va su un volume
# persistente (un riavvio non deve perdere gli esiti in sospeso). Creati qui perché il volume erediti il proprietario.
RUN useradd --uid 10002 --create-home runner && mkdir -p /repos /home/runner/.jarvis \
    && chown runner /repos /home/runner/.jarvis
USER runner
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && /home/runner/.local/bin/claude --version
ENV PATH=/home/runner/.local/bin:$PATH DISABLE_AUTOUPDATER=1 DISABLE_TELEMETRY=1

COPY jarvis/ jarvis/
COPY runner/ runner/
COPY prompts/ prompts/
COPY locales/ locales/
COPY db/ db/

# JARVIS_RUNNER_ID, JARVIS_URL (http://jarvis:8080), JARVIS_RUNNER_TOKEN, GH_TOKEN e CLAUDE_CODE_OAUTH_TOKEN
# arrivano dall'ambiente (docker-compose del hub); i checkout stanno in /repos (vps_path dei progetti), lo stato in /home/runner/.jarvis.
ENV JARVIS_RUNNER_ID=vps JARVIS_HOME=/home/runner/.jarvis
CMD ["python", "-m", "runner.runner"]
