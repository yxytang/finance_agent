FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/finance
WORKDIR /app

# Native libraries used by local OCR and document parsing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 libgl1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 finance \
    && useradd --uid 10001 --gid finance --create-home finance

COPY pyproject.toml README.md ./
COPY fa ./fa
COPY web ./web
COPY backend ./backend
COPY mcp_server ./mcp_server
RUN pip install --no-cache-dir '.[web,documents,cache,rag,kafka]'
COPY alembic.ini ./
COPY alembic ./alembic
COPY data/transactions.csv ./data/transactions.csv
COPY knowledge ./knowledge
COPY skills ./skills
# New named volumes inherit these directories' owner and initial contents.
# API and document workers share the same UID and writable directories.
RUN mkdir -p /app/memory /app/data/backend_jobs \
    && chown -R finance:finance /app/data /app/knowledge /app/memory

USER 10001:10001
EXPOSE 8000
# exec preserves SIGTERM delivery; Compose supplies a tiny init for MCP children.
ENTRYPOINT ["python", "-m", "backend.container"]
CMD ["python", "-m", "web.server", "--host", "0.0.0.0", "--port", "8000"]
