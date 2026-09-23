FROM node:22-alpine AS web
WORKDIR /build
RUN npm install --global pnpm@11.19.0
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src ./src
RUN pnpm build

FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.17
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY --from=web /build/dist ./web/dist
RUN useradd --system --uid 10001 --create-home awb && mkdir /data && chown awb:awb /data
USER awb
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["/app/.venv/bin/awb"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/agent-workbench.db"]
