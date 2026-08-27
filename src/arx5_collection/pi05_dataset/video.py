from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
import time


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


PhaseReporter = Callable[[str, float], None]


@contextmanager
def configured_lerobot_encoder(
    config: VideoEncodingConfig | None,
    reporter: PhaseReporter | None = None,
) -> Iterator[None]:
    """Temporarily bind LeRobot's episode writer to an explicit SVT policy.

    LeRobot 2.1 does not expose encoder options through ``LeRobotDataset``.
    Each streaming worker is a separate process and exports one episode at a
    time, so this process-local binding stays isolated and is always restored.
    """

    if config is None:
        yield
        return

    from lerobot.common.datasets import lerobot_dataset

    original = lerobot_dataset.encode_video_frames

    def encode(
        imgs_dir: Path | str,
        video_path: Path | str,
        fps: int,
        **kwargs: object,
    ) -> None:
        started = time.monotonic()
        try:
            encode_svtav1_frames(
                imgs_dir,
                video_path,
                fps,
                config,
                overwrite=bool(kwargs.get("overwrite", False)),
            )
        finally:
            if reporter is not None:
                reporter("video_encode", max(time.monotonic() - started, 0.0))

    lerobot_dataset.encode_video_frames = encode
    try:
        yield
    finally:
        lerobot_dataset.encode_video_frames = original


def encode_svtav1_frames(
    images_dir: Path | str,
    video_path: Path | str,
    fps: int,
    config: VideoEncodingConfig,
    *,
    overwrite: bool = False,
) -> None:
    import av
    from PIL import Image

    images_dir = Path(images_dir)
    video_path = Path(video_path)
    if video_path.exists() and not overwrite:
        raise FileExistsError(video_path)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    frames = sorted(images_dir.glob("frame_[0-9][0-9][0-9][0-9][0-9][0-9].png"))
    if not frames:
        raise FileNotFoundError(f"no video frames found in {images_dir}")

    with Image.open(frames[0]) as first:
        width, height = first.size
    options = {
        "g": str(config.gop),
        "crf": str(config.crf),
        "preset": str(config.preset),
    }
    with av.open(str(video_path), "w") as output:
        stream = output.add_stream(config.codec, fps, options=options)
        stream.pix_fmt = config.pixel_format
        stream.width = width
        stream.height = height
        if config.threads:
            stream.thread_count = config.threads
        for path in frames:
            with Image.open(path) as image:
                frame = av.VideoFrame.from_image(image.convert("RGB"))
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)

    if not video_path.is_file():
        raise OSError(f"video encoding did not create {video_path}")
