import importlib.metadata
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_the_kubernetes_manifests_deploy_the_current_version():
    version = importlib.metadata.version("verifair")
    deployment = (ROOT / "deploy/kustomize/base/deployment.yaml").read_text()
    assert f"image: ghcr.io/romanzdk/verifair:{version}\n" in deployment, (
        f"bump the image tag in deploy/kustomize/base/deployment.yaml to {version}"
    )


def test_compose_runs_the_current_release_image():
    version = importlib.metadata.version("verifair")
    compose = (ROOT / "docker-compose.yml").read_text()
    assert f"image: ghcr.io/romanzdk/verifair:${{VERIFAIR_VERSION:-{version}}}\n" in compose, (
        f"bump the app image tag in docker-compose.yml to {version}"
    )


def test_compose_does_not_publish_postgres():
    # a host that already runs Postgres on 5432 would fail the first `docker compose up`
    compose = (ROOT / "docker-compose.yml").read_text()
    db = compose.split("\n  app:\n")[0]
    assert "ports:" not in db
