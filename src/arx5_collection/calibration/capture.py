"""Traverse every portable pose; missing visual targets are explicit skipped samples."""

from copy import deepcopy
from pathlib import Path
from time import monotonic, sleep

import cv2

from .board import Board, detect
from .geometry import Kinematics
from .motion import MotionLimits
from .preview import ROLE_NAMES, WINDOW_CLOSED, live_checks, render
from .replay import ReplayControl, StableWindow, preflight_start
from .routes import route_hash, split_observations, validate, validate_session
from .storage import file_digest, identifier, read_json, write_json
from .timing import capture_ready, timing_error


def capture_pose(hardware, control, route, board, point, index, window, show):
    """Visual rejection advances the route. Motion/controller failures propagate."""
    limits = MotionLimits(**route["motion"])
    kin = Kinematics.from_payload(route["kinematics"])
    deadline = monotonic() + limits.segment_timeout_s
    expected_end = control.move(point["q"][route["active_arm"]])
    gate = StableWindow(limits)
    started, best, seen, detection = None, None, None, None
    reason = "no usable image in capture window"
    samples = []
    while monotonic() < deadline:
        control.require_ok()
        hardware.supervisor.require_running()
        frame = hardware.camera.latest()
        new_frame = frame["number"] != seen
        if new_frame:
            # Only this station's pixels: never compare against a teaching workstation.
            detection = detect(frame["image"], board)
            seen = frame["number"]
        control.require_ok()
        sample = hardware.arms.read()  # After potentially slow detection.
        now = monotonic()
        stable = gate.update(sample, now, control.target()) and now >= expected_end
        if started is not None:
            samples.append(sample)
        if stable and point["kind"] == "via":
            return None, "via", "transit pose reached"
        if not stable and started is not None:
            raise RuntimeError("arm moved during capture window")
        if stable and started is None:
            started = now
        if new_frame:
            within = (
                capture_ready(frame, now)
                and started is not None
                and started + 0.1 <= frame["source_monotonic_s"] <= now
            )
            reason = (
                detection.reason
                or timing_error(frame)
                or "no fresh frame after settling"
            )
            if (
                detection.valid
                and within
                and (best is None or detection.sharpness > best[0])
            ):
                best = (
                    detection.sharpness,
                    frame,
                    deepcopy(sample),
                    detection.payload(),
                )
        status = (
            "CAPTURE"
            if started is not None
            else "SETTLE"
            if now >= expected_end
            else "MOVE"
        )
        checks = live_checks(
            frame, detection, board, sample, kin, limits, gate, stable, monotonic()
        )
        key = show(
            window,
            render(
                frame["image"],
                board,
                detection,
                f"{ROLE_NAMES[route['role']]} | {index + 1}/{len(route['waypoints'])} | {status}",
                checks,
                ["Esc or close window to stop"],
            ),
        )
        if key in (27, WINDOW_CLOSED):
            raise KeyboardInterrupt("operator stopped calibration")
        if started is not None and now - started >= limits.capture_s:
            control.require_ok()
            if best is None:
                return None, "skipped", reason
            _, selected, actual, detected = best
            observation = {
                "pose_id": point["pose_id"],
                "split": None,  # Assigned after all visual outcomes, before any fitting.
                "frame": {k: v for k, v in selected.items() if k != "image"},
                "actual": actual,
                "window": {"start": started, "end": now},
                "window_samples": samples,
                "detection": detected,
                "T_base_link6": kin.fk(actual[route["active_arm"]]["q"]).tolist(),
                "raw_states": hardware.arms.evidence(started - 0.2, now),
            }
            from .solve import check_observation

            check_observation(route, observation)
            return (selected["image"], observation), "captured", ""
        sleep(0.005)
    raise TimeoutError(f"checkpoint {point['pose_id']} failed to settle before timeout")


def persist_observation(output, image, observation):
    image_path = output / "images" / f"{observation['pose_id']}.png"
    if not cv2.imwrite(str(image_path), image):
        raise OSError("failed to persist PNG")
    observation.update(
        image=str(image_path.relative_to(output)), image_sha256=file_digest(image_path)
    )
    path = output / "observations" / f"{observation['pose_id']}.json"
    write_json(path, observation)
    return {"file": str(path.relative_to(output)), "sha256": file_digest(path)}


def finalize_samples(output, run):
    observations = [read_json(output / entry["file"]) for entry in run["observations"]]
    run["selection"] = split_observations(observations)
    for entry, observation in zip(run["observations"], observations):
        path = output / entry["file"]
        write_json(path, observation)
        entry["sha256"] = file_digest(path)
    run["selection"]["skipped"] = sum(
        p["status"] == "skipped" for p in run["pose_results"]
    )
    run["selection"]["attempted"] = len(run["pose_results"])


def record_route(
    hardware, route, output: Path, *, session, prepared_window, show, open_window
):
    validate(route, replay=True)
    validate_session(session)
    if (
        hardware.camera.metadata["serial"]
        != session["station"]["cameras"][route["camera_role"]]
    ):
        raise ValueError("camera does not match current station")
    if output.exists():
        raise ValueError("run directory already exists; choose a new run")
    (output / "images").mkdir(parents=True)
    write_json(output / "route.json", route)
    run = {
        "schema_version": 2,
        "run_id": output.name,
        "status": "partial",
        "role": route["role"],
        "route_sha256": route_hash(route),
        "station": session["station"],
        "setup_id": session["setup_id"],
        "board": session["board"],
        "board_mount_id": identifier(),
        "camera": hardware.camera.metadata,
        "pose_results": [],
        "observations": [],
        "error": None,
    }
    write_json(output / "run.json", run)
    window = prepared_window or f"ARX5 calibration | {route['role']} | RECORD"
    if prepared_window is None:
        open_window(window)
    control = None
    try:
        limits = MotionLimits(**route["motion"])
        actual = hardware.arms.read()
        preflight_start(route, actual, limits)
        kin = Kinematics.from_payload(route["kinematics"])
        for side in ("left", "right"):
            kin.validate(actual[side]["q"])
        control = ReplayControl(hardware.arms, route["active_arm"], limits)
        control.start()
        board = Board(**session["board"])
        for index, point in enumerate(route["waypoints"]):
            captured, status, reason = capture_pose(
                hardware, control, route, board, point, index, window, show
            )
            if captured is not None:
                image, observation = captured
                run["observations"].append(
                    persist_observation(output, image, observation)
                )
            run["pose_results"].append(
                {"pose_id": point["pose_id"], "status": status, "reason": reason}
            )
            write_json(output / "run.json", run)
            control.require_ok()
            print(
                f"位姿 {index + 1}/{len(route['waypoints'])}：{status}"
                + (f"（{reason}）" if reason else "")
            )
        finalize_samples(output, run)
        run["status"] = (
            "completed"  # Traversal completed; calibration validity is separate.
        )
        return run
    except BaseException as error:
        run["error"] = str(error) or type(error).__name__
        raise
    finally:
        try:
            if control:
                control.stop()
        except BaseException as error:
            run.update(status="failed", error=f"stop failed: {error}")
            raise
        finally:
            if control:
                write_json(
                    output / "control-trace.json", {"samples": list(control.trace)}
                )
            write_json(output / "run.json", run)
            try:
                cv2.destroyWindow(window)
            except cv2.error:
                pass
