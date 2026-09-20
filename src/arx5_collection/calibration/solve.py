"""Offline-only intrinsic/hand-eye solver, bound to immutable RGB/state evidence."""

from __future__ import annotations

import shutil
from pathlib import Path

import cv2
import numpy as np

from .board import Board
from .geometry import Kinematics, errors, handeye, inverse, observable, rigid, transform
from .motion import MotionLimits, stationary, validate_sample
from .profiles import validate_profile
from .routes import route_hash, split_observations, validate, validate_session
from .storage import bound_path, digest, file_digest, read_json, write_json
from .timing import timing_error

THRESHOLDS = {
    "translation_m": 0.002,
    "rotation_rad": float(np.deg2rad(1)),
    "reprojection_px": 1.0,
}


def check_observation(route, obs):
    """Check timestamp association and continuous feedback, not just a later pose read."""
    limits = MotionLimits(**route["motion"])
    frame, window = obs["frame"], obs["window"]
    begin, end = window["start"], window["end"]
    source = frame["source_monotonic_s"]
    if (
        timing_error(frame)
        or not np.isfinite([begin, end, source]).all()
        or end - begin < limits.capture_s
        or not begin + 0.1 <= source <= end
    ):
        raise ValueError("invalid capture clock/window")
    actual = obs["actual"]
    validate_sample(
        actual,
        max(
            frame["received_monotonic_s"],
            *(actual[s]["received_monotonic_s"] for s in ("left", "right")),
        ),
        limits,
    )
    if any(
        abs(actual[s]["source_time_s"] - frame["source_time_s"]) > 0.25
        for s in ("left", "right")
    ):
        raise ValueError("image and actual feedback cannot be associated")
    reference = {s: actual[s]["q"] for s in ("left", "right")}
    raw = obs["raw_states"]
    for side in ("left", "right"):
        states = [
            s
            for s in raw
            if s["side"] == side and begin - 0.05 <= s["received_monotonic_s"] <= end
        ]
        if (
            len(states) < 5
            or states[0]["received_monotonic_s"] > begin + 0.05
            or end - states[-1]["received_monotonic_s"] > 0.05
        ):
            raise ValueError("incomplete stationary feedback evidence")
        times = [s["received_monotonic_s"] for s in states]
        if (
            np.max(np.diff(times)) > limits.feedback_age_s
            or np.min(np.diff(times)) <= 0
        ):
            raise ValueError("feedback gap/reordering during exposure window")
        for state in states:
            if (
                np.max(np.abs(np.asarray(state["q"]) - reference[side]))
                > limits.arrival_rad
                or np.max(np.abs(state["velocity"])) > limits.still_velocity_rad_s
                or not -0.02
                <= state["received_wall_s"] - state["source_time_s"]
                <= limits.feedback_age_s
            ):
                raise ValueError("motion or stale state during capture")
    if not stationary(actual, reference, limits):
        raise ValueError("moving observation")


