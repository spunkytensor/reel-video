# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

import io
import json
import time
from collections import namedtuple

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import provider
from image_codec import MAX_UPLOAD_BYTES, DecodedImage, decode_image
from provider import (
    IMAGE_PATH,
    MODEL,
    AdmissionError,
    Store,
    VideoRequest,
    WorkflowSwitch,
)
from server import Settings, create_app

TOKEN = "i" * 32
BASE = "http://127.0.0.1:8088"


def png(size=(32, 24), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


def body(**changes):
    result = {"model": MODEL, "prompt": "image contract"}
    result.update(changes)
    return result


def ref(url):
    return {"type": "image_url", "image_url": {"url": url}}


def frame(url, role):
    return ref(url) | {"frame_type": role}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(token=TOKEN, root=tmp_path), start_worker=False)
    with TestClient(app, base_url=BASE) as value:
        yield app, value


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def upload(client, data=None, **headers):
    return client.post(
        IMAGE_PATH.rstrip("/"),
        content=data or png(),
        headers=auth() | {"Content-Type": "image/png"} | headers,
    )


def test_raw_upload_auth_contract_and_normalized_get_head(client):
    _, http = client
    assert http.post(IMAGE_PATH.rstrip("/"), content=png()).status_code == 401
    response = upload(http)
    assert response.status_code == 201
    value = response.json()
    assert set(value) == {"id", "url", "width", "height", "expires_at"}
    assert value["url"] == BASE + IMAGE_PATH + value["id"]
    assert (value["width"], value["height"]) == (32, 24)
    assert http.get(value["url"]).status_code == 401
    fetched = http.get(value["url"], headers=auth())
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(fetched.content)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
    head = http.head(value["url"], headers=auth())
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(fetched.content)


def test_session_upload_requires_csrf_origin(client):
    _, http = client
    assert http.post("/session", json={"token": TOKEN}).status_code == 200
    assert http.post(IMAGE_PATH.rstrip("/"), content=png()).status_code == 403
    assert (
        http.post(
            IMAGE_PATH.rstrip("/"),
            content=png(),
            headers={"Origin": BASE, "Content-Type": "image/png"},
        ).status_code
        == 201
    )


