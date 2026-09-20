"""Almacenamiento local de fotos de autorregistro (rostro y licencia).

No hay S3 ni Blobs en este backend: las fotos se guardan en UPLOAD_DIR con
nombre UUID (no adivinable) y se sirven por GET /api/v1/uploads/{filename}.
Solo `profile` y `license` — el tarjetón no existe como kind.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import settings

ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

UPLOAD_KINDS = frozenset({"profile", "license"})


def upload_dir() -> Path:
    path = Path(settings.UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_url(filename: str) -> str:
    return f"/api/v1/uploads/{filename}"


def resolve_filename(filename: str) -> Path:
    """Rechaza path traversal: el nombre tiene que ser un archivo plano
    dentro de UPLOAD_DIR."""
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nombre de archivo inválido")
    path = (upload_dir() / filename).resolve()
    if path.parent != upload_dir().resolve():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nombre de archivo inválido")
    return path


async def save_image(file: UploadFile) -> str:
    content_type = (file.content_type or "").lower()
    ext = ALLOWED_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Solo se aceptan imágenes JPEG, PNG o WebP",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El archivo está vacío")
    if len(data) > settings.UPLOAD_MAX_BYTES:
        raise HTTPException(
            413,
            "El archivo es demasiado grande",
        )

    filename = f"{uuid.uuid4().hex}{ext}"
    path = upload_dir() / filename
    path.write_bytes(data)
    return public_url(filename)
