# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Durable local video contract and storage. No inference imports in HTTP processes."""

import json
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from image_codec import (
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_UPLOAD_BYTES,
    DecodedImage,
)

MODEL = "local/minimax-h3"
SIZES = ["960x544", "544x960", "768x768"]
DURATIONS = [5, 10]
IMAGE_PATH = "/api/v1/videos/images/"
IMAGE_ID = r"image-[a-f0-9]{64}"
UPLOAD_TTL = 86400
MAX_STORED_IMAGES = 128
MAX_STORED_IMAGE_BYTES = 256 * 2**20
MAX_REFERENCES = 2
REFERENCE_TOKEN_BUDGET = 15000
MODE_SWITCH_EXIT = 75


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class ImageReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


class FrameImage(ImageReference):
    frame_type: Literal["first_frame", "last_frame"]


class InferenceParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    num_inference_steps: StrictInt = Field(default=50, ge=2, le=100)


class LocalProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: InferenceParameters = Field(default_factory=InferenceParameters)


class ProviderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local: LocalProvider = Field(default_factory=LocalProvider)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: ProviderOptions = Field(default_factory=ProviderOptions)


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export: Literal["original", "1080p"] = "original"


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["local/minimax-h3"]
    prompt: str = Field(min_length=1, max_length=8000)
    duration: StrictInt = 5
    size: str = "960x544"
    resolution: str | None = None
    aspect_ratio: str | None = None
    seed: StrictInt = Field(default=42, ge=0, lt=2**63)
    generate_audio: StrictBool = True
    local: ExportOptions = Field(default_factory=ExportOptions)
    frame_images: list[FrameImage] = Field(default_factory=list, max_length=2)
    input_references: list[ImageReference] = Field(
        default_factory=list, max_length=MAX_REFERENCES
    )
    provider: ProviderConfig = Field(default_factory=ProviderConfig)

    @property
    def workflow(self):
        return (
            "ref2va"
            if self.input_references
            else "fl2va"
            if self.frame_images
            else "t2va"
        )

    @property
    def images(self):
        return [*self.frame_images, *self.input_references]

    @property
    def steps(self):
        return self.provider.options.local.parameters.num_inference_steps

    @property
    def worker_profile(self):
        if not self.input_references:
            return self.workflow
        # Reference conditioning nearly fills the GPU. Changing its allocation
        # shapes after a warm job can OOM despite the same request passing cold.
        # Include conditioning identity/text conservatively; seed, steps and
        # delivery export do not change these shapes and may reuse the pipeline.
        return self.model_dump_json(
            include={"prompt", "size", "duration", "input_references"}
        )

    @model_validator(mode="after")
    def supported(self):
        if self.frame_images and self.input_references:
            raise ValueError("Choose frame_images OR input_references, not both.")
        roles = [image.frame_type for image in self.frame_images]
        if len(roles) != len(set(roles)):
            raise ValueError("Supply at most one image for each first/last frame role.")
        if not self.prompt.strip():
            raise ValueError("Prompt must not be empty.")
        if self.duration not in DURATIONS or self.size not in SIZES:
            raise ValueError(f"Supported durations: {DURATIONS}; sizes: {SIZES}.")
        # These canvases have no truthful standard resolution/aspect-ratio label.
        if self.resolution is not None or self.aspect_ratio is not None:
            raise ValueError(
                f"Use exact size, one of {SIZES}; omit resolution/aspect_ratio."
            )
        return self


