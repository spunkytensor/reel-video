# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Private-network HTTP provider and same-origin studio run by the container."""

import fcntl
import hashlib
import hmac
import ipaddress
import multiprocessing
import os
import re
import secrets
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from image_codec import MAX_UPLOAD_BYTES, decode_image
from provider import (
    IMAGE_ID,
    IMAGE_PATH,
    MODE_SWITCH_EXIT,
    AdmissionError,
    Store,
    VideoRequest,
    capabilities,
)
from worker import StopSignal, run_worker

ROOT = Path(__file__).resolve().parent


def validate_origin(origin):
    url = urlsplit(origin)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.path
        or url.query
        or url.fragment
        or url.username
    ):
        raise ValueError("Service origins must have no path, credentials or query.")
    try:
        addresses = {
            entry[4][0]
            for entry in socket.getaddrinfo(
                url.hostname, url.port or 80, type=socket.SOCK_STREAM
            )
        }
    except OSError as exc:
        raise ValueError("Configured service hostname cannot be resolved.") from exc
    if not addresses or any(
        not ipaddress.ip_address(address).is_private
        or ipaddress.ip_address(address).is_unspecified
        or ipaddress.ip_address(address).is_multicast
        for address in addresses
    ):
        raise ValueError(
            "Service origins must resolve only to private/loopback addresses."
        )


@dataclass
class Settings:
    token: str
    base_url: str = "http://127.0.0.1:8088"
    root: Path = ROOT / "state"
    queue_limit: int = 4
    retention_days: int = 3
    quota_bytes: int = 25 * 2**30
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self):
        if len(self.token) < 32 or not self.token.isascii():
            raise ValueError(
                "REEL_VIDEO_API_TOKEN must contain at least 32 ASCII characters."
            )
        for origin in (self.base_url, *self.allowed_origins):
            validate_origin(origin)
        hosts = [
            urlsplit(origin).netloc for origin in (self.base_url, *self.allowed_origins)
        ]
        if len(hosts) != len(set(hosts)):
            raise ValueError(
                "Configure one origin per Host; duplicate hosts are ambiguous."
            )
        if min(self.queue_limit, self.retention_days, self.quota_bytes) <= 0:
            raise ValueError("Queue, retention and quota settings must be positive.")

    @classmethod
    def from_env(cls):
        return cls(
            token=os.environ.get("REEL_VIDEO_API_TOKEN", ""),
            base_url=os.environ.get("REEL_VIDEO_BASE_URL", "http://127.0.0.1:8088"),
            root=Path(os.environ.get("REEL_VIDEO_STATE_DIR", ROOT / "state")),
            queue_limit=int(os.environ.get("REEL_VIDEO_QUEUE_LIMIT", "4")),
            retention_days=int(os.environ.get("REEL_VIDEO_RETENTION_DAYS", "3")),
            quota_bytes=int(os.environ.get("REEL_VIDEO_QUOTA_GIB", "25")) * 2**30,
            allowed_origins=tuple(
                value.strip()
                for value in os.environ.get("REEL_VIDEO_ALLOWED_ORIGINS", "").split(",")
                if value.strip()
            ),
        )


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=4096)


