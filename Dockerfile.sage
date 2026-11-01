# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app

RUN addgroup --system --gid 1001 appgroup \
    && adduser --system --uid 1001 --ingroup appgroup appuser \
    && mkdir -p /data \
    && chown 1001:1001 /data

COPY --from=build --chown=1001:1001 /install /usr/local
COPY certs/cloudflare-gateway-ca.crt /usr/local/share/ca-certificates/cloudflare-gateway-ca.crt
RUN update-ca-certificates

ENV PYTHONUNBUFFERED=1
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ARG APP_PORT=8000
ENV PORT=${APP_PORT}
EXPOSE 8000 8080
LABEL org.opencontainers.image.title="ViharBot sage agent"

USER 1001:1001
CMD ["sh", "-c", "uvicorn viharbot.sage_agent:app --host 0.0.0.0 --port \"$PORT\""]