def load_run(path):
    path = Path(path).resolve()
    route, run = read_json(path / "route.json"), read_json(path / "run.json")
    validate(route, replay=True)
    validate_session(run)
    if (
        run["schema_version"] != 2
        or run["status"] != "completed"
        or run["role"] != route["role"]
        or run["route_sha256"] != route_hash(route)
        or run["camera"]["serial"] != run["station"]["cameras"][route["camera_role"]]
        or run["camera"]["profile"] != route["profile"]
    ):
        raise ValueError("incomplete or incompatible capture run")
    captures = {p["pose_id"]: p for p in route["waypoints"] if p["kind"] == "capture"}
    observations = []
    for entry in run["observations"]:
        if "file" in entry:
            observation_path = bound_path(path, entry["file"])
            if file_digest(observation_path) != entry["sha256"]:
                raise ValueError("observation evidence hash mismatch")
            observations.append(read_json(observation_path))
        else:
            observations.append(
                entry
            )  # Inline evidence is also accepted for offline fixtures.
    run = {**run, "observations": observations}
    points = {p["pose_id"]: p for p in route["waypoints"]}
    outcomes = run["pose_results"]
    if len(outcomes) != len(points) or [p["pose_id"] for p in outcomes] != [
        p["pose_id"] for p in route["waypoints"]
    ]:
        raise ValueError("incomplete route traversal evidence")
    accepted = []
    for outcome in outcomes:
        point = points[outcome["pose_id"]]
        allowed = {"via"} if point["kind"] == "via" else {"captured", "skipped"}
        if outcome["status"] not in allowed or (
            outcome["status"] == "skipped" and not outcome.get("reason")
        ):
            raise ValueError("invalid pose outcome")
        if outcome["status"] == "captured":
            accepted.append(outcome["pose_id"])
    if [o["pose_id"] for o in observations] != accepted:
        raise ValueError("missing/duplicate accepted capture checkpoints")
    expected = [{"pose_id": o["pose_id"]} for o in observations]
    selection = split_observations(expected)
    selection.update(
        attempted=len(outcomes), skipped=sum(o["status"] == "skipped" for o in outcomes)
    )
    if run["selection"] != selection or [o["split"] for o in observations] != [
        o["split"] for o in expected
    ]:
        raise ValueError("accepted-view split changed after capture")
    kin, board = Kinematics.from_payload(route["kinematics"]), Board(**run["board"])
    for obs in observations:
        check_observation(route, obs)
        if not stationary(
            obs["actual"],
            captures[obs["pose_id"]]["q"],
            MotionLimits(**route["motion"]),
        ):
            raise ValueError("capture did not reach its taught target")
        image_path = bound_path(path, obs["image"])
        if file_digest(image_path) != obs["image_sha256"]:
            raise ValueError("source image hash mismatch")
        image = cv2.imread(str(image_path))
        if image is None or image.shape != (
            route["profile"]["height"],
            route["profile"]["width"],
            3,
        ):
            raise ValueError("source image dimensions differ from calibration profile")
        corners = np.asarray(obs["detection"]["corners"], np.float32)
        if (
            corners.shape != (board.rows * board.columns, 2)
            or not np.isfinite(corners).all()
            or obs["detection"]["reason"]
            or obs["detection"]["coverage"] < board.min_coverage
            or obs["detection"]["sharpness"] < board.min_sharpness
        ):
            raise ValueError("invalid accepted corner evidence")
        # Re-detect from immutable pixels; choose the stored physical numbering, never refit IDs.
        from .board import detect

        detected = detect(image, board, reference=corners)
        if (
            not detected.valid
            or np.max(np.linalg.norm(detected.corners - corners, axis=1)) > 0.25
        ):
            raise ValueError("stored corners do not reproduce from original image")
        fk = kin.fk(obs["actual"][route["active_arm"]]["q"])
        if not np.allclose(fk, transform(obs["T_base_link6"]), atol=1e-9):
            raise ValueError("stored FK differs from raw readback/model")
    return route, run


def intrinsics(datasets):
    if not datasets:
        raise ValueError("intrinsic calibration needs input datasets")
    profile = validate_profile(datasets[0][0]["profile"])
    if any(validate_profile(route["profile"]) != profile for route, _ in datasets):
        raise ValueError("cannot fit intrinsics from mixed native camera profiles")
    points, corners = [], []
    for route, run in datasets:
        board = Board(**run["board"])
        for obs in run["observations"]:
            if obs["split"] == "training":
                points.append(board.points())
                corners.append(np.array(obs["detection"]["corners"], np.float32))
    if len(points) < 15:
        raise ValueError("at least 15 intrinsic training views required")
    rms, k, dist, _, _ = cv2.calibrateCamera(
        points, corners, (profile["width"], profile["height"]), None, None
    )
    if (
        not np.isfinite(k).all()
        or not np.isfinite(dist).all()
        or min(k[0, 0], k[1, 1]) <= 0
        or rms > THRESHOLDS["reprojection_px"]
    ):
        raise ValueError(f"intrinsic fit failed quality gate: RMS={rms}")
    return {
        "K": k.tolist(),
        "distortion": dist.ravel().tolist(),
        "model": "opencv_brown5",
        "rms_px": float(rms),
        "training_views": len(points),
        "profile": profile,
    }


def board_pose(board, corners, intrinsic, *, require_unique=True):
    k, distortion = np.array(intrinsic["K"]), np.array(intrinsic["distortion"])
    points = board.points()
    corners = np.asarray(corners, dtype=np.float32)
    count, rotations, translations, _ = cv2.solvePnPGeneric(
        points, corners, k, distortion, flags=cv2.SOLVEPNP_IPPE
    )
    candidates = []
    for r, p in zip(rotations, translations):
        t = rigid(r, p.ravel())
        if np.min((points @ t[:3, :3].T + t[:3, 3])[:, 2]) <= 0:
            continue
        projected = cv2.projectPoints(points, r, p, k, distortion)[0].reshape(-1, 2)
        residual = float(np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=1))))
        candidates.append((residual, t))
    candidates.sort(key=lambda v: v[0])
    if not candidates or candidates[0][0] > THRESHOLDS["reprojection_px"]:
        raise ValueError("no positive-depth, accurate planar PnP candidate")
    if (
        require_unique
        and len(candidates) > 1
        and candidates[1][0] - candidates[0][0] < 0.05
    ):
        separation = errors(candidates[0][1], candidates[1][1])
        if separation["translation_m"] > 0.005 or separation["rotation_rad"] > 0.05:
            raise ValueError("ambiguous planar PnP; teach stronger board tilt/coverage")
    return candidates[0][1], candidates[0][0]


