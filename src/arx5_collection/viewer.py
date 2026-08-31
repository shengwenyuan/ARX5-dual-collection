from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import mimetypes
import os
import re
import subprocess
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import gettempdir
from tempfile import mkstemp
from urllib.parse import parse_qs, quote, urlparse


DEFAULT_DATASET = Path("/mnt/cfs/data/swy/folding_the_cloth_20260821_20260822_v1")
STATIC_ROOT = Path(__file__).with_name("viewer_static")
CAMERA_LABELS = {
    "observation.images.cam_high": "Overview",
    "observation.images.cam_left_wrist": "Left wrist",
    "observation.images.cam_right_wrist": "Right wrist",
}
ACTION_HORIZON = 50
MOTOR_NAMES = (
    "left_j1",
    "left_j2",
    "left_j3",
    "left_j4",
    "left_j5",
    "left_j6",
    "left_gripper",
    "right_j1",
    "right_j2",
    "right_j3",
    "right_j4",
    "right_j5",
    "right_j6",
    "right_gripper",
)


@dataclass(frozen=True, slots=True)
class DatasetIndex:
    root: Path
    info: dict
    episodes: tuple[dict, ...]
    cameras: tuple[str, ...]

    @classmethod
    def read(cls, root: Path) -> DatasetIndex:
        resolved = root.expanduser().resolve(strict=True)
        info_path = resolved / "meta" / "info.json"
        episodes_path = resolved / "meta" / "episodes.jsonl"
        info = json.loads(info_path.read_text())
        episodes = tuple(
            json.loads(line)
            for line in episodes_path.read_text().splitlines()
            if line.strip()
        )
        if info.get("codebase_version") != "v2.1":
            raise ValueError("dataset must use LeRobot v2.1")
        if info.get("total_episodes") != len(episodes):
            raise ValueError("meta/info.json and meta/episodes.jsonl disagree")
        cameras = tuple(
            key
            for key, feature in info.get("features", {}).items()
            if feature.get("dtype") == "video"
        )
        if not cameras:
            raise ValueError("dataset contains no video features")
        return cls(resolved, info, episodes, cameras)

    def episode(self, index: int) -> dict:
        matches = [item for item in self.episodes if item["episode_index"] == index]
        if len(matches) != 1:
            raise ValueError(f"episode {index} does not exist")
        return matches[0]

    def video_path(self, episode_index: int, camera: str) -> Path:
        self.episode(episode_index)
        if camera not in self.cameras:
            raise ValueError(f"unknown camera {camera}")
        pattern = self.info["video_path"]
        path = pattern.format(
            episode_chunk=episode_index // self.info["chunks_size"],
            video_key=camera,
            episode_index=episode_index,
        )
        resolved = (self.root / path).resolve(strict=True)
        resolved.relative_to(self.root)
        return resolved

    def parquet_path(self, episode_index: int) -> Path:
        self.episode(episode_index)
        pattern = self.info["data_path"]
        path = pattern.format(
            episode_chunk=episode_index // self.info["chunks_size"],
            episode_index=episode_index,
        )
        resolved = (self.root / path).resolve(strict=True)
        resolved.relative_to(self.root)
        return resolved

    def payload(self) -> dict:
        fps = self.info["fps"]
        return {
            "path": str(self.root),
            "name": self.root.name,
            "fps": fps,
            "total_episodes": len(self.episodes),
            "total_frames": self.info["total_frames"],
            "total_videos": self.info["total_videos"],
            "cameras": [
                {"key": key, "label": CAMERA_LABELS.get(key, key.rsplit(".", 1)[-1])}
                for key in self.cameras
            ],
            "episodes": [
                {
                    **item,
                    "duration_s": round(item["length"] / fps, 2),
                    "thumbnail_url": endpoint("thumbnail", self.root, item["episode_index"]),
                }
                for item in self.episodes
            ],
        }

    def episode_payload(self, episode_index: int) -> dict:
        item = self.episode(episode_index)
        fps = self.info["fps"]
        camera = self.cameras[0]
        return {
            **item,
            "fps": fps,
            "duration_s": round(item["length"] / fps, 2),
            "tasks": item.get("tasks", []),
            "action_horizon": ACTION_HORIZON,
            "training_samples": item["length"],
            "video": {
                "key": camera,
                "label": CAMERA_LABELS.get(camera, camera.rsplit(".", 1)[-1]),
                "url": endpoint("video", self.root, episode_index, camera),
            },
            "cameras": [
                {"key": key, "label": CAMERA_LABELS.get(key, key.rsplit(".", 1)[-1])}
                for key in self.cameras
            ],
            "provenance": {
                "available": False,
                "detail": "Showing the final LeRobot training snapshot directly from Parquet and MP4.",
            },
        }

    def sample_timeline_payload(self, episode_index: int) -> dict:
        episode = self.episode(episode_index)
        table = read_parquet_episode(self.parquet_path(episode_index))
        points = [
            [table["index"][offset], frame, table["timestamp"][offset]]
            for offset, frame in enumerate(table["frame_index"])
        ]
        if len(points) != episode["length"]:
            raise ValueError(f"episode {episode_index} metadata and Parquet disagree")
        return {
            "episode_index": episode_index,
            "mode": "training_snapshot",
            "point_format": ["dataset_index", "frame_index", "timestamp"],
            "points": points,
        }

    def action_chunk_payload(self, episode_index: int, frame_index: int) -> dict:
        episode = self.episode(episode_index)
        if not 0 <= frame_index < episode["length"]:
            raise ValueError(f"frame {frame_index} is outside episode {episode_index}")
        table = read_parquet_episode(self.parquet_path(episode_index))
        actions = table["action"]
        states = table["state"]
        timestamps = table["timestamp"]
        stop = min(frame_index + ACTION_HORIZON, len(actions))
        chunk = actions[frame_index:stop]
        padding = ACTION_HORIZON - len(chunk)
        if padding:
            chunk = [*chunk, *([chunk[-1]] * padding)]
        return {
            "episode_index": episode_index,
            "frame_index": frame_index,
            "timestamp": timestamps[frame_index],
            "state": states[frame_index],
            "actions": chunk,
            "action_horizon": ACTION_HORIZON,
            "padding_steps": padding,
            "motor_names": MOTOR_NAMES,
            "images": [
                {
                    "key": key,
                    "label": CAMERA_LABELS.get(key, key.rsplit(".", 1)[-1]),
                    "url": frame_endpoint(
                        self.root,
                        episode_index,
                        frame_index,
                        key,
                    ),
                }
                for key in self.cameras
            ],
        }


