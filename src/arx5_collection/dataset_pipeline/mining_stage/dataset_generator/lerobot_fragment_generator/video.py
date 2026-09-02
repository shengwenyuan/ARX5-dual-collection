from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import time

from ..models import VideoEncodingConfig


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