def solve_route(route, run, intrinsic):
    if validate_profile(intrinsic["profile"]) != validate_profile(route["profile"]):
        raise ValueError(
            "intrinsic profile differs from image profile; never reuse 480p K on 720p pixels"
        )
    board = Board(**run["board"])
    from .orientation import align_orientations

    aligned, orientation = align_orientations(
        route, run["observations"], board, intrinsic
    )
    views = [
        (obs, transform(obs["T_base_link6"]), pose, px, corners)
        for obs, (pose, px, corners) in zip(run["observations"], aligned)
    ]
    train = [v for v in views if v[0]["split"] == "training"]
    x, y, quality = handeye(route["mode"], [v[1] for v in train], [v[2] for v in train])
    report = []
    passed = True
    for obs, a, z, px, corners in views:
        operator = a if route["mode"] == "eye_in_hand" else inverse(a)
        residual = errors(y, operator @ x @ z)
        predicted = inverse(x) @ inverse(operator) @ y
        projected = cv2.projectPoints(
            board.points(),
            cv2.Rodrigues(predicted[:3, :3])[0],
            predicted[:3, 3],
            np.array(intrinsic["K"]),
            np.array(intrinsic["distortion"]),
        )[0].reshape(-1, 2)
        # The pixel gate measures image/PnP fit; hand-eye metric error is gated separately.
        # Also expose the prediction from the fixed hand-eye model, without conflating the two.
        residual["predicted_reprojection_px"] = float(
            np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=1)))
        )
        residual["reprojection_px"] = px
        valid = all(residual[key] <= value for key, value in THRESHOLDS.items())
        passed &= valid
        report.append(
            {
                "pose_id": obs["pose_id"],
                "split": obs["split"],
                "passed": valid,
                "pnp_reprojection_px": px,
                **residual,
            }
        )
    heldout = [row for row in report if row["split"] == "validation"]
    statistics = {
        key: {
            "p95": float(np.percentile([r[key] for r in heldout], 95)),
            "max": float(max(r[key] for r in heldout)),
        }
        for key in THRESHOLDS
    }
    return {
        "role": route["role"],
        "mode": route["mode"],
        "passed": bool(passed),
        "X": x.tolist(),
        "Y": y.tolist(),
        "thresholds": THRESHOLDS,
        "observability": quality,
        "orientation": orientation,
        "residuals": report,
        "validation_statistics": statistics,
        "route_sha256": route_hash(route),
        "run_id": run["run_id"],
    }


def world_transforms(solutions):
    out = {"T_overview_overview": np.eye(4).tolist()}
    for side in ("left", "right"):
        overview, wrist = (
            solutions.get(f"overview-{side}"),
            solutions.get(f"{side}-wrist"),
        )
        if overview:
            out[f"T_overview_{side}_base"] = inverse(overview["X"]).tolist()
        if wrist:
            out[f"T_{side}_link6_{side}_optical"] = transform(wrist["X"]).tolist()
    if "overview-left" in solutions and "overview-right" in solutions:
        out["T_left_base_right_base"] = (
            transform(solutions["overview-left"]["X"])
            @ inverse(solutions["overview-right"]["X"])
        ).tolist()
    return out


def camera_world(result, side, actual_q, kinematics):
    if side == "overview":
        return np.eye(4)
    if side not in ("left", "right"):
        raise ValueError("unknown camera")
    t = result["transforms"]
    return (
        transform(t[f"T_overview_{side}_base"])
        @ kinematics.fk(actual_q)
        @ transform(t[f"T_{side}_link6_{side}_optical"])
    )


def require_usable_views(route, run):
    training = [o for o in run["observations"] if o["split"] == "training"]
    heldout = [o for o in run["observations"] if o["split"] == "validation"]
    if len(training) < 15 or len(heldout) < 5:
        raise ValueError(
            f"{route['role']}: need 15 training + 5 validation accepted views; got {len(training)} + {len(heldout)}"
        )
    observable([transform(o["T_base_link6"]) for o in training])


