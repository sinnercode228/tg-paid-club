FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --wheel-dir /wheels ".[postgres]"

FROM base AS runtime
RUN useradd --create-home --home-dir /app --uid 10001 paidclub
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links /wheels "tg-paid-club[postgres]" && rm -rf /wheels
WORKDIR /app
USER paidclub
# Listen on every interface inside the container; publish the port as needed.
ENV PAIDCLUB_WEB_HOST=0.0.0.0 \
    PAIDCLUB_WEB_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, sys, urllib.request; port = os.environ.get('PAIDCLUB_WEB_PORT', '8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"
CMD ["paidclub", "run"]