def test_fake_and_streamed_oversize_uploads_are_rejected(client):
    _, http = client
    fake = upload(http, b"not png")
    assert fake.status_code == 400

    def chunks():
        yield b"x" * (MAX_UPLOAD_BYTES // 2)
        yield b"x" * (MAX_UPLOAD_BYTES // 2 + 1)

    oversized = http.post(
        IMAGE_PATH.rstrip("/"),
        content=chunks(),
        headers=auth() | {"Content-Type": "image/png"},
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "body_too_large"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/image.png",
        "http://169.254.169.254/latest/meta-data",
        "file:///tmp/image.png",
        "/tmp/image.png",
        "data:image/png;base64,AAAA",
        "http://[broken/image.png",
        IMAGE_PATH + "image-" + "0" * 64 + "?x=1",
        "http://127.0.0.1:8088@evil.test" + IMAGE_PATH + "image-" + "0" * 64,
    ],
)
def test_submission_rejects_non_service_image_urls_without_fetch(
    client, monkeypatch, url
):
    _, http = client
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("outbound fetch")
    )
    response = http.post(
        "/api/v1/videos", json=body(input_references=[ref(url)]), headers=auth()
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_image_url"


def test_roles_order_canonical_urls_and_strict_model_parameters(client):
    app, http = client
    one, two = upload(http).json(), upload(http, png(color="blue")).json()
    frames = [frame(one["url"], "last_frame"), frame(two["url"], "first_frame")]
    accepted = http.post(
        "/api/v1/videos", json=body(frame_images=frames), headers=auth()
    )
    assert accepted.status_code == 202
    stored = json.loads(app.state.store.get(accepted.json()["id"])["request"])
    assert [item["frame_type"] for item in stored["frame_images"]] == [
        "last_frame",
        "first_frame",
    ]
    assert all(
        item["image_url"]["url"].startswith(IMAGE_PATH)
        for item in stored["frame_images"]
    )
    for invalid in (
        {"frame_images": [frame(one["url"], "first_frame")] * 2},
        {"frame_images": frames, "input_references": [ref(one["url"])]},
        {"input_references": [ref(one["url"]), ref(two["url"]), ref(one["url"])]},
    ):
        assert (
            http.post(
                "/api/v1/videos", json=body(**invalid), headers=auth()
            ).status_code
            == 400
        )
    for steps in (True, 2.0, "2", 1, 101):
        options = {"options": {"local": {"parameters": {"num_inference_steps": steps}}}}
        assert (
            http.post(
                "/api/v1/videos", json=body(provider=options), headers=auth()
            ).status_code
            == 400
        )


def test_general_reference_aspect_and_materialization_order(client, tmp_path):
    app, http = client
    wide = upload(http, png((80, 20))).json()
    assert (
        http.post(
            "/api/v1/videos",
            json=body(input_references=[ref(wide["url"])]),
            headers=auth(),
        ).status_code
        == 400
    )
    first = upload(http, png(color="green")).json()
    second = upload(http, png(color="blue")).json()
    request = VideoRequest(
        **body(input_references=[ref(first["url"]), ref(second["url"])])
    )
    row = app.state.store.submit(request, None, (BASE,))
    folder = tmp_path / "materialized"
    folder.mkdir()
    paths, provenance = app.state.store.materialize_images(
        VideoRequest.model_validate_json(row["request"]), folder
    )
    assert [item["id"] for item in provenance] == [first["id"], second["id"]]
    assert [path.name for path in paths["reference_image"]] == [
        "input-0.png",
        "input-1.png",
    ]


def test_dedup_tombstone_unknown_quota_and_admission_rollback(tmp_path, monkeypatch):
    store = Store(tmp_path, queue_limit=1)
    decoded = decode_image(png(), "image/png")
    assert store.save_image(decoded)["id"] == store.save_image(decoded)["id"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM images").fetchone()[0] == 1
    missing = "image-" + "f" * 64
    with pytest.raises(AdmissionError) as error:
        store.image(missing)
    assert error.value.status == 404
    with store.connect() as db:
        db.execute("UPDATE images SET expires=?,data=NULL", (time.time() - 1,))
    with pytest.raises(AdmissionError) as error:
        store.image("image-" + decoded.sha256)
    assert error.value.status == 410

    Disk = namedtuple("usage", "total used free")
    monkeypatch.setattr(provider.shutil, "disk_usage", lambda _: Disk(10, 9, 1))
    with pytest.raises(AdmissionError) as error:
        store.save_image(DecodedImage(b"new", "a" * 64, 1, 1))
    assert error.value.code == "image_storage_full"

    # Validation/admission failures never create a job or links.
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM job_images").fetchone()[0] == 0


def test_reference_conditioning_budget_rejects_oom_profile_before_enqueue(client):
    app, http = client
    landscape = upload(http, png((80, 40))).json()
    portrait = upload(http, png((40, 80))).json()
    response = http.post(
        "/api/v1/videos",
        headers=auth(),
        json=body(input_references=[ref(landscape["url"]), ref(portrait["url"])]),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "reference_conditioning_too_large"
    with app.state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM job_images").fetchone()[0] == 0
    # One extreme reference still works; the conservative prompt budget counts
    # UTF-8 bytes, not characters, so multilingual prompts cannot bypass it.
    available = provider.REFERENCE_TOKEN_BUDGET - 8192 - 16
    for prompt, status in [
        ("x" * available, 202),
        ("x" * (available + 1), 400),
        ("狐" * (available // 3 + 1), 400),
    ]:
        response = http.post(
            "/api/v1/videos",
            headers=auth(),
            json=body(prompt=prompt, input_references=[ref(landscape["url"])]),
        )
        assert response.status_code == status
    model = http.get("/api/v1/videos/models", headers=auth()).json()["data"][0]
    assert model["local"]["images"]["reference_conditioning_budget"]["limit"] == 15000


def test_images_pin_across_ttl_retention_restart_and_delete(tmp_path):
    store = Store(tmp_path, retention_days=2)
    saved = store.save_image(decode_image(png(), "image/png"))
    request = VideoRequest(
        **body(frame_images=[frame(IMAGE_PATH + saved["id"], "first_frame")])
    )
    first = store.submit(request, None)
    second = store.submit(request, None)
    with store.connect() as db:
        db.execute("UPDATE images SET expires=?", (time.time() - 3 * 86400,))
    Store(tmp_path, retention_days=2).cleanup()
    assert store.image(saved["id"])["data"] is not None
    store.finish(first["id"], error={"code": "x"})
    store.delete_job(first["id"])
    store.cleanup()
    assert store.image(saved["id"])["data"] is not None  # second pending still pins
    store.delete_job(second["id"])
    store.cleanup()
    with pytest.raises(AdmissionError) as error:
        store.image(saved["id"])
    assert error.value.status == 410


def test_legacy_idempotency_defaults_and_url_canonicalization(tmp_path):
    store = Store(tmp_path)
    request = VideoRequest(**body())
    row = store.submit(request, "legacy")
    legacy = json.loads(row["request"])
    for key in ("frame_images", "input_references", "provider"):
        legacy.pop(key)
    with store.connect() as db:
        db.execute(
            "UPDATE jobs SET request=? WHERE id=?", (json.dumps(legacy), row["id"])
        )
    replay = Store(tmp_path).submit(VideoRequest(**body()), "legacy")
    assert replay["id"] == row["id"]


def test_workflow_handoff_happens_before_claim_and_leaves_jobs_untouched(tmp_path):
    store = Store(tmp_path)
    image = store.save_image(decode_image(png(), "image/png"))
    first = store.submit(
        VideoRequest(
            **body(frame_images=[frame(IMAGE_PATH + image["id"], "first_frame")])
        ),
        None,
    )
    second = store.submit(VideoRequest(**body(prompt="next")), None)
    with pytest.raises(WorkflowSwitch):
        store.claim("t2va")
    assert store.get(first["id"])["status"] == "pending"
    assert store.get(second["id"])["status"] == "pending"
    claimed = store.claim("fl2va")
    assert claimed["id"] == first["id"]
    assert store.get(second["id"])["status"] == "pending"


def test_concurrent_uploads_are_bounded_before_decode(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event

    import server

    _, http = client
    started = Barrier(3)
    release = Event()

    def blocking_decode(data, content_type):
        started.wait(timeout=10)
        assert release.wait(timeout=10)
        return decode_image(data, content_type)

    monkeypatch.setattr(server, "decode_image", blocking_decode)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(upload, http) for _ in range(2)]
        try:
            started.wait(timeout=10)
            response = upload(http)
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "uploads_busy"
            assert response.headers["retry-after"] == "2"
            assert http.post(IMAGE_PATH.rstrip("/"), content=png()).status_code == 401
        finally:
            release.set()
        assert all(future.result(timeout=10).status_code == 201 for future in futures)
    monkeypatch.setattr(server, "decode_image", decode_image)
    assert (
        upload(http).status_code == 201
    )  # slots released, including after busy rejection


@pytest.mark.parametrize("failed", [True, False])
def test_terminal_images_remain_reusable_until_retention(tmp_path, failed):
    store = Store(tmp_path)
    image = store.save_image(decode_image(png(), "image/png"))
    request = VideoRequest(**body(input_references=[ref(IMAGE_PATH + image["id"])]))
    job = store.submit(request, "retained")
    store.finish(job["id"], error={"code": "test"} if failed else None)
    with store.connect() as db:
        db.execute("UPDATE images SET expires=?", (time.time() - 86400,))
    store = Store(tmp_path)
    store.cleanup()
    assert store.image(image["id"])["data"]
    with store.connect() as db:
        db.execute("UPDATE jobs SET completed=?", (time.time() - 8 * 86400,))
    store.cleanup()
    with pytest.raises(AdmissionError) as error:
        store.image(image["id"])
    assert error.value.status == 410
    # Reconciliation is still safe after expiration; creating a NEW job is not.
    assert store.submit(request, "retained")["id"] == job["id"]
    with pytest.raises(AdmissionError) as error:
        store.submit(request, "new")
    assert error.value.status == 410


def test_image_url_idempotency_and_quota_rollback(tmp_path, monkeypatch):
    store = Store(tmp_path, queue_limit=1)
    image = store.save_image(decode_image(png(), "image/png"))
    relative = VideoRequest(**body(input_references=[ref(IMAGE_PATH + image["id"])]))
    absolute = VideoRequest(
        **body(input_references=[ref(BASE + IMAGE_PATH + image["id"])])
    )
    row = store.submit(absolute, "canonical", (BASE,))
    assert store.submit(relative, "canonical")["id"] == row["id"]
    with pytest.raises(AdmissionError) as error:
        store.submit(relative, "full")
    assert error.value.status == 429
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM job_images").fetchone()[0] == 1
    monkeypatch.setattr(provider, "MAX_STORED_IMAGES", 1)
    with pytest.raises(AdmissionError) as error:
        store.save_image(decode_image(png(color="blue"), "image/png"))
    assert error.value.code == "image_storage_full"
    # Deduplication still works at quota without allocating a second payload.
    assert store.save_image(decode_image(png(), "image/png"))["id"] == image["id"]
    monkeypatch.setattr(provider, "MAX_STORED_IMAGES", 128)
    monkeypatch.setattr(provider, "MAX_STORED_IMAGE_BYTES", 1)
    with pytest.raises(AdmissionError) as error:
        store.save_image(decode_image(png(color="blue"), "image/png"))
    assert error.value.code == "image_storage_full"


def test_reference_profile_handoff_preserves_warm_seed_steps_and_export(tmp_path):
    store = Store(tmp_path)
    image = store.save_image(decode_image(png(), "image/png"))
    request = VideoRequest(**body(input_references=[ref(IMAGE_PATH + image["id"])]))
    first = store.submit(request, None)
    assert store.claim(request.worker_profile)["id"] == first["id"]
    store.finish(first["id"])
    warm = VideoRequest(
        **(
            request.model_dump()
            | {
                "seed": 99,
                "local": {"export": "1080p"},
                "provider": {
                    "options": {"local": {"parameters": {"num_inference_steps": 2}}}
                },
            }
        )
    )
    assert warm.worker_profile == request.worker_profile
    second = store.submit(warm, None)
    assert store.claim(request.worker_profile)["id"] == second["id"]
    store.finish(second["id"])
    for changes in (
        {"size": "768x768"},
        {"duration": 10},
        {"prompt": "Different conditioning"},
    ):
        changed = VideoRequest(**(request.model_dump() | changes))
        job = store.submit(changed, None)
        with pytest.raises(WorkflowSwitch):
            store.claim(request.worker_profile)
        assert store.get(job["id"])["status"] == "pending"
        assert store.claim(changed.worker_profile)["id"] == job["id"]
        store.finish(job["id"])
    other = store.save_image(decode_image(png(color="blue"), "image/png"))
    changed = VideoRequest(**body(input_references=[ref(IMAGE_PATH + other["id"])]))
    assert changed.worker_profile != request.worker_profile