def validate_intrinsics(datasets, intrinsic):
    rows = []
    for route, run in datasets:
        board = Board(**run["board"])
        for observation in run["observations"]:
            if observation["split"] == "validation":
                _, error = board_pose(
                    board,
                    observation["detection"]["corners"],
                    intrinsic,
                    require_unique=False,
                )
                rows.append(
                    {
                        "role": route["role"],
                        "pose_id": observation["pose_id"],
                        "reprojection_px": error,
                    }
                )
    if len(rows) < 5:
        raise ValueError("intrinsic validation needs at least 5 held-out views")
    return {
        "valid": True,
        "views": len(rows),
        "max_reprojection_px": max(r["reprojection_px"] for r in rows),
        "residuals": rows,
    }


def solve_runs(paths, output: Path):
    if output.exists():
        raise ValueError("result directory already exists")
    output.mkdir(parents=True)
    result = {
        "schema_version": 2,
        "world_frame": "overview_optical",
        "status": "failed",
        "valid": False,
        "opencv_version": cv2.__version__,
        "numpy_version": np.__version__,
        "method": "PARK",
        "intrinsics": {},
        "intrinsic_validation": {},
        "solutions": {},
        "transforms": {},
        "selection": {},
        "absolute_accuracy_verified": False,
        "cross_arm_independently_verified": False,
    }
    try:
        datasets = [load_run(p) for p in paths]
        if not datasets or len({r["role"] for r, _ in datasets}) != len(datasets):
            raise ValueError("provide at most one completed run per route")
        first_route, first_run = datasets[0]
        for route, run in datasets:
            if any(run[k] != first_run[k] for k in ("station", "setup_id")) or any(
                route[k] != first_route[k] for k in ("kinematics", "profile")
            ):
                raise ValueError(
                    "cannot combine different current station/setup/kinematics/profile"
                )
            result["selection"][route["role"]] = run["selection"]
        result.update(
            setup_id=first_run["setup_id"],
            station=first_run["station"],
            kinematics=first_route["kinematics"],
        )
        evidence = []
        for path, (route, _) in zip(paths, datasets):
            destination = output / "evidence" / route["role"]
            shutil.copytree(path, destination)
            for file in sorted(destination.rglob("*")):
                if file.is_file():
                    evidence.append(
                        {
                            "path": str(file.relative_to(output)),
                            "sha256": file_digest(file),
                        }
                    )
        write_json(output / "evidence-manifest.json", {"files": evidence})
        for camera in sorted({r["camera_role"] for r, _ in datasets}):
            group = [(r, run) for r, run in datasets if r["camera_role"] == camera]
            try:
                for route, run in group:
                    require_usable_views(route, run)
                result["intrinsics"][camera] = intrinsics(group)
                result["intrinsic_validation"][camera] = validate_intrinsics(
                    group, result["intrinsics"][camera]
                )
            except ValueError as error:
                result["intrinsic_validation"][camera] = {
                    "valid": False,
                    "error": str(error),
                }
        for route, run in datasets:
            camera = route["camera_role"]
            try:
                if not result["intrinsic_validation"][camera]["valid"]:
                    raise ValueError(
                        "intrinsic validation failed; extrinsic fit not attempted"
                    )
                result["solutions"][route["role"]] = solve_route(
                    route, run, result["intrinsics"][camera]
                )
            except ValueError as error:
                result["solutions"][route["role"]] = {
                    "passed": False,
                    "error": str(error),
                }
        if not all(s["passed"] for s in result["solutions"].values()):
            raise ValueError(
                "intrinsic/hand-eye validation failed; inspect calibration.json"
            )
        result["transforms"] = world_transforms(result["solutions"])
        result["status"] = "complete" if len(datasets) == 4 else "partial"
        result["valid"] = True
        return result
    except BaseException as error:
        result["error"] = str(error)
        raise
    finally:
        write_json(output / "calibration.json", result)


def verify_result(path: Path):
    import tempfile

    manifest = read_json(path / "evidence-manifest.json")
    for item in manifest["files"]:
        if file_digest(bound_path(path, item["path"])) != item["sha256"]:
            raise ValueError("result evidence hash mismatch")
    expected = read_json(path / "calibration.json")
    if expected["status"] not in {"complete", "partial"}:
        raise ValueError("cannot verify failed result")
    with tempfile.TemporaryDirectory(prefix="arx5-cali-verify-") as temporary:
        actual = solve_runs(
            [path / "evidence" / role for role in expected["solutions"]],
            Path(temporary) / "result",
        )
    if digest(actual) != digest(expected):
        raise ValueError("recomputed result differs (including software versions)")
    return {
        "verified": True,
        "status": expected["status"],
        "world_frame": expected["world_frame"],
    }