def endpoint(kind: str, root: Path, episode: int, camera: str | None = None) -> str:
    value = f"/api/{kind}?path={quote(str(root))}&episode={episode}"
    if camera is not None:
        value += f"&camera={quote(camera)}"
    return value


def frame_endpoint(
    root: Path,
    episode: int,
    frame: int,
    camera: str,
) -> str:
    return (
        f"/api/frame?path={quote(str(root))}&episode={episode}"
        f"&frame={frame}&camera={quote(camera)}"
    )


@lru_cache(maxsize=16)
def read_parquet_episode(path: Path) -> dict[str, list]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as error:
        raise RuntimeError("viewer requires the pyarrow package") from error
    table = parquet.read_table(
        path,
        columns=["index", "frame_index", "timestamp", "action", "observation.state"],
    )
    rows = table.to_pylist()
    if [row["frame_index"] for row in rows] != list(range(len(rows))):
        raise ValueError(f"non-contiguous frame_index in {path}")
    return {
        "index": [int(row["index"]) for row in rows],
        "frame_index": [int(row["frame_index"]) for row in rows],
        "timestamp": [float(row["timestamp"]) for row in rows],
        "action": [[float(value) for value in row["action"]] for row in rows],
        "state": [
            [float(value) for value in row["observation.state"]]
            for row in rows
        ],
    }


def thumbnail_path(dataset: DatasetIndex, episode: int) -> Path:
    video = dataset.video_path(episode, dataset.cameras[0])
    identity = f"{video}:{video.stat().st_size}:{video.stat().st_mtime_ns}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(gettempdir()) / "arx5-viewer-thumbnails"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{digest}.jpg"
    if not output.exists():
        duration = dataset.episode(episode)["length"] / dataset.info["fps"]
        descriptor, temporary_name = mkstemp(dir=root, suffix=".jpg")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{duration / 2:.3f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    str(temporary),
                ],
                check=True,
            )
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return output


