FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.15-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0
COPY --from=uv /uv /bin/uv
RUN useradd -r -u 10001 app
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY verifair ./verifair
COPY skills ./skills
RUN uv sync --frozen --no-dev
ENV UV_NO_CACHE=1 FORWARDED_ALLOW_IPS=* UVICORN_HOST=0.0.0.0
USER 10001
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"
CMD ["uv", "run", "--no-sync", "uvicorn", "verifair.asgi:app", "--port", "8000", "--proxy-headers"]