def create_app(settings=None, start_worker=True):
    settings = settings or Settings.from_env()
    store = Store(
        settings.root,
        settings.queue_limit,
        settings.retention_days,
        settings.quota_bytes,
    )
    context = multiprocessing.get_context("spawn")
    stop = threading.Event()
    decode_limiter = anyio.CapacityLimiter(2)
    upload_limiter = anyio.CapacityLimiter(2)

    def supervise():
        while not stop.is_set():
            store.worker_state("starting")
            worker_stop = StopSignal(context.RawValue("b", 0))
            process = context.Process(
                target=run_worker,
                args=(
                    str(settings.root),
                    settings.queue_limit,
                    settings.retention_days,
                    settings.quota_bytes,
                    worker_stop,
                    os.getpid(),
                ),
                daemon=True,
            )
            process.start()
            while process.is_alive() and not stop.wait(1):
                pass
            if stop.is_set():
                worker_stop.set()
                process.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                if process.is_alive():
                    process.kill()
            process.join()
            if process.exitcode == MODE_SWITCH_EXIT:
                # A deliberate handoff never claimed the next job. The old process
                # (and its CUDA/host memory) is gone before loading another graph.
                store.worker_state("switching")
                continue
            store.worker_state("unavailable")
            stop.wait(10)

    @asynccontextmanager
    async def lifespan(app):
        lock = (settings.root / "server.lock").open("a")
        thread = None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if start_worker:
                thread = threading.Thread(target=supervise, daemon=True)
                thread.start()
            yield
        finally:
            stop.set()
            if start_worker and thread:
                thread.join(15)
            lock.close()

    app = FastAPI(
        title="Reel Video API",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store
    origins = {
        urlsplit(origin).netloc: origin
        for origin in (settings.base_url, *settings.allowed_origins)
    }

    def request_origin(request):
        return origins[request.headers["host"]]

    @app.middleware("http")
    async def bounded_uploads(request, call_next):
        if request.method != "POST" or request.url.path != IMAGE_PATH.rstrip("/"):
            return await call_next(request)
        try:
            upload_limiter.acquire_nowait()
        except anyio.WouldBlock:
            return JSONResponse(
                {
                    "error": {
                        "code": "uploads_busy",
                        "message": "Two uploads are already active. Retry shortly.",
                    }
                },
                429,
                headers={"Retry-After": "2"},
            )
        try:
            return await call_next(request)
        finally:
            upload_limiter.release()

    @app.middleware("http")
    async def private_boundary(request, call_next):
        if request.headers.get("host") not in origins:
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_host",
                        "message": "Use the configured service origin.",
                    }
                },
                400,
            )
        origin = request.headers.get("origin")
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.cookies.get("reel_video_session")
            and not request.headers.get("authorization")
            and origin != request_origin(request)
        ):
            return JSONResponse(
                {
                    "error": {
                        "code": "csrf",
                        "message": "Session changes require a same-origin request.",
                    }
                },
                403,
            )
        if origin and origin != request_origin(request):
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_origin",
                        "message": "Cross-origin access is disabled.",
                    }
                },
                403,
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_origin",
                        "message": "Cross-site access is disabled.",
                    }
                },
                403,
            )
        upload = request.url.path == IMAGE_PATH.rstrip("/")
        if upload:
            # Reject unauthenticated uploads before buffering or decoding their body.
            try:
                authenticate(request)
            except HTTPException as exc:
                return JSONResponse(
                    {"error": {"code": "http_401", "message": exc.detail}},
                    401,
                    headers=exc.headers,
                )
        # Bound bodies before FastAPI reads them, including chunked uploads.
        if request.method in ("POST", "PUT", "PATCH") and not upload:
            limit = 64 * 1024
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "body_too_large",
                                "message": f"Maximum request size is {limit} bytes.",
                            }
                        },
                        413,
                    )
                body.extend(chunk)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def digest(value):
        return hmac.new(
            settings.token.encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    def authenticate(request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization:
            if hmac.compare_digest(
                authorization.encode(), ("Bearer " + settings.token).encode()
            ):
                return
        else:
            cookie = request.cookies.get("reel_video_session")
            if cookie:
                with store.connect() as db:
                    if db.execute(
                        "SELECT 1 FROM sessions WHERE digest=? AND expires>?",
                        (digest(cookie), time.time()),
                    ).fetchone():
                        return
        raise HTTPException(
            401,
            "A valid bearer token or studio session is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {"error": {"code": f"http_{exc.status_code}", "message": exc.detail}},
            exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo input values (especially login tokens) or internal exception contexts.
        messages = [
            ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors()
        ]
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "; ".join(messages)}}, 400
        )

    @app.exception_handler(AdmissionError)
    async def admission_error(request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            exc.status,
            headers={"Retry-After": "30"} if exc.status in (429, 503) else None,
        )

    app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")

    @app.get("/logo.png", response_class=FileResponse)
    def studio_logo():
        return FileResponse(ROOT / "logo.png", media_type="image/png")

    @app.get("/", response_class=HTMLResponse)
    def studio():
        return (ROOT / "studio.html").read_text()

    @app.post("/session")
    def login(request: Request, body: Login | None = None):
        # Studio is open to trusted network users; never expose the API credential.
        # Origin/Host protect browsers, not against clients forging headers. A
        # tokenless session grants shared access, so the firewall is the boundary.
        if body is None and request.headers.get("origin") != request_origin(request):
            raise HTTPException(403, "Studio sessions require a same-origin request.")
        if body is not None and not hmac.compare_digest(
            body.token.encode(), settings.token.encode()
        ):
            raise HTTPException(401, "Invalid token.")
        session = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute(
                "DELETE FROM sessions WHERE expires <= ? OR digest = ?",
                (time.time(), digest(request.cookies.get("reel_video_session", ""))),
            )
            db.execute(
                "INSERT INTO sessions VALUES(?,?)",
                (digest(session), time.time() + 12 * 3600),
            )
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            "reel_video_session",
            session,
            httponly=True,
            secure=request_origin(request).startswith("https:"),
            samesite="strict",
            max_age=12 * 3600,
        )
        return response

    @app.delete("/session")
    def logout(request: Request):
        with store.connect() as db:
            db.execute(
                "DELETE FROM sessions WHERE digest=?",
                (digest(request.cookies.get("reel_video_session", "")),),
            )
        response = Response(status_code=204)
        response.delete_cookie("reel_video_session")
        return response

    @app.get("/health", dependencies=[Depends(authenticate)])
    def health():
        state = store.worker_state()
        if time.time() - state["updated"] > 20:
            state["state"] = "unavailable"
        return {
            "http": "ready",
            "worker": state,
            "model_ready": state["state"] in ("ready", "generating", "encoding"),
        }

    @app.get("/api/v1/videos/models", dependencies=[Depends(authenticate)])
    def models():
        return capabilities()

    @app.post(
        "/api/v1/videos/images", status_code=201, dependencies=[Depends(authenticate)]
    )
    async def upload_image(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_UPLOAD_BYTES:
                raise AdmissionError(
                    "body_too_large", "Maximum image upload size is 8 MiB.", 413
                )
            body.extend(chunk)
        try:
            image = await anyio.to_thread.run_sync(
                decode_image,
                bytes(body),
                request.headers.get("content-type", ""),
                limiter=decode_limiter,
            )
        except ValueError as exc:
            raise HTTPException(400, f"Invalid image: {exc}") from None
        saved = await anyio.to_thread.run_sync(store.save_image, image)
        return saved | {"url": request_origin(request) + IMAGE_PATH + saved["id"]}

    @app.api_route(
        "/api/v1/videos/images/{image_id}",
        methods=["GET", "HEAD"],
        dependencies=[Depends(authenticate)],
    )
    def uploaded_image(image_id: str, request: Request):
        if not re.fullmatch(IMAGE_ID, image_id):
            raise HTTPException(404, "Unknown image ID.")
        image = store.image(image_id)
        return Response(
            content=image["data"] if request.method == "GET" else b"",
            media_type="image/png",
            headers={
                "Content-Length": str(len(image["data"])),
                "Content-Disposition": f'inline; filename="{image_id}.png"',
            },
        )

    @app.get("/api/v1/videos", dependencies=[Depends(authenticate)])
    def history(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return {
            "data": [
                store.public(row, request_origin(request))
                for row in store.recent(limit, offset)
            ]
        }

    @app.post("/api/v1/videos", status_code=202, dependencies=[Depends(authenticate)])
    def submit(body: VideoRequest, request: Request):
        key = request.headers.get("idempotency-key")
        if key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise HTTPException(
                400,
                "Idempotency-Key must contain 1–128 ASCII letters, digits, _, ., : or -.",
            )
        return store.public(
            store.submit(body, key, tuple(origins.values())), request_origin(request)
        )

    def find(job_id):
        row = store.get(job_id)
        if row is None:
            raise HTTPException(404, "Unknown job ID.")
        return row

    @app.get("/api/v1/videos/{job_id}", dependencies=[Depends(authenticate)])
    def poll(job_id: str, request: Request):
        return store.public(find(job_id), request_origin(request))

    @app.delete(
        "/api/v1/videos/{job_id}", status_code=204, dependencies=[Depends(authenticate)]
    )
    def delete_job(job_id: str):
        store.delete_job(job_id)
        return Response(status_code=204)

    @app.post(
        "/api/v1/videos/{job_id}/cancel",
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    def cancel_job(job_id: str, request: Request):
        return store.public(store.cancel_job(job_id), request_origin(request))

    @app.api_route(
        "/api/v1/videos/{job_id}/content",
        methods=["GET", "HEAD"],
        dependencies=[Depends(authenticate)],
    )
    def content(job_id: str, request: Request, index: int = 0):
        if index != 0:
            raise HTTPException(400, "Only content index 0 is supported.")
        row = find(job_id)
        if row["status"] != "completed":
            raise HTTPException(409, "Job has no completed content.")
        if row["expired"]:
            raise HTTPException(410, "Content expired; job metadata is retained.")
        try:
            stream = (store.root / "media" / row["id"] / "final.mp4").open("rb")
        except FileNotFoundError:
            raise HTTPException(
                410, "Content is unavailable; job metadata is retained."
            ) from None
        length = os.fstat(stream.fileno()).st_size
        start, end, status = 0, length - 1, 200
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{row["id"]}.mp4"',
        }
        value = request.headers.get("range")
        if value:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            try:
                if not match or not any(match.groups()):
                    raise ValueError
                left, right = match.groups()
                if left:
                    start, end = int(left), min(int(right), end) if right else end
                else:
                    suffix = int(right)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, length - suffix)
                if not 0 <= start <= end < length:
                    raise ValueError
            except ValueError:
                stream.close()
                raise HTTPException(
                    416,
                    "Invalid or unsupported byte range.",
                    headers={"Content-Range": f"bytes */{length}"},
                ) from None
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{length}"
        headers["Content-Length"] = str(end - start + 1)
        if request.method == "HEAD":
            stream.close()
            return Response(status_code=status, media_type="video/mp4", headers=headers)

        def chunks():
            try:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data
            finally:
                stream.close()

        return StreamingResponse(
            chunks(),
            status_code=status,
            media_type="video/mp4",
            headers=headers,
            background=BackgroundTask(stream.close),
        )

    return app


if __name__ == "__main__":
    import uvicorn

    configuration = Settings.from_env()
    origin = urlsplit(configuration.base_url)
    # HTTPS is terminated by the operator's private reverse proxy; bind upstream explicitly.
    bind = os.environ.get(
        "REEL_VIDEO_BIND", origin.hostname if origin.scheme == "http" else "127.0.0.1"
    )
    if bind != "localhost" and (
        not ipaddress.ip_address(bind).is_private
        or ipaddress.ip_address(bind).is_unspecified
    ):
        raise ValueError(
            "REEL_VIDEO_BIND must be an explicit private or loopback address, not a wildcard."
        )
    uvicorn.run(
        create_app(configuration),
        host=bind,
        port=int(os.environ.get("REEL_VIDEO_PORT", origin.port or 8088)),
        proxy_headers=False,
        access_log=False,
    )