def capabilities():
    return {
        "data": [
            {
                "id": MODEL,
                "canonical_slug": MODEL,
                "name": "MiniMax H3 · local RTX 5090",
                "supported_durations": DURATIONS,
                "supported_sizes": SIZES,
                "supported_resolutions": [],
                "supported_aspect_ratios": [],
                "allowed_passthrough_parameters": ["num_inference_steps"],
                "pricing_skus": {},
                "local": {
                    "metered_charge": False,
                    "input_modes": ["text", "frames", "references"],
                    "steps": {
                        "provider": "local",
                        "parameter": "num_inference_steps",
                        "min": 2,
                        "max": 100,
                        "default": 50,
                        "unit": "sigma_grid_points",
                    },
                    "images": {
                        "upload_url": IMAGE_PATH.rstrip("/"),
                        "mime_types": ["image/png", "image/jpeg", "image/webp"],
                        "max_upload_bytes": MAX_UPLOAD_BYTES,
                        "max_pixels": MAX_IMAGE_PIXELS,
                        "max_dimension": MAX_IMAGE_DIMENSION,
                        "max_frame_images": 2,
                        "max_input_references": MAX_REFERENCES,
                        "reference_aspect_ratio": {"min": 0.5, "max": 2},
                        "reference_short_edge": 2048,
                        "reference_conditioning_budget": {
                            "limit": REFERENCE_TOKEN_BUDGET,
                            "unit": "conservative_qwen_tokens",
                            "image_cost": "64 * ceil(64 * max(width,height) / min(width,height)) + 16",
                            "prompt_cost": "utf8_bytes",
                        },
                        "unbound_ttl_seconds": UPLOAD_TTL,
                        "transport": "authenticated_local_upload_url_only",
                    },
                    "exports": ["original", "1080p"],
                    "fit_policy": "pad",
                    "audio": "optional_delivery",
                    "fps": 24,
                    "duration_frames": {"5": 124, "10": 243},
                    "defaults": {"duration": 5, "size": "960x544"},
                    "content_auth": "bearer_or_session",
                    "single_operator": True,
                },
            }
        ],
    }


class AdmissionError(Exception):
    def __init__(self, code, message, status=503):
        self.code, self.message, self.status = code, message, status


class WorkflowSwitch(Exception):
    """The next job stays pending while a fresh GPU worker loads its workflow."""


class JobCancelled(Exception):
    """The operator requested cancellation at a safe worker boundary."""


class ImagePaths(TypedDict):
    first_frame: Path | None
    last_frame: Path | None
    reference_image: list[Path]


