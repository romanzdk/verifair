import base64
import hashlib
from contextlib import closing

from fastapi.testclient import TestClient

from verifair import db

H = {"Authorization": "Bearer t"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8


def upload(client, data=PNG, content_type="image/png", headers=H):
    return client.post(
        "/api/images", content=data, headers=headers | {"Content-Type": content_type}
    )


def test_upload_returns_a_url_that_serves_the_image(client):
    r = upload(client)
    digest = hashlib.sha256(PNG).hexdigest()
    assert (r.status_code, r.json()) == (201, {"url": f"/images/{digest}.png"})
    image = client.get(r.json()["url"])
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_same_image_twice_gives_the_same_url(client):
    assert upload(client).json() == upload(client).json()


def test_jpeg_and_webp_are_accepted(client):
    assert upload(client, JPEG, "image/jpeg").json()["url"].endswith(".jpg")
    assert upload(client, WEBP, "image/webp").json()["url"].endswith(".webp")


def test_other_types_are_refused(client):
    r = upload(client, b"<svg/>", "image/svg+xml")
    assert r.status_code == 415
    assert r.json() == {"detail": "Send a PNG, JPEG or WebP image."}


def test_bytes_must_match_the_declared_type(client):
    r = upload(client, JPEG, "image/png")
    assert (r.status_code, r.json()) == (422, {"detail": "The file is not a valid image/png."})


def test_upload_needs_a_token(client):
    assert upload(client, headers={}).status_code == 401


def test_images_up_to_5_mb_pass_the_body_limit(client):
    big = PNG + b"\x00" * (5 * 1024 * 1024 - len(PNG))
    assert upload(client, big).status_code == 201
    assert upload(client, big + b"\x00").status_code == 413


def test_other_api_calls_keep_the_1_mb_limit(client):
    r = client.post("/api/reports", content=b"x" * (1024 * 1024 + 1), headers=H)
    assert r.status_code == 413


def test_unknown_or_mismatched_image_is_404(client):
    url = upload(client).json()["url"]
    assert client.get(url.replace(".png", ".jpg")).status_code == 404
    assert client.get(f"/images/{'0' * 64}.png").status_code == 404


def test_shared_mode_upload_with_member_token_and_view_needs_sign_in(
    shared_client, shared_settings, make_app
):
    with closing(db.connect(shared_settings.database_url)) as conn:
        token, _ = db.create_token(conn, owner_id="admin@example.com", label="agent")
    r = upload(shared_client, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    assert shared_client.get(r.json()["url"]).content == PNG
    anonymous = TestClient(shared_client.app)
    seen = anonymous.get(r.json()["url"], follow_redirects=False)
    assert seen.status_code != 200
    assert seen.content != PNG


def _push(client, id, body):
    payload = {"id": id, "project": "p", "title": "t", "risk": "low", "body": body}
    assert client.post("/api/reports", json=payload, headers=H).status_code == 201


def test_deleting_a_report_removes_images_only_it_used(client, settings):
    only = upload(client).json()["url"]
    shared = upload(client, JPEG, "image/jpeg").json()["url"]
    _push(client, "one", f"## Intent\n![a]({only})\n![b]({shared})")
    _push(client, "two", f"## Intent\n![b]({shared})")
    with closing(db.connect(settings.database_url)) as conn:
        assert db.delete_report(conn, "one")
    assert client.get(only).status_code == 404
    assert client.get(shared).status_code == 200


def test_images_in_an_older_version_or_plan_count_as_used(client, settings):
    url = upload(client).json()["url"]
    _push(client, "keep", f"## Intent\n![a]({url})")
    _push(client, "keep", "## Intent\nno image now")
    _push(client, "gone", f"## Intent\n![a]({url})")
    with closing(db.connect(settings.database_url)) as conn:
        assert db.delete_report(conn, "gone")
    assert client.get(url).status_code == 200
