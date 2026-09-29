# Read-only Schwab MCP server.
#   docker build -t schwab-mcp .
#   docker run -it --rm -v schwab-mcp:/config -e SCHWAB_CLIENT_ID -e SCHWAB_CLIENT_SECRET schwab-mcp auth
#   docker run -i  --rm -v schwab-mcp:/config -e SCHWAB_CLIENT_ID -e SCHWAB_CLIENT_SECRET schwab-mcp
# The token (and an optional .env) live in the /config volume, shared by `auth` and the server.

FROM python:3.12-slim-bookworm AS build
RUN pip install --no-cache-dir "uv>=0.8"
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /config/schwab-mcp \
    && chown -R app:app /config \
    && chmod 700 /config/schwab-mcp
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/config \
    MCP_HOST=0.0.0.0
USER app
WORKDIR /home/app
VOLUME ["/config"]
EXPOSE 8765
ENTRYPOINT ["schwab-mcp"]
CMD ["serve"]
