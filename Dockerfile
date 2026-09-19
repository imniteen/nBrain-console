# nbrain daemon in a container. Mount your vault at /vault (it must already contain
# nbrain/config.yaml; run `nbrain setup` on the host first, or `docker run ... nbrain setup --defaults`).
FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .
ENV NBRAIN_VAULT=/vault TZ=UTC
VOLUME ["/vault"]
EXPOSE 8765
CMD ["nbrain", "daemon", "--vault", "/vault"]
