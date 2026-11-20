"""FastAPI service: unpack a single-manifest OCI Image Layout tar into rootfs.tar."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .sample_image import build_sample_image
from .unpacker import CHUNK, UnpackError, unpack_image
from .volume_seed import build_volume_seed

logger = logging.getLogger("oci-unpacker")

WORK_ROOT = os.environ.get(
    "UNPACK_WORKDIR", os.path.join(tempfile.gettempdir(), "oci-unpacker")
)
SAMPLE_IMAGE_PATH = os.path.join(WORK_ROOT, "sample-image.tar")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(WORK_ROOT, exist_ok=True)
    # Drop leftovers from previous runs so a crash never leaks state between
    # requests.
    for name in os.listdir(WORK_ROOT):
        if name.startswith("req-"):
            shutil.rmtree(os.path.join(WORK_ROOT, name), ignore_errors=True)
    build_sample_image(SAMPLE_IMAGE_PATH)
    yield


app = FastAPI(
    title="OCI Image Unpacker",
    version="1.0.0",
    description=(
        "Upload an OCI Image Layout tar containing exactly one manifest and "
        "get the reconstructed `rootfs.tar` back.\n\n"
        "* verifies blob presence, size and SHA-256, plus config `diff_ids`;\n"
        "* supports plain and gzip layers;\n"
        "* whiteouts / opaque markers only delete lower-layer content;\n"
        "* hardlink overwrite is byte-safe for the other end;\n"
        "* paths and links can never escape the per-request sandbox;\n"
        "* any missing/corrupt/illegal input fails the whole request.\n\n"
        "Try it: download `GET /sample-image`, then upload it to `POST /unpack` "
        "or `POST /volume-seed`."
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "oci-image-unpacker",
        "docs": "/docs",
        "endpoints": {
            "unpack": "POST /unpack",
            "volume_seed": "POST /volume-seed",
            "sample_image": "GET /sample-image",
            "healthz": "GET /healthz",
        },
    }


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get(
    "/sample-image",
    summary="Download a sample OCI Image Layout tar",
    response_description="sample-image.tar -- a single-manifest OCI Image Layout "
    "with deletions, an opaque directory, a hardlink overwrite and two nested "
    "volumes with a hardlink crossing their boundary",
    responses={200: {"content": {"application/x-tar": {}}}},
)
def sample_image():
    if not os.path.isfile(SAMPLE_IMAGE_PATH):
        raise HTTPException(status_code=404, detail="sample image not available")
    return FileResponse(
        SAMPLE_IMAGE_PATH, media_type="application/x-tar", filename="sample-image.tar"
    )


@app.post(
    "/unpack",
    summary="Unpack an OCI Image Layout tar into rootfs.tar",
    response_description="The reconstructed root filesystem as rootfs.tar",
    responses={
        200: {"content": {"application/x-tar": {}}},
        400: {
            "description": "Missing blobs, corrupt data, digest/diff_id mismatch "
            "or illegal paths -- nothing is returned."
        },
    },
)
def unpack(
    file: UploadFile = File(
        ..., description="OCI Image Layout tar containing exactly one manifest"
    ),
):
    # Every request gets its own scratch directory: requests never interfere
    # with each other, and a failed request leaves nothing behind.
    work = tempfile.mkdtemp(prefix="req-", dir=WORK_ROOT)
    try:
        upload_path = os.path.join(work, "upload.tar")
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out, CHUNK)
        rootfs_tar = unpack_image(upload_path, work)
    except UnpackError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        logger.exception("unexpected error while unpacking")
        raise HTTPException(status_code=500, detail="internal error")
    return FileResponse(
        rootfs_tar,
        media_type="application/x-tar",
        filename="rootfs.tar",
        background=BackgroundTask(partial(shutil.rmtree, work, ignore_errors=True)),
    )


@app.post(
    "/volume-seed",
    summary="Unpack an OCI Image Layout tar and seed its declared volumes",
    response_description=(
        "seed-bundle.tar containing rootfs.tar, one seed tar per declared "
        "volume (config.Volumes) and mounts.json"
    ),
    responses={
        200: {"content": {"application/x-tar": {}}},
        400: {
            "description": "Missing blobs, corrupt data, digest/diff_id "
            "mismatch, illegal paths or an invalid declared volume -- nothing "
            "is returned."
        },
    },
)
def volume_seed(
    file: UploadFile = File(
        ..., description="OCI Image Layout tar containing exactly one manifest"
    ),
):
    work = tempfile.mkdtemp(prefix="req-", dir=WORK_ROOT)
    try:
        upload_path = os.path.join(work, "upload.tar")
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out, CHUNK)
        bundle = build_volume_seed(upload_path, work, os.path.join(work, "bundle.tar"))
    except UnpackError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        logger.exception("unexpected error while seeding volumes")
        raise HTTPException(status_code=500, detail="internal error")
    return FileResponse(
        bundle,
        media_type="application/x-tar",
        filename="seed-bundle.tar",
        background=BackgroundTask(partial(shutil.rmtree, work, ignore_errors=True)),
    )
