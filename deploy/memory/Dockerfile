FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md uv.lock /app/
COPY src /app/src
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home workbench && mkdir -p /data && chown workbench /data
USER 10001:10001
EXPOSE 8766
ENTRYPOINT ["awb", "memory-serve"]
CMD ["--database", "/data/replica/memory.db", "--host", "0.0.0.0", "--allow-remote", "--port", "8766", "--token-file", "/run/secrets/memory_token"]
