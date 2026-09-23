# Runtime image for the digest one-shot container (see PLAN.md §3, §6).
# Not built on the VM: this image is only ever built by
# .github/workflows/release.yml and pulled by the homelab repo's Ansible
# `myapps` role. `docker build .` / `docker compose build` here are for local
# dev only.

FROM python:3.14-slim-bookworm

# --- uv ---
# Pinned uv release tag (not :latest) for reproducible builds. Renovate
# tracks COPY --from=<image>:<tag> references, so it can open a bump PR here
# same as it does for FROM lines.
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

# --- Node.js + Claude Code CLI ---
# Debian bookworm's apt nodejs package is 18.x, which meets Claude Code's
# minimum supported Node version — plain apt avoids adding the NodeSource
# repo/key for what is otherwise a non-perf-critical CLI dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Pinned to the same version the owner's homelab repo pins as
# `claude_cli_version` for other Claude Code-based homelab services — bump
# both together, deliberately, not independently.
RUN npm install -g @anthropic-ai/claude-code@2.1.278

WORKDIR /app

# uv Docker guidance: use the system interpreter (already 3.12 here) instead
# of letting uv download its own, and copy instead of hardlink since the
# layer cache and the final image are on different filesystems.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Copy only the dependency manifests first so `uv sync` is cached across
# builds unless pyproject.toml/uv.lock actually changed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Runtime needs only the application package and the prompt template.
# tests/ (dev-only suite) and scripts/ (one-time interactive Telegram login,
# PR tooling) are intentionally left out of the image — they're never
# invoked by the running container.
COPY digest/ ./digest/
COPY prompts/ ./prompts/

# Non-root runtime user.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin digest \
    && mkdir -p /data \
    && chown -R digest:digest /app /data
USER digest

ENV PATH="/app/.venv/bin:${PATH}"
# USER is set explicitly because digest/config.py's claude_subprocess_env()
# passes it through to the `claude -p` subprocess — required by the CLI's
# auth flow per that function's docstring.
ENV USER=digest
# CLAUDE_CONFIG_DIR is deliberately NOT set here. Auth is a long-lived
# CLAUDE_CODE_OAUTH_TOKEN supplied at runtime, so the CLI's config dir holds
# nothing worth persisting. Baking a default made it worse than useless: an
# env_file can only OVERRIDE an image ENV, never unset one, so a deployment
# that wanted an ephemeral dir could not simply omit the variable -- it had
# to know this default existed in order to override it. Callers that want a
# specific location set CLAUDE_CONFIG_DIR themselves.

ENTRYPOINT ["python", "-m", "digest"]
