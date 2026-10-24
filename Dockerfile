FROM python:3.15-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.16 /uv /bin/uv
RUN useradd -r -u 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY verifair ./verifair
RUN uv sync --frozen --no-dev
ENV UV_NO_CACHE=1 FORWARDED_ALLOW_IPS=* UVICORN_HOST=0.0.0.0
USER app
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"
CMD ["uv", "run", "--no-sync", "uvicorn", "verifair.asgi:app", "--port", "8000", "--proxy-headers"]
