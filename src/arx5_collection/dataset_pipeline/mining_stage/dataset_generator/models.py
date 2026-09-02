from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VideoEncodingConfig:
    codec: str = "libsvtav1"
    pixel_format: str = "yuv420p"
    gop: int = 2
    crf: int = 30
    preset: int = 8
    threads: int = 0

    def __post_init__(self) -> None:
        if self.codec != "libsvtav1":
            raise ValueError("video codec must be libsvtav1")
        if self.pixel_format != "yuv420p":
            raise ValueError("video pixel_format must be yuv420p")
        if self.gop < 1:
            raise ValueError("video gop must be positive")
        if not 0 <= self.crf <= 63:
            raise ValueError("video crf must be between 0 and 63")
        if not 0 <= self.preset <= 13:
            raise ValueError("video preset must be between 0 and 13")
        if self.threads < 0:
            raise ValueError("video threads must be non-negative")

    def as_report(self) -> dict[str, object]:
        return asdict(self)
