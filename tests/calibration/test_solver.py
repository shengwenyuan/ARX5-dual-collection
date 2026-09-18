"""Synthetic camera pixels + real URDF FK exercise the complete offline boundary."""

from copy import deepcopy

import cv2
import numpy as np
import pytest

from arx5_collection.calibration.board import Board, detect
from arx5_collection.calibration.geometry import inverse, rigid
from arx5_collection.calibration.profiles import (
    DEFAULT_PROFILE,
)
from arx5_collection.calibration.profiles import (
    LEGACY_PROFILE as PROFILE,
)
from arx5_collection.calibration.routes import new_route, route_hash, validate
from arx5_collection.calibration.solve import (
    check_observation,
    intrinsics,
    solve_runs,
    verify_result,
)
from arx5_collection.calibration.storage import file_digest, write_json


def render(board, z, k, profile=PROFILE):
    scale = 3
    image = np.full(
        (profile["height"] * scale, profile["width"] * scale, 3), 170, np.uint8
    )
    r = cv2.Rodrigues(z[:3, :3])[0]
    for y in range(-1, board.rows):
        for x in range(-1, board.columns):
            points = (
                np.array(
                    [[x, y, 0], [x + 1, y, 0], [x + 1, y + 1, 0], [x, y + 1, 0]],
                    np.float32,
                )
                * board.square_mm
                / 1000
            )
            polygon = cv2.projectPoints(points, r, z[:3, 3], k, None)[0].reshape(-1, 2)
            cv2.fillConvexPoly(
                image,
                np.rint(polygon * scale).astype(np.int32),
                (245,) * 3 if (x + y) % 2 else (8,) * 3,
            )
    return cv2.resize(
        image, (profile["width"], profile["height"]), interpolation=cv2.INTER_AREA
    )


def raw_sample(q, time_s):
    return {
        "q": list(q),
        "velocity": [0.0] * 6,
        "gripper": -0.8,
        "source_time_s": 1000 + time_s,
        "received_wall_s": 1000 + time_s + 0.001,
        "received_monotonic_s": time_s + 0.001,
    }


def build_run(root, kin, profile=PROFILE):
    board = Board(7, 5, 10)
    identity = {
        "station_id": "synthetic",
        "sdk_type": 2,
        "arms": {"left": "l", "right": "r"},
        "cameras": {"left": "cl", "right": "cr", "overview": "co"},
    }
    route = new_route("left-wrist", identity, board, kin, "test-only", profile)
    k = np.array([[650.0, 0, 424], [0, 660.0, 240], [0, 0, 1]])
    k[0] *= profile["width"] / 848
    k[1] *= profile["height"] / 480
    home = np.array([0, 0.948, 0.858, -0.573, 0, 0])
    x = rigid([0.12, -0.15, 0.08], [0.03, 0.02, 0.01])
    z0 = rigid([0.2, -0.15, 0.05], [-0.03, -0.02, 0.24])
    y = kin.fk(home) @ x @ z0
    rng = np.random.default_rng(123)
    root.mkdir()
    (root / "images").mkdir()
    observations = []
    for attempt in range(2000):
        if len(observations) == 25:
            break
        q = home + rng.uniform(-1, 1, 6) * [0.13, 0.17, 0.18, 0.25, 0.30, 0.30]
        z = inverse(x) @ inverse(kin.fk(q)) @ y
        corners = cv2.projectPoints(
            board.points(), cv2.Rodrigues(z[:3, :3])[0], z[:3, 3], k, None
        )[0].reshape(-1, 2)
        normalized_corners = corners / [profile["width"] / 848, profile["height"] / 480]
        if (
            (normalized_corners.min(axis=0) < [40, 40]).any()
            or (normalized_corners.max(axis=0) > [808, 440]).any()
            or z[2, 3] < 0.14
        ):
            continue
        image = render(board, z, k, profile)
        detection = detect(image, board, reference=corners)
        if not detection.valid:
            continue
        index = len(observations)
        pose_id = f"pose-{index}"
        split = "validation" if index % 5 == 4 else "training"
        route["waypoints"].append(
            {
                "pose_id": pose_id,
                "q": {"left": q.tolist(), "right": home.tolist()},
                "kind": "capture",
                "split": split,
                "teaching": {
                    "origin_confirmed": True,
                    "corners": detection.corners.tolist(),
                },
            }
        )
        path = root / "images" / f"{pose_id}.png"
        assert cv2.imwrite(str(path), image)
        begin = 10 + index * 10
        actual = {
            "left": raw_sample(q, begin + 0.5),
            "right": raw_sample(home, begin + 0.5),
        }
        raw = [
            {"side": side, **raw_sample(q if side == "left" else home, float(t))}
            for t in np.arange(begin - 0.02, begin + 2.011, 0.01)
            for side in ("left", "right")
        ]
        observations.append(
            {
                "pose_id": pose_id,
                "split": split,
                "image": f"images/{pose_id}.png",
                "image_sha256": file_digest(path),
                "frame": {
                    "number": index,
                    "clock": "global_time",
                    "source_time_s": 1000 + begin + 0.5,
                    "source_monotonic_s": begin + 0.5,
                    "received_monotonic_s": begin + 0.51,
                    "received_wall_s": 1000 + begin + 0.51,
                },
                "actual": actual,
                "window": {"start": begin, "end": begin + 2.01},
                "raw_states": raw,
                "window_samples": [],
                "detection": detection.payload(),
                "T_base_link6": kin.fk(q).tolist(),
            }
        )
    assert len(observations) == 25
    route["draft"] = False
    validate(route, replay=True)
    run = {
        "schema_version": 1,
        "run_id": "synthetic",
        "status": "completed",
        "role": "left-wrist",
        "route_sha256": route_hash(route),
        "camera": {"serial": "cl", "profile": profile},
        "observations": observations,
    }
    write_json(root / "route.json", route)
    write_json(root / "run.json", run)
    return route, run, x


