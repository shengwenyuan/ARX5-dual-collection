from __future__ import annotations

from pathlib import Path
from typing import Any

from arx5_collection.cleaning.models import MessageRef


SUPPORTED_ENCODINGS = {"yuv422_yuy2", "yuyv", "yuy2"}


def decode_yuyv(data: bytes, width: int, height: int, step: int):
    """Decode packed YUYV using the BT.601 limited-range conversion."""

    import numpy as np

    if width <= 0 or height <= 0 or width % 2:
        raise ValueError(f"YUYV image dimensions must be positive with even width: {width}x{height}")
    if step < width * 2 or len(data) < step * height:
        raise ValueError("YUYV image payload is shorter than its declared dimensions")
    packed = np.frombuffer(data, dtype=np.uint8, count=step * height).reshape(height, step)
    pixels = packed[:, : width * 2].reshape(height, width // 2, 4).astype(np.int32)
    y0 = pixels[:, :, 0] - 16
    u = pixels[:, :, 1] - 128
    y1 = pixels[:, :, 2] - 16
    v = pixels[:, :, 3] - 128

    def convert(y):
        c = np.maximum(y, 0)
        red = (298 * c + 409 * v + 128) >> 8
        green = (298 * c - 100 * u - 208 * v + 128) >> 8
        blue = (298 * c + 516 * u + 128) >> 8
        return np.stack((red, green, blue), axis=-1)

    even = convert(y0)
    odd = convert(y1)
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, 0::2] = np.clip(even, 0, 255).astype(np.uint8)
    rgb[:, 1::2] = np.clip(odd, 0, 255).astype(np.uint8)
    return rgb


def decode_color_message(message: Any):
    encoding = str(message.encoding).lower()
    if encoding not in SUPPORTED_ENCODINGS:
        raise ValueError(f"unsupported color encoding: {message.encoding}")
    return decode_yuyv(
        bytes(message.data),
        int(message.width),
        int(message.height),
        int(message.step),
    )


def extract_selected_rgb(
    episode_dir: Path,
    selected_refs: set[MessageRef],
    output_dir: Path,
    output_size: tuple[int, int] = (640, 360),
) -> dict[tuple[str, int], Path]:
    """Decode selected MCAP color messages into a bounded on-disk JPEG cache."""

    import rosbag2_py
    from PIL import Image
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import Image as RosImage

    wanted = {(ref.topic, ref.sequence): ref for ref in selected_refs}
    if len(wanted) != len(selected_refs):
        raise ValueError("selected image references are not unique")
    output_dir.mkdir(parents=True, exist_ok=False)
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=str((episode_dir / "episode.mcap").resolve()), storage_id="mcap"),
        rosbag2_py.ConverterOptions("", ""),
    )
    sequences: dict[str, int] = {}
    paths: dict[tuple[str, int], Path] = {}
    while reader.has_next() and len(paths) < len(wanted):
        topic, payload, _ = reader.read_next()
        sequence = sequences.get(topic, 0)
        sequences[topic] = sequence + 1
        key = (topic, sequence)
        if key not in wanted:
            continue
        message = deserialize_message(payload, RosImage)
        stamp_ns = int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)
        if stamp_ns != wanted[key].header_stamp_ns:
            raise ValueError(f"selected image Header changed for {key}")
        rgb = decode_color_message(message)
        image = Image.fromarray(rgb, mode="RGB")
        if image.size != output_size:
            image = image.resize(output_size, Image.Resampling.BILINEAR)
        role_dir = output_dir / topic.strip("/").replace("/", "_")
        role_dir.mkdir(exist_ok=True)
        path = role_dir / f"{sequence:08d}.jpg"
        image.save(path, format="JPEG", quality=95, subsampling=0)
        paths[key] = path
    missing = sorted(set(wanted) - set(paths))
    if missing:
        raise ValueError(f"selected images missing from MCAP: {missing[:5]}")
    return paths
