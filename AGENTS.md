# Notes for coding agents

Read the README's Contributing section first. The rest is what it does not say.

## Checks before a pull request

```bash
docker run -d --rm --name verifair-dev-db -e POSTGRES_USER=verifair -e POSTGRES_PASSWORD=verifair \
  -p 127.0.0.1:5432:5432 postgres:18
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run --locked --only-group docs mkdocs build --strict -d /tmp/verifair-site
```

- Every pull request adds a line under `## [Unreleased]` in `CHANGELOG.md`.
- CI upgrades from the last release: it loads that release's demo with its image, then runs this code on the same database (`scripts/upgrade-check.py`). A schema change must keep everything the old release wrote: the same values and the same list lengths (new fields are fine), and the upgraded schema must match a fresh one.
- The docs build is strict: a link from `CHANGELOG.md` to a file under `docs/` breaks it, so write the path as plain text.
- Use plain, simple language in docs and messages.

## Release

The version is written in four places. Each has a test that fails, naming the file, when one is missed.

1. Set the new version in `pyproject.toml`, then run `uv lock`.
2. In `CHANGELOG.md`, add `## [X.Y.Z] - YYYY-MM-DD` under `## [Unreleased]`, and update the compare links at the bottom.
3. Change the image tag to `X.Y.Z` in `docker-compose.yml` and `deploy/kustomize/base/deployment.yaml`.
4. Change the `blob/vOLD/` doc links in `skills/verifair-review/SKILL.md` to `blob/vX.Y.Z/`.
5. Run the checks above. They must pass now, because the tag pipeline runs the tests before it publishes anything.
6. Commit `chore: release X.Y.Z`, tag `vX.Y.Z` (both signed), and push with `git push origin main --follow-tags`.

The tag pipeline publishes `ghcr.io/romanzdk/verifair:X.Y.Z` and creates the GitHub release from the changelog section.