class Store:
    def __init__(
        self, root: Path, queue_limit=4, retention_days=3, quota_bytes=25 * 2**30
    ):
        self.root = root
        self.queue_limit = queue_limit
        self.retention_seconds = retention_days * 86400
        self.quota_bytes = quota_bytes
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "media").mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL,
                    idem TEXT UNIQUE, status TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, completed REAL,
                    phase TEXT, metadata TEXT, error TEXT, expired INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS worker (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    state TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY, data BLOB, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_images (
                    job_id TEXT NOT NULL, image_id TEXT NOT NULL,
                    PRIMARY KEY(job_id,image_id)
                );
                CREATE INDEX IF NOT EXISTS job_images_image ON job_images(image_id);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "deleted" not in columns:
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
                )
            if "progress" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN progress TEXT")
            if "cancel_requested" not in columns:
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, job_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def recent(self, limit=50, offset=0):
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM jobs WHERE deleted=0 ORDER BY created DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

    def save_image(self, image: DecodedImage):
        """One durable SQLite transaction; no orphan files or uncommitted upload URLs."""
        image_id = "image-" + image.sha256
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT length(data) AS size FROM images WHERE id=?", (image_id,)
            ).fetchone()
            if not old or old["size"] is None:
                count, size = db.execute(
                    "SELECT count(data),coalesce(sum(length(data)),0) FROM images"
                ).fetchone()
                if (
                    count >= MAX_STORED_IMAGES
                    or size + len(image.data) > MAX_STORED_IMAGE_BYTES
                    or self._media_bytes() + size + len(image.data) > self.quota_bytes
                    or shutil.disk_usage(self.root).free < 2 * 2**30 + len(image.data)
                ):
                    raise AdmissionError(
                        "image_storage_full",
                        "Image storage is full. Wait for provider retention cleanup.",
                    )
            now = time.time()
            if old and old["size"] is not None:
                db.execute(
                    "UPDATE images SET expires=? WHERE id=?",
                    (now + UPLOAD_TTL, image_id),
                )
            else:
                db.execute(
                    "INSERT INTO images VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,expires=excluded.expires",
                    (
                        image_id,
                        image.data,
                        image.width,
                        image.height,
                        now,
                        now + UPLOAD_TTL,
                    ),
                )
            return {
                "id": image_id,
                "width": image.width,
                "height": image.height,
                "expires_at": now + UPLOAD_TTL,
            }

    def _image(self, db, image_id):
        row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None:
            raise AdmissionError(
                "unknown_image",
                "Upload the image to this service before submitting a job.",
                404,
            )
        if row["data"] is None or (
            row["expires"] <= time.time() and not self._image_pinned(db, image_id)
        ):
            raise AdmissionError(
                "image_expired",
                "Image expired. Upload it again before creating a new job.",
                410,
            )
        return row

    def _image_pinned(self, db, image_id):
        return (
            db.execute(
                "SELECT 1 FROM job_images ji JOIN jobs j ON j.id=ji.job_id WHERE ji.image_id=? AND j.deleted=0 AND (j.status IN ('pending','in_progress') OR j.completed>?) LIMIT 1",
                (image_id, time.time() - self.retention_seconds),
            ).fetchone()
            is not None
        )

    def image(self, image_id):
        with self.connect() as db:
            # Serialize retrieval/admission/cleanup. The returned bytes stay valid after expiry.
            db.execute("BEGIN IMMEDIATE")
            return self._image(db, image_id)

    def submit(self, request: VideoRequest, key: str | None, origins=()):
        request = request.model_copy(deep=True)
        for image in request.images:
            try:
                url = urlsplit(image.image_url.url)
            except ValueError:
                raise AdmissionError(
                    "invalid_image_url", "Use a valid local upload URL.", 400
                ) from None
            if (
                url.query
                or url.fragment
                or url.username
                or (url.netloc and f"{url.scheme}://{url.netloc}" not in origins)
                or (url.scheme and not url.netloc)
                or not re.fullmatch(IMAGE_PATH + IMAGE_ID, url.path)
            ):
                raise AdmissionError(
                    "invalid_image_url",
                    "Use an image URL returned by POST /api/v1/videos/images. Remote URLs, data URLs and filesystem paths are not accepted.",
                    400,
                )
            image.image_url.url = url.path
        payload = request.model_dump_json()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                old = db.execute("SELECT * FROM jobs WHERE idem=?", (key,)).fetchone()
                if old:
                    # Preserve idempotency for pre-image production jobs whose stored JSON
                    # predates the newly defaulted fields.
                    if (
                        VideoRequest.model_validate_json(
                            old["request"]
                        ).model_dump_json()
                        != payload
                    ):
                        raise AdmissionError(
                            "idempotency_conflict",
                            "Key already used with different settings.",
                            409,
                        )
                    if old["deleted"]:
                        raise AdmissionError(
                            "job_deleted",
                            "This job was deleted. Use a new key only to explicitly create a new job.",
                            410,
                        )
                    return old
            image_ids = [
                image.image_url.url.removeprefix(IMAGE_PATH) for image in request.images
            ]
            conditioning_cost = len(request.prompt.encode("utf-8"))
            for image_id in image_ids:
                image = self._image(db, image_id)
                if (
                    request.input_references
                    and not 0.5 <= image["width"] / image["height"] <= 2
                ):
                    raise AdmissionError(
                        "unsupported_reference_aspect",
                        "General references must have aspect ratios between 1:2 and 2:1. Crop the image before upload.",
                        400,
                    )
                if request.input_references:
                    # Native 2048 short edge, 32-pixel merged Qwen patches. Round
                    # upward (upstream rounds nearest), and count one text token
                    # per UTF-8 byte plus label/vision-boundary overhead. No
                    # tokenizer/model load or silent reference downsampling here.
                    ratio = max(image["width"], image["height"]) / min(
                        image["width"], image["height"]
                    )
                    conditioning_cost += 64 * math.ceil(64 * ratio) + 16
            if request.input_references and conditioning_cost > REFERENCE_TOKEN_BUDGET:
                raise AdmissionError(
                    "reference_conditioning_too_large",
                    f"References and prompt exceed the local conditioner memory budget ({conditioning_cost} > {REFERENCE_TOKEN_BUDGET}). Shorten the prompt, crop references toward square, or remove a reference.",
                    400,
                )
            pending = db.execute(
                "SELECT count(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0]
            if pending >= self.queue_limit:
                raise AdmissionError(
                    "queue_full",
                    "Waiting queue is full. Retry later with the same key.",
                    429,
                )
            # Leave substantial headroom for active encoding; quota is a high-water mark.
            used = self._media_bytes()
            used += db.execute(
                "SELECT coalesce(sum(length(data)),0) FROM images"
            ).fetchone()[0]
            if (
                used >= self.quota_bytes
                or shutil.disk_usage(self.root).free < 2 * 2**30
            ):
                raise AdmissionError(
                    "storage_full",
                    "Output storage is full. Free space or wait for retention cleanup.",
                )
            job_id = "video-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs(id,request,idem,status,created,phase) VALUES(?,?,?,'pending',?,'queued')",
                (job_id, payload, key, time.time()),
            )
            db.executemany(
                "INSERT OR IGNORE INTO job_images VALUES(?,?)",
                [(job_id, image_id) for image_id in image_ids],
            )
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def _media_bytes(self):
        used = 0
        for path in (self.root / "media").glob("*/*"):
            try:
                used += path.stat().st_size
            except FileNotFoundError:
                # Publication and cleanup may race with this admission high-water check.
                continue
        return used

    def materialize_images(self, request, folder):
        """Worker-owned execution copies; canonical uploads remain pinned in SQLite."""
        paths: ImagePaths = {
            "first_frame": None,
            "last_frame": None,
            "reference_image": [],
        }
        provenance = []
        with self.connect() as db:
            for index, image in enumerate(request.images):
                image_id = image.image_url.url.removeprefix(IMAGE_PATH)
                if not re.fullmatch(IMAGE_ID, image_id):
                    raise ValueError("Invalid persisted image identifier.")
                row = self._image(db, image_id)
                path = folder / f"input-{index}.png"
                with path.open("xb") as output:
                    output.write(row["data"])
                    output.flush()
                    os.fsync(output.fileno())
                role = (
                    image.frame_type if isinstance(image, FrameImage) else "reference"
                )
                provenance.append(
                    {
                        "id": image_id,
                        "role": role,
                        "index": index,
                        "width": row["width"],
                        "height": row["height"],
                    }
                )
                if isinstance(image, FrameImage):
                    paths[image.frame_type] = path
                else:
                    paths["reference_image"].append(path)
        return paths, provenance

    def delete_job(self, job_id):
        """Serialize deletion with claim; retain a tombstone for idempotency safety."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise AdmissionError("unknown_job", "Unknown job ID.", 404)
            if row["deleted"]:
                return
            if row["status"] not in ("pending", "failed", "completed"):
                raise AdmissionError(
                    "job_not_deletable",
                    "Only pending, failed, or completed jobs can be deleted. Active jobs are protected.",
                    409,
                )
            db.execute(
                "UPDATE jobs SET deleted=1,status='failed',phase='deleted',completed=?,error=? WHERE id=?",
                (
                    time.time(),
                    json.dumps(
                        {
                            "code": "job_deleted",
                            "message": "Deleted by the operator; retained media was removed and this job will not execute.",
                        }
                    ),
                    job_id,
                ),
            )
        # The transaction hides the job before deleting its files. Open download
        # descriptors remain readable on Linux; retention cleanup retries failures.
        shutil.rmtree(self.root / "media" / job_id, ignore_errors=True)

    def cancel_job(self, job_id):
        """Durably request cooperative cancellation of an active GPU job."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise AdmissionError("unknown_job", "Unknown job ID.", 404)
            if row["deleted"]:
                raise AdmissionError("job_deleted", "This job was deleted.", 410)
            if row["cancel_requested"]:
                return row
            if row["status"] != "in_progress":
                raise AdmissionError(
                    "job_not_cancelable", "Only an active job can be canceled.", 409
                )
            db.execute(
                "UPDATE jobs SET cancel_requested=1,phase='canceling' WHERE id=?",
                (job_id,),
            )
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def raise_if_cancelled(self, job_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        if row and row["cancel_requested"]:
            raise JobCancelled()

    def claim(self, profile=None):
        # Reserve the write transaction before selecting: claiming the oldest job
        # and marking it in progress must be atomic, including profile handoffs.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT * FROM jobs WHERE status='pending' ORDER BY created LIMIT 1"
            ).fetchone()
            if job:
                if (
                    profile
                    and VideoRequest.model_validate_json(job["request"]).worker_profile
                    != profile
                ):
                    raise WorkflowSwitch()
                db.execute(
                    "UPDATE jobs SET status='in_progress',started=?,phase='loading' WHERE id=?",
                    (time.time(), job["id"]),
                )
            return job

    def phase(self, job_id, phase):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET phase=? WHERE id=? AND cancel_requested=0",
                (phase, job_id),
            )

    def progress(self, job_id, completed, total):
        if not 0 <= completed <= total or total <= 0:
            raise ValueError("Invalid denoising step progress.")
        with self.connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row and row["cancel_requested"]:
                raise JobCancelled()
            db.execute(
                "UPDATE jobs SET progress=?,phase=? WHERE id=? AND status='in_progress' AND cancel_requested=0",
                (
                    json.dumps(
                        {
                            "unit": "denoising_steps",
                            "completed": completed,
                            "total": total,
                            "updated_at": time.time(),
                        }
                    ),
                    "decoding" if completed == total else "denoising",
                    job_id,
                ),
            )

    def finish(self, job_id, metadata=None, error=None):
        # Cancellation wins a race with completion. The caller must discard its
        # output when this conditional update returns False.
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET status=?,completed=?,phase=?,metadata=?,error=? WHERE id=? AND cancel_requested=0",
                (
                    "failed" if error else "completed",
                    time.time(),
                    "failed" if error else "ready",
                    json.dumps(metadata) if metadata else None,
                    json.dumps(error) if error else None,
                    job_id,
                ),
            )
            return result.rowcount == 1

    def finish_cancelled(self, job_id):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',completed=?,phase='canceled',metadata=NULL,error=? WHERE id=?",
                (
                    time.time(),
                    json.dumps(
                        {
                            "code": "job_canceled",
                            "message": "Canceled by the operator before completion.",
                        }
                    ),
                    job_id,
                ),
            )

    def reconcile(self):
        """Only call after exclusive worker ownership, never alongside live generation."""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',completed=?,phase='canceled',metadata=NULL,error=? WHERE status='in_progress' AND cancel_requested=1",
                (
                    time.time(),
                    json.dumps(
                        {
                            "code": "job_canceled",
                            "message": "Canceled by the operator before completion.",
                        }
                    ),
                ),
            )
            db.execute(
                "UPDATE jobs SET status='failed',completed=?,phase='interrupted',error=? WHERE status='in_progress' AND cancel_requested=0",
                (
                    time.time(),
                    json.dumps(
                        {
                            "code": "worker_interrupted",
                            "message": "Worker stopped before completion. Reuse settings to create a new job.",
                        }
                    ),
                ),
            )

    def worker_state(self, state=None):
        with self.connect() as db:
            if state:
                db.execute(
                    "INSERT OR REPLACE INTO worker VALUES(1,?,?)", (state, time.time())
                )
            row = db.execute("SELECT * FROM worker WHERE singleton=1").fetchone()
            return dict(row) if row else {"state": "unavailable", "updated": 0}

    def cleanup(self):
        # The worker invokes this sweep; expiry is not a wall-clock deletion
        # guarantee while it is stopped. Keep job/idempotency history, not media.
        # Open download fds survive unlink on Linux.
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM jobs WHERE status='failed' OR (status='completed' AND completed<? AND expired=0)",
                (now - self.retention_seconds,),
            ).fetchall()
            for row in rows:
                shutil.rmtree(self.root / "media" / row["id"], ignore_errors=True)
                if row["status"] == "completed":
                    db.execute("UPDATE jobs SET expired=1 WHERE id=?", (row["id"],))
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            for image in db.execute(
                "SELECT id FROM images WHERE expires<=? AND data IS NOT NULL", (now,)
            ).fetchall():
                if not self._image_pinned(db, image["id"]):
                    # Tombstones distinguish expiration from unknown IDs; freed BLOB pages
                    # are reusable, bounding retained payload without a blocking VACUUM.
                    db.execute("UPDATE images SET data=NULL WHERE id=?", (image["id"],))

    def public(self, row, base):
        available = row["status"] == "completed" and not row["expired"]
        available = (
            available and (self.root / "media" / row["id"] / "final.mp4").is_file()
        )
        url = f"{base}/api/v1/videos/{row['id']}"
        result = {
            "id": row["id"],
            "polling_url": url,
            "status": row["status"],
            "local": {
                "created_at": row["created"],
                "started_at": row["started"],
                "completed_at": row["completed"],
                "phase": row["phase"],
                "deleted": bool(row["deleted"]),
                "progress": json.loads(row["progress"]) if row["progress"] else None,
                "request": json.loads(row["request"]),
                "workflow": VideoRequest.model_validate_json(row["request"]).workflow,
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
                "content_expired": row["status"] == "completed" and not available,
            },
        }
        if available:
            result["unsigned_urls"] = [url + "/content?index=0"]
        if row["error"]:
            result["error"] = json.loads(row["error"])
        return result
