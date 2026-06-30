from __future__ import annotations

import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from starlette.datastructures import UploadFile

from app.app_settings import EnvSettings
from app.core.models import AttachmentScanStatus
from app.services.antivirus import AntivirusFileInfectedError
from app.services.antivirus import ClamAVScanner
from app.services.antivirus import scan_upload_non_blocking

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ATTACHMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_CHUNK_SIZE = 1024 * 1024


def _safe_filename(filename: str) -> str:
    name = Path(filename or "upload.bin").name.strip() or "upload.bin"
    safe = _SAFE_NAME_RE.sub("_", name)
    return safe[:180] or "upload.bin"


def _safe_app_code(app_code: str) -> str:
    safe = _SAFE_NAME_RE.sub("_", str(app_code or "default").strip())
    return safe[:80] or "default"


def _attachment_dir(settings: EnvSettings, app_code: str) -> Path:
    return settings.upload_root / _safe_app_code(app_code) / "attachments"


def _metadata_path(root: Path, attachment_id: str) -> Path:
    return root / f"{attachment_id}.json"


def _validate_attachment_id(attachment_id: str) -> str:
    normalized = str(attachment_id or "").strip().lower()
    if not _ATTACHMENT_ID_RE.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )
    return normalized


async def read_upload_content(
    upload: UploadFile,
    *,
    max_bytes: int,
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File exceeds configured upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def first_upload_file(request: Request) -> UploadFile:
    form = await request.form()
    for _, value in form.multi_items():
        if isinstance(value, UploadFile):
            return value
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="Missing file upload",
    )


async def save_formio_attachment(
    *,
    request: Request,
    settings: EnvSettings,
    app_code: str,
) -> dict[str, Any]:
    upload = await first_upload_file(request)
    filename = _safe_filename(upload.filename or "")
    content = await read_upload_content(
        upload,
        max_bytes=settings.max_upload_size_bytes,
    )

    try:
        scan = await scan_upload_non_blocking(ClamAVScanner(settings), content)
    except AntivirusFileInfectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File infected: {exc.signature}",
        ) from exc
    if (
        scan.status == AttachmentScanStatus.ERROR
        and settings.clamav_fail_closed
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=scan.signature or "Antivirus scan unavailable",
        )

    root = _attachment_dir(settings, app_code)
    root.mkdir(parents=True, exist_ok=True)
    attachment_id = uuid.uuid4().hex
    suffix = Path(filename).suffix
    stored_name = f"{attachment_id}{suffix}"
    file_path = root / stored_name
    file_path.write_bytes(content)

    content_type = (
        upload.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    url = f"/client/attachment/{attachment_id}"
    metadata = {
        "id": attachment_id,
        "storage": "url",
        "name": filename,
        "originalName": filename,
        "size": len(content),
        "type": content_type,
        "url": url,
        "path": url,
        "scan": {
            "status": str(scan.status),
            "signature": scan.signature,
            "engine": scan.engine,
            "scanned_at": scan.scanned_at.isoformat(),
        },
        "_stored_name": stored_name,
    }
    _metadata_path(root, attachment_id).write_text(
        json.dumps(metadata, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return {key: value for key, value in metadata.items() if key != "_stored_name"}


def load_attachment_metadata(
    *,
    settings: EnvSettings,
    app_code: str,
    attachment_id: str,
) -> tuple[Path, dict[str, Any]]:
    normalized_id = _validate_attachment_id(attachment_id)
    root = _attachment_dir(settings, app_code)
    metadata_file = _metadata_path(root, normalized_id)
    if not metadata_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    stored_name = str(metadata.get("_stored_name") or "")
    file_path = root / Path(stored_name).name
    if not stored_name or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment file not found",
        )
    return file_path, metadata


def load_record_attachment_file(
    *,
    settings: EnvSettings,
    model: str,
    rec_name: str,
    filename: str,
) -> tuple[Path, dict[str, Any]]:
    safe_filename = Path(filename or "").name
    if not model or not rec_name or not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )

    root = settings.upload_root.resolve()
    file_path = (root / model / rec_name / safe_filename).resolve()
    try:
        file_path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        ) from exc

    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found",
        )

    content_type = (
        mimetypes.guess_type(safe_filename)[0]
        or "application/octet-stream"
    )
    return file_path, {
        "name": safe_filename,
        "type": content_type,
        "storage": "url",
        "model": model,
        "rec_name": rec_name,
    }


def delete_attachment(
    *,
    settings: EnvSettings,
    app_code: str,
    attachment_id: str,
) -> dict[str, Any]:
    file_path, metadata = load_attachment_metadata(
        settings=settings,
        app_code=app_code,
        attachment_id=attachment_id,
    )
    metadata_file = _metadata_path(
        _attachment_dir(settings, app_code),
        str(metadata["id"]),
    )
    file_path.unlink(missing_ok=True)
    metadata_file.unlink(missing_ok=True)
    return {"deleted": True, "id": metadata["id"]}