def frame_thumbnail_path(
    dataset: DatasetIndex,
    episode: int,
    frame: int,
    camera: str,
) -> Path:
    video = dataset.video_path(episode, camera)
    episode_item = dataset.episode(episode)
    if not 0 <= frame < episode_item["length"]:
        raise ValueError(f"frame {frame} is outside episode {episode}")
    identity = (
        f"{video}:{video.stat().st_size}:{video.stat().st_mtime_ns}:{frame}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    root = Path(gettempdir()) / "arx5-viewer-frames"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{digest}.jpg"
    if output.exists():
        return output
    descriptor, temporary_name = mkstemp(dir=root, suffix=".jpg")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                f"{frame / dataset.info['fps']:.6f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2",
                "-q:v",
                "3",
                str(temporary),
            ],
            check=True,
        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


class RangeNotSatisfiable(ValueError):
    def __init__(self, size: int):
        super().__init__("requested Range is outside the file")
        self.size = size


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "ARX5Viewer/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_file(STATIC_ROOT / "index.html")
            elif parsed.path.startswith("/static/"):
                static_path = (
                    STATIC_ROOT / parsed.path.removeprefix("/static/")
                ).resolve(strict=True)
                static_path.relative_to(STATIC_ROOT.resolve())
                self.send_file(static_path)
            elif parsed.path == "/api/dataset":
                dataset = self.dataset(parsed.query)
                self.send_json(dataset.payload())
            elif parsed.path == "/api/episode":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                self.send_json(dataset.episode_payload(self.integer(query, "episode")))
            elif parsed.path == "/api/action-chunk":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                self.send_json(
                    dataset.action_chunk_payload(
                        self.integer(query, "episode"),
                        self.integer(query, "frame"),
                    )
                )
            elif parsed.path == "/api/sample-timeline":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                self.send_json(
                    dataset.sample_timeline_payload(self.integer(query, "episode"))
                )
            elif parsed.path == "/api/thumbnail":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                self.send_file(thumbnail_path(dataset, self.integer(query, "episode")))
            elif parsed.path == "/api/frame":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                self.send_file(
                    frame_thumbnail_path(
                        dataset,
                        self.integer(query, "episode"),
                        self.integer(query, "frame"),
                        self.single(query, "camera"),
                    )
                )
            elif parsed.path == "/api/video":
                query = parse_qs(parsed.query)
                dataset = self.dataset(parsed.query)
                camera = self.single(query, "camera")
                self.send_file(
                    dataset.video_path(self.integer(query, "episode"), camera),
                    allow_range=True,
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except RangeNotSatisfiable as error:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{error.size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except subprocess.CalledProcessError as error:
            self.send_json({"error": f"thumbnail generation failed: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def dataset(self, query_text: str) -> DatasetIndex:
        query = parse_qs(query_text)
        path = Path(query.get("path", [str(self.server.default_dataset)])[0])
        return DatasetIndex.read(path)

    def single(self, query: dict[str, list[str]], name: str) -> str:
        values = query.get(name, [])
        if len(values) != 1 or not values[0]:
            raise ValueError(f"query parameter {name} is required")
        return values[0]

    def integer(self, query: dict[str, list[str]], name: str) -> int:
        return int(self.single(query, name))

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, allow_range: bool = False) -> None:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match:
                raise RangeNotSatisfiable(size)
            first, last = match.groups()
            if not first and (not last or int(last) <= 0):
                raise RangeNotSatisfiable(size)
            if first:
                start = int(first)
                end = int(last) if last else end
            else:
                length = int(last)
                start = max(0, size - length)
            if start > end or end >= size:
                raise RangeNotSatisfiable(size)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with resolved.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise OSError(f"unexpected end of file: {resolved}")
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} {format % args}")


class ViewerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], default_dataset: Path):
        super().__init__(address, ViewerHandler)
        self.default_dataset = default_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arx5-viewer")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dataset = DatasetIndex.read(args.dataset)
    server = ViewerServer((args.host, args.port), dataset.root)
    print(f"ARX5 Dataset Viewer: http://{args.host}:{args.port}")
    print(f"Dataset: {dataset.root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