@pytest.mark.parametrize("profile", [PROFILE, DEFAULT_PROFILE])
def test_full_pixel_fk_pipeline_and_recomputable_evidence(tmp_path, kin, profile):
    route, run, truth = build_run(tmp_path / "run", kin, profile)
    index = []
    for observation in run["observations"]:
        path = tmp_path / "run" / "observations" / (observation["pose_id"] + ".json")
        write_json(path, observation)
        index.append(
            {
                "file": str(path.relative_to(tmp_path / "run")),
                "sha256": file_digest(path),
            }
        )
    write_json(tmp_path / "run" / "run.json", {**run, "observations": index})
    result = solve_runs([tmp_path / "run"], tmp_path / "result")
    assert result["world_frame"] == "overview_optical" and result["status"] == "partial"
    assert not result["absolute_accuracy_verified"]
    np.testing.assert_allclose(
        np.array(result["solutions"]["left-wrist"]["X"])[:3, 3],
        truth[:3, 3],
        atol=0.002,
    )
    assert verify_result(tmp_path / "result")["verified"]
    image = next((tmp_path / "result/evidence/left-wrist/images").glob("*.png"))
    image.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_result(tmp_path / "result")


def test_clock_or_motion_mismatch_is_rejected_before_fit(tmp_path, kin):
    route, run, _ = build_run(tmp_path / "run", kin)
    obs = deepcopy(run["observations"][0])
    check_observation(route, obs)
    obs["frame"]["source_monotonic_s"] = obs["window"]["start"] - 1
    with pytest.raises(ValueError, match="clock/window"):
        check_observation(route, obs)
    obs = deepcopy(run["observations"][0])
    obs["raw_states"][30]["velocity"][1] = 0.03
    with pytest.raises(ValueError, match="motion|Motion"):
        check_observation(route, obs)


def test_heldout_pixels_do_not_change_fitted_intrinsics(tmp_path, kin):
    route, run, _ = build_run(tmp_path / "run", kin)
    expected = intrinsics([(route, run)])
    changed = deepcopy(run)
    for obs in changed["observations"]:
        if obs["split"] == "validation":
            obs["detection"]["corners"] = (
                np.asarray(obs["detection"]["corners"]) + 50
            ).tolist()
    assert expected == intrinsics([(route, changed)])
