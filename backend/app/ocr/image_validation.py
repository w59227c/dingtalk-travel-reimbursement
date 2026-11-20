"""Lightweight entry point imported by each isolated PNG/JPEG checker."""

from __future__ import annotations

import warnings
from os import O_NOFOLLOW, O_RDONLY, fdopen
from os import open as os_open
from typing import Any

from PIL import Image, UnidentifiedImageError

from app.core.exceptions import ApiError
from app.ocr.worker_limits import (
    apply_worker_limits,
    safe_worker_failure,
    worker_resource_failure,
)


def validate_image_worker(
    path: str,
    expected_extension: str,
    raw_image_limits: dict[str, int],
    raw_worker_limits: dict[str, int],
) -> dict[str, Any]:
    """Validate an untrusted image in the bounded native worker process."""

    def open_image():
        descriptor = os_open(path, O_RDONLY | O_NOFOLLOW)
        return fdopen(descriptor, "rb")

    try:
        apply_worker_limits(raw_worker_limits)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with open_image() as stream, Image.open(stream) as image:
                image.verify()
            with open_image() as stream, Image.open(stream) as image:
                width, height = image.size
                detected = (image.format or "").upper()
        if width < 1 or height < 1:
            raise ApiError("INVALID_IMAGE", "图片尺寸无效", 400)
        if (
            width > raw_image_limits["max_dimension"]
            or height > raw_image_limits["max_dimension"]
            or width * height > raw_image_limits["max_pixels"]
        ):
            raise ApiError("IMAGE_TOO_LARGE", "图片尺寸或像素超过限制", 400)
        expected_format = "JPEG" if expected_extension == "jpg" else "PNG"
        if detected != expected_format:
            raise ApiError("FILE_TYPE_MISMATCH", "文件扩展名与图片内容不一致", 400)
        return {"ok": True, "width": width, "height": height}
    except MemoryError:
        return worker_resource_failure()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return safe_worker_failure(ApiError("IMAGE_TOO_LARGE", "图片像素过大", 400))
    except (UnidentifiedImageError, OSError, ValueError):
        return safe_worker_failure(ApiError("INVALID_IMAGE", "图片文件损坏或格式无效", 400))
    except ApiError as exc:
        return safe_worker_failure(exc)
