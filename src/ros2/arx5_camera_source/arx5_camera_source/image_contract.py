from __future__ import annotations

import math


RGB8_ENCODING = "rgb8"
RGB8_BYTES_PER_PIXEL = 3


def timestamp_parts(timestamp_ms: float) -> tuple[int, int]:
    if not math.isfinite(timestamp_ms) or timestamp_ms < 0:
        raise ValueError("frame timestamp must be a finite non-negative value")
    timestamp_ns = int(timestamp_ms * 1_000_000)
    return divmod(timestamp_ns, 1_000_000_000)


def validate_image_buffer(
    width: int,
    height: int,
    step: int,
    payload_size: int,
    bytes_per_pixel: int,
) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    if step < width * bytes_per_pixel:
        raise ValueError("image stride is smaller than the declared encoding")
    if payload_size != step * height:
        raise ValueError("image payload size does not match stride and height")
