FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY verifair ./verifair
RUN uv sync --frozen --no-dev
ENV VERIFAIR_DB_PATH=/data/verifair.db
VOLUME /data
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "verifair.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
