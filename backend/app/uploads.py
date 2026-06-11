"""Storage for task image attachments.

Images attached to a task are stored OUTSIDE the project repo (under
``data_dir/task_images/{task_id}/``) so the automatic commit never picks them
up.  The agent receives their absolute file paths appended to its prompt and
opens them with its own file/image reading tool (e.g. Claude Code's Read).
"""
from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path

from .config import get_settings

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # decoded size per image

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# e.g. "data:image/png;base64," — the browser's FileReader data-URL prefix.
_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,")


class ImageError(ValueError):
    """Invalid upload (format/size/encoding) — maps to HTTP 400."""


def task_image_dir(task_id: str) -> Path:
    return get_settings().data_dir / "task_images" / task_id


def media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _safe_name(name: str, index: int, used: set[str]) -> str:
    base = Path(name or "").name  # drop any client-sent path components
    ext = Path(base).suffix.lower()
    if ext not in _ALLOWED_EXT:
        raise ImageError(
            f"Nicht unterstütztes Bildformat: '{name or '(ohne Name)'}' "
            f"(erlaubt: {', '.join(sorted(e.lstrip('.') for e in _ALLOWED_EXT))})."
        )
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(base).stem).strip("._") or f"bild-{index + 1}"
    candidate = f"{stem}{ext}"
    n = 2
    while candidate in used:
        candidate = f"{stem}-{n}{ext}"
        n += 1
    used.add(candidate)
    return candidate


def decode_images(images) -> list[tuple[str, bytes]]:
    """Validate the uploaded images (name + base64/data-URL) and decode them.

    Returns ``[(safe_filename, raw_bytes), ...]``; raises :class:`ImageError`
    on any invalid entry so nothing is persisted for a bad request.
    """
    if len(images) > MAX_IMAGES:
        raise ImageError(f"Maximal {MAX_IMAGES} Bilder pro Aufgabe.")
    out: list[tuple[str, bytes]] = []
    used: set[str] = set()
    for i, img in enumerate(images):
        data = _DATA_URL_RE.sub("", (img.data or "").strip())
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:  # includes binascii.Error
            raise ImageError(f"Bild '{img.name}' ist kein gültiges Base64.")
        if not raw:
            raise ImageError(f"Bild '{img.name}' ist leer.")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageError(
                f"Bild '{img.name}' ist größer als {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
            )
        out.append((_safe_name(img.name, i, used), raw))
    return out


def save_images(task_id: str, decoded: list[tuple[str, bytes]]) -> list[str]:
    d = task_image_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for name, raw in decoded:
        (d / name).write_bytes(raw)
        names.append(name)
    return names


def image_paths(task_id: str, names: list[str]) -> list[str]:
    """Absolute paths of the stored images that still exist on disk."""
    d = task_image_dir(task_id)
    return [str((d / n).resolve()) for n in names if (d / n).exists()]


def delete_images(task_id: str) -> None:
    shutil.rmtree(task_image_dir(task_id), ignore_errors=True)
