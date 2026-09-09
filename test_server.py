# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from provider import DURATIONS, MODEL, SIZES, Store, VideoRequest
from server import Settings, create_app

TOKEN = "x" * 32
BASE = "http://127.0.0.1:8088"


def request_body(**changes):
    body = {
        "model": MODEL,
        "prompt": "A lighthouse in a storm",
        "duration": 5,
        "size": "960x544",
        "seed": 42,
        "generate_audio": True,
        "local": {"export": "original"},
    }
    body.update(changes)
    return body


@pytest.fixture
def app_client(tmp_path):
    app = create_app(Settings(token=TOKEN, root=tmp_path), start_worker=False)
    with TestClient(app, base_url=BASE) as client:
        yield app, client


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def submit(client, body=None, headers=None, **extra_headers):
    return client.post(
        "/api/v1/videos",
        json=body or request_body(),
        headers=auth() | (headers or {}) | extra_headers,
    )


def complete(store, job_id, data=b"0123456789"):
    folder = store.root / "media" / job_id
    folder.mkdir(parents=True)
    (folder / "final.mp4").write_bytes(data)
    store.finish(job_id, metadata={"tested": True})


def test_capabilities_exact_contract(app_client):
    _, client = app_client
    response = client.get("/api/v1/videos/models", headers=auth())
    assert response.status_code == 200
    model = response.json()["data"][0]
    assert model["id"] == MODEL
    assert model["supported_sizes"] == SIZES == ["960x544", "544x960", "768x768"]
    assert model["supported_durations"] == DURATIONS == [5, 10]
    assert model["local"]["duration_frames"] == {"5": 124, "10": 243}
    assert model["local"]["audio"] == "optional_delivery"
    assert submit(client, request_body(generate_audio=False)).status_code == 202


