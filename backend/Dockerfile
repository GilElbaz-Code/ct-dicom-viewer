# One image for the API and the worker; docker-compose.yml picks the command.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv
WORKDIR /app

# Dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change, not on every code edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1

COPY api ./api
COPY core ./core
COPY db ./db
COPY services ./services
COPY main.py ./

# Never run as root; the DICOM folder is mounted read-only anyway.
RUN useradd --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
