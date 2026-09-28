# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - build and verify. The full test suite runs here, so a failing test
# fails the image rather than reaching the deployment.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt

COPY app ./app
COPY tests ./tests

RUN python -m unittest discover -s tests -p "test_*.py" -v

# ---------------------------------------------------------------------------
# Stage 2 - runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=build /usr/local /usr/local

WORKDIR /srv
COPY app ./app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /data/uploads \
 && chown -R appuser:appuser /data /srv

USER appuser

ENV KOALACARE_ENV=production \
    KOALACARE_UPLOAD_DIR=/data/uploads \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "8", "--timeout", "60", "--graceful-timeout", "20", "--access-logfile", "-", "--error-logfile", "-", "app.main:create_app()"]