@pytest.mark.parametrize(
    "field,value", [("resolution", "544p"), ("aspect_ratio", "30:17")]
)
def test_rejects_unsupported_fields_even_when_they_match(app_client, field, value):
    _, client = app_client
    response = submit(client, request_body(**{field: value}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "changes",
    [
        {"size": "1920x1080"},
        {"duration": 6},
        {"local": {"export": "square"}},
        {"prompt": "   "},
        {"unknown": True},
    ],
)
def test_strict_request_combinations(app_client, changes):
    _, client = app_client
    assert submit(client, request_body(**changes)).status_code == 400


def test_durable_queue_idempotency_conflict_and_overload(tmp_path):
    settings = Settings(token=TOKEN, root=tmp_path, queue_limit=1)
    with TestClient(create_app(settings, start_worker=False), base_url=BASE) as client:
        first = submit(client, headers={"Idempotency-Key": "stable-1"})
        assert first.status_code == 202
        repeated = submit(client, headers={"Idempotency-Key": "stable-1"})
        assert repeated.status_code == 202
        assert repeated.json()["id"] == first.json()["id"]
        conflict = submit(
            client,
            request_body(prompt="Different"),
            **{"Idempotency-Key": "stable-1"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"
        full = submit(client, headers={"Idempotency-Key": "stable-2"})
        assert full.status_code == 429
        assert full.headers["retry-after"] == "30"

    # A new app process observes the same queued job and idempotency record.
    with TestClient(create_app(settings, start_worker=False), base_url=BASE) as client:
        persisted = submit(client, headers={"Idempotency-Key": "stable-1"})
        assert persisted.json()["id"] == first.json()["id"]


def test_bearer_cookie_csrf_host_and_logout(app_client):
    _, client = app_client
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=auth()).status_code == 200
    assert (
        client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert client.post("/session", json={"token": TOKEN}).status_code == 200
    assert client.get("/health").status_code == 200

    cross = client.post(
        "/api/v1/videos",
        json=request_body(),
        headers={"Origin": "http://evil.invalid"},
    )
    assert cross.status_code == 403
    missing_origin = client.post("/api/v1/videos", json=request_body())
    assert (
        missing_origin.status_code == 403
    )  # Cookie mutation requires same-origin proof.
    same = client.post("/api/v1/videos", json=request_body(), headers={"Origin": BASE})
    assert same.status_code == 202
    assert client.get("/health", headers={"Host": "localhost:8088"}).status_code == 400
    assert client.delete("/session", headers={"Origin": BASE}).status_code == 204
    assert client.get("/health").status_code == 401


def test_retention_preserves_active_and_expires_completed(tmp_path):
    store = Store(tmp_path, retention_days=1)
    old = time.time() - 2 * 86400
    active = store.submit(VideoRequest(**request_body(prompt="active")), None)
    done = store.submit(VideoRequest(**request_body(prompt="done")), None)
    complete(store, done["id"])
    with store.connect() as db:
        db.execute("UPDATE jobs SET created=? WHERE id=?", (old, active["id"]))
        db.execute("UPDATE jobs SET completed=? WHERE id=?", (old, done["id"]))
    store.cleanup()
    assert store.get(active["id"])["status"] == "pending"
    assert (tmp_path / "media" / active["id"]).exists() is False
    assert store.get(done["id"])["expired"] == 1
    assert not (tmp_path / "media" / done["id"]).exists()


def test_restart_reconcile_fails_running_but_pending_survives(tmp_path):
    store = Store(tmp_path)
    running = store.submit(VideoRequest(**request_body(prompt="running")), None)
    pending = store.submit(VideoRequest(**request_body(prompt="pending")), None)
    assert store.claim()["id"] == running["id"]
    store.reconcile()
    failed = store.get(running["id"])
    assert failed["status"] == "failed"
    assert failed["phase"] == "interrupted"
    assert json.loads(failed["error"])["code"] == "worker_interrupted"
    assert store.get(pending["id"])["status"] == "pending"


def test_authenticated_ranges_head_index_and_unavailable(app_client):
    app, client = app_client
    row = app.state.store.submit(VideoRequest(**request_body()), None)
    complete(app.state.store, row["id"])
    url = f"/api/v1/videos/{row['id']}/content"
    assert client.get(url).status_code == 401
    whole = client.get(url, headers=auth())
    assert whole.status_code == 200 and whole.content == b"0123456789"
    partial = client.get(url, headers=auth() | {"Range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    suffix = client.get(url, headers=auth() | {"Range": "bytes=-3"})
    assert suffix.status_code == 206 and suffix.content == b"789"
    malformed = client.get(url, headers=auth() | {"Range": "bytes=5-2"})
    assert malformed.status_code == 416
    assert malformed.headers["content-range"] == "bytes */10"
    assert client.get(url + "?index=1", headers=auth()).status_code == 400
    head = client.head(url, headers=auth())
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == "10"
    (app.state.store.root / "media" / row["id"] / "final.mp4").unlink()
    assert client.get(url, headers=auth()).status_code == 410


def test_partial_download_fd_survives_unlink(tmp_path):
    """Linux guarantees the server's already-open content fd remains readable."""
    path = tmp_path / "video"
    path.write_bytes(b"abcdefghij")
    stream = path.open("rb")
    path.unlink()
    stream.seek(3)
    assert stream.read(4) == b"defg"
    stream.close()


def test_delete_pending_is_durable_hidden_and_idempotent(app_client):
    app, client = app_client
    job = submit(client, headers={"Idempotency-Key": "delete-test"}).json()
    url = f"/api/v1/videos/{job['id']}"
    assert client.delete(url).status_code == 401
    assert client.delete(url, headers=auth()).status_code == 204
    assert client.delete(url, headers=auth()).status_code == 204
    assert client.get("/api/v1/videos", headers=auth()).json()["data"] == []
    assert app.state.store.claim() is None
    tombstone = client.get(url, headers=auth()).json()
    assert tombstone["status"] == "failed" and tombstone["local"]["deleted"]
    assert tombstone["error"]["code"] == "job_deleted"
    assert submit(client, headers={"Idempotency-Key": "delete-test"}).status_code == 410
    reopened = Store(app.state.store.root)
    assert reopened.recent() == [] and reopened.get(job["id"])["deleted"]
    assert client.delete("/api/v1/videos/missing", headers=auth()).status_code == 404


def test_delete_rejects_active_job(app_client):
    app, client = app_client
    row = app.state.store.submit(VideoRequest(**request_body()), None)
    with app.state.store.connect() as db:
        db.execute("UPDATE jobs SET status='in_progress' WHERE id=?", (row["id"],))
    result = client.delete(f"/api/v1/videos/{row['id']}", headers=auth())
    assert result.status_code == 409
    assert app.state.store.get(row["id"])["status"] == "in_progress"
    assert not app.state.store.get(row["id"])["deleted"]


def test_cancel_is_authenticated_active_idempotent_and_wins_finish_race(app_client):
    app, client = app_client
    pending = submit(client).json()
    cancel_url = f"/api/v1/videos/{pending['id']}/cancel"
    assert client.post(cancel_url, headers=auth()).status_code == 409

    assert app.state.store.claim()["id"] == pending["id"]
    assert client.post(cancel_url).status_code == 401
    requested = client.post(cancel_url, headers=auth())
    assert requested.status_code == 202
    assert requested.json()["status"] == "in_progress"
    assert requested.json()["local"]["phase"] == "canceling"
    assert client.post(cancel_url, headers=auth()).status_code == 202

    assert not app.state.store.finish(pending["id"], metadata={"too_late": True})
    app.state.store.finish_cancelled(pending["id"])
    canceled = client.get(f"/api/v1/videos/{pending['id']}", headers=auth()).json()
    assert canceled["status"] == "failed"
    assert canceled["local"]["phase"] == "canceled"
    assert canceled["error"]["code"] == "job_canceled"


def test_delete_completed_removes_media_and_retains_tombstone(app_client):
    app, client = app_client
    job = submit(client, headers={"Idempotency-Key": "delete-completed"}).json()
    media = app.state.store.root / "media" / job["id"]
    complete(app.state.store, job["id"])
    assert media.is_dir()

    url = f"/api/v1/videos/{job['id']}"
    assert client.delete(url, headers=auth()).status_code == 204
    assert not media.exists()
    assert app.state.store.recent() == []
    tombstone = client.get(url, headers=auth()).json()
    assert tombstone["status"] == "failed" and tombstone["local"]["deleted"]
    assert tombstone["error"]["code"] == "job_deleted"
    assert (
        submit(client, headers={"Idempotency-Key": "delete-completed"}).status_code
        == 410
    )
    assert client.delete(url, headers=auth()).status_code == 204


def test_delete_cookie_requires_same_origin(app_client):
    _, client = app_client
    job = submit(client).json()
    client.post("/session", json={"token": TOKEN})
    url = f"/api/v1/videos/{job['id']}"
    assert client.delete(url).status_code == 403
    assert (
        client.delete(url, headers={"Origin": "http://evil.invalid"}).status_code == 403
    )
    assert client.delete(url, headers={"Origin": BASE}).status_code == 204


def test_claim_and_delete_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from provider import AdmissionError

    store = Store(tmp_path)
    for _ in range(12):
        row = store.submit(VideoRequest(**request_body()), None)
        barrier = Barrier(2)

        def claim(barrier):
            barrier.wait()
            return store.claim()

        def delete(barrier, job_id):
            barrier.wait()
            try:
                store.delete_job(job_id)
                return 204
            except AdmissionError as exc:
                return exc.status

        with ThreadPoolExecutor(2) as pool:
            claimed = pool.submit(claim, barrier)
            deleted = pool.submit(delete, barrier, row["id"])
            claimed, deleted = claimed.result(), deleted.result()
        actual = store.get(row["id"])
        if claimed:
            assert deleted == 409 and actual["status"] == "in_progress"
            assert not actual["deleted"]
            store.finish(row["id"], error={"code": "test", "message": "test"})
        else:
            assert deleted == 204 and actual["deleted"]


def test_explicit_hostname_alias_preserves_same_origin_links(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 80))
        ],
    )
    alias = "http://gpu.example.internal"
    settings = Settings(token=TOKEN, root=tmp_path, allowed_origins=(alias,))
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url=alias) as client:
        assert client.get("/").status_code == 200
        assert client.post("/session", json={"token": TOKEN}).status_code == 200
        job = client.post(
            "/api/v1/videos", json=request_body(), headers={"Origin": alias}
        ).json()
        assert job["polling_url"].startswith(alias + "/")
        complete(app.state.store, job["id"])
        done = client.get(job["polling_url"]).json()
        assert done["unsigned_urls"][0].startswith(alias + "/")
        assert (
            client.post(
                "/api/v1/videos", json=request_body(), headers={"Origin": BASE}
            ).status_code
            == 403
        )
        assert client.get("/", headers={"Host": "unlisted.test"}).status_code == 400


def test_failed_job_deletion_and_measured_progress(app_client):
    app, client = app_client
    store = app.state.store
    row = store.submit(VideoRequest(**request_body()), "failed-deletion")
    store.claim()
    store.progress(row["id"], 12, 49)
    job = client.get(f"/api/v1/videos/{row['id']}", headers=auth()).json()
    assert job["local"]["progress"]["completed"] == 12
    assert job["local"]["progress"]["total"] == 49
    assert job["local"]["phase"] == "denoising"
    store.progress(row["id"], 49, 49)
    assert store.get(row["id"])["phase"] == "decoding"
    assert store.get(row["id"])["status"] == "in_progress"
    with pytest.raises(ValueError):
        store.progress(row["id"], 50, 49)
    store.finish(row["id"], error={"code": "test", "message": "test failure"})
    assert (
        client.delete(f"/api/v1/videos/{row['id']}", headers=auth()).status_code == 204
    )
    assert store.recent() == []


def test_studio_automatic_session_keeps_api_key_private(app_client):
    _, client = app_client
    assert client.get("/health").status_code == 401
    assert client.post("/session").status_code == 403
    assert (
        client.post("/session", headers={"Origin": "http://evil.test"}).status_code
        == 403
    )
    response = client.post("/session", headers={"Origin": BASE})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert TOKEN not in response.text + response.headers["set-cookie"]
    assert client.get("/health").status_code == 200
    assert client.post("/api/v1/videos", json=request_body()).status_code == 403
    assert client.get("/assets/studio-tokens.css").status_code == 200
    assert client.get("/assets/fonts/Inter-latin.woff2").status_code == 200
    icon = client.get("/assets/icon.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")
    html = client.get("/").text
    assert 'class="mark" src="/assets/icon.png"' in html
    assert 'id="token"' not in html
    assert TOKEN not in html


def test_environment_configuration_in_fresh_process(tmp_path):
    # Exercise real environment parsing without patching Settings or its inputs.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path
from server import Settings
settings = Settings.from_env()
assert settings.token == os.environ['REEL_VIDEO_API_TOKEN']
assert settings.base_url == 'http://127.0.0.1:18088'
assert settings.root == Path(os.environ['REEL_VIDEO_STATE_DIR'])
assert settings.queue_limit == 7
assert settings.retention_days == 5
assert settings.quota_bytes == 40 * 2**30
assert settings.allowed_origins == ('http://192.0.2.1:18088',)
""",
        ],
        env={
            **os.environ,
            "REEL_VIDEO_API_TOKEN": TOKEN,
            "REEL_VIDEO_BASE_URL": "http://127.0.0.1:18088",
            "REEL_VIDEO_STATE_DIR": str(tmp_path),
            "REEL_VIDEO_QUEUE_LIMIT": "7",
            "REEL_VIDEO_RETENTION_DAYS": "5",
            "REEL_VIDEO_QUOTA_GIB": "40",
            "REEL_VIDEO_ALLOWED_ORIGINS": " http://192.0.2.1:18088, ",
        },
        check=True,
        timeout=20,
    )


@pytest.mark.parametrize("retention,quota", [(None, None), (5, 40)])
def test_storage_configuration_defaults_and_overrides(
    tmp_path, monkeypatch, retention, quota
):
    monkeypatch.setenv("REEL_VIDEO_API_TOKEN", TOKEN)
    monkeypatch.setenv("REEL_VIDEO_BASE_URL", BASE)
    monkeypatch.setenv("REEL_VIDEO_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("REEL_VIDEO_STATE_DIR", str(tmp_path))
    for key, value in (
        ("REEL_VIDEO_RETENTION_DAYS", retention),
        ("REEL_VIDEO_QUOTA_GIB", quota),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    settings = Settings.from_env()
    assert settings.retention_days == (retention or 3)
    assert settings.quota_bytes == (quota or 25) * 2**30
    defaults = Settings(token=TOKEN, root=tmp_path)
    assert defaults.retention_days == 3
    assert defaults.quota_bytes == 25 * 2**30
    store = Store(tmp_path)
    assert store.retention_seconds == 3 * 86400
    assert store.quota_bytes == 25 * 2**30
