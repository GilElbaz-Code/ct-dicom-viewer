# syntax=docker/dockerfile:1
# One image for the API, the worker, migrations and tools; the command picks the role.

ARG PYTHON_VERSION=3.13

# --- build: install dependencies into a virtualenv -------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# Requirements first: this layer is rebuilt only when dependencies change, not on every code edit.
COPY requirements.txt .
RUN pip install -r requirements.txt

# --- runtime: slim, no build tools, non-root ------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/library /data/uploads /data/derived \
 && chown -R app:app /data/uploads /data/derived
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY migrations ./migrations
COPY app ./app
USER app
EXPOSE 8000
# Worker processes: WEB_CONCURRENCY (read by uvicorn), set in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

# --- test: runtime + dev tools + tests (CI: docker build --target test) ----------------------
FROM runtime AS test
USER root
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY pyproject.toml ./
COPY tests ./tests
USER app
CMD ["pytest", "-p", "no:cacheprovider"]
