import importlib.metadata
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_the_kubernetes_manifests_deploy_the_current_version():
    version = importlib.metadata.version("verifair")
    deployment = (ROOT / "deploy/kustomize/base/deployment.yaml").read_text()
    assert f"image: ghcr.io/romanzdk/verifair:{version}\n" in deployment, (
        f"bump the image tag in deploy/kustomize/base/deployment.yaml to {version}"
    )
