"""OpenCV Space-to-save teaching and independent replay/capture sessions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from time import monotonic, sleep

import cv2
import numpy as np

from .board import Board, detect, overlay, next_orientation
from .geometry import Kinematics
from .motion import MotionLimits, stationary
from .replay import ReplayControl, StableWindow, preflight_start
from .routes import finalize, validate, route_hash
from .storage import archive_route, file_digest, identifier, write_json


def _window(name):
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(name, 1100, 680)


def _key(name, image):
    cv2.imshow(name, image)
    key = cv2.waitKey(1) & 0xFF
    if cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE) < 1:
        return 27
    return key


def park(hardware, held_board, prompt=input):
    """Keep controllers alive until the user has physically supported/parked the arms."""
    if held_board:
        prompt("请支撑并取下夹持的标定板，完成后按 Enter 切换重力补偿：")
    hardware.arms.call("gravity_compensation")
    prompt("已进入重力补偿。请手动将双臂归位并支撑妥当，完成后按 Enter 结束：")


def teach(hardware, route, path: Path):
    board = Board(**route["board"])
    kin = Kinematics.from_payload(route["kinematics"])
    limits = MotionLimits(**route["motion"])
    window, gate = f"ARX5 calibration | {route['role']} | TEACH", StableWindow(limits)
    message, reference = "Move by hand; release and wait for green", None
    archive_route(path)
    write_json(path, route)
    hardware.arms.call("gravity_compensation")
    print(
        "重力补偿示教：Space 保存；V 保存过渡点；R 翻转角点编号；Backspace 撤销；Enter 完成；Esc 保存草稿退出。"
    )
    print(
        "请在板上标记固定物理内角点 A。每次 Space 前确认画面的 A 对准该点；编号反向时按 R。"
    )
    evidence_dir = path.parent / "teaching" / route["route_id"]
    evidence_dir.mkdir(parents=True, exist_ok=False)
    _window(window)
    last_frame = None
    detection = None
    try:
        while True:
            hardware.supervisor.require_running()
            frame, state = hardware.camera.latest(), hardware.arms.read()
            if frame["number"] != last_frame:
                detection = detect(frame["image"], board, reference=reference)
                if detection.corners is not None:
                    reference = detection.corners
                last_frame = frame["number"]
            stable = gate.update(state, monotonic())
            joint_error = ""
            try:
                for side in ("left", "right"):
                    kin.validate(state[side]["q"])
            except ValueError as error:
                joint_error = str(error)
            clock_error = frame.get("clock_error", "")
            fresh = not clock_error and monotonic() - frame["source_monotonic_s"] < 0.25
            ready = (
                detection.valid
                and stable
                and fresh
                and not joint_error
                and gate.since is not None
                and frame["source_monotonic_s"] > gate.since + 0.1
            )
            n = sum(p["kind"] == "capture" for p in route["waypoints"])
            lines = [
                f"{route['role']} | captured {n} | {'READY' if ready else clock_error or joint_error or detection.reason or 'hold still'}",
                "SPACE save | R flip A | V transit | Backspace undo | Enter finish | Esc draft",
                f"coverage {detection.coverage:.1%} | sharpness {detection.sharpness:.0f}",
                message,
            ]
            key = _key(window, overlay(frame["image"], board, detection, lines, ready))
            if key == ord("r") and detection.corners is not None:
                reference = next_orientation(detection.corners, board)
                detection.corners = reference
            elif key in (8, 127) and route["waypoints"]:
                route["waypoints"].pop()
                write_json(path, route)
                message = "Last waypoint removed"
            elif key in (32, ord("v")):
                capture = key == 32
                pressed_state = hardware.arms.read()
                pressed_reference = {
                    side: state[side]["q"] for side in ("left", "right")
                }
                fresh = (
                    not clock_error and monotonic() - frame["source_monotonic_s"] < 0.25
                )
                if (
                    joint_error
                    or not stable
                    or not stationary(pressed_state, pressed_reference, limits)
                    or (capture and (not ready or not fresh))
                ):
                    message = "Not saved: " + (
                        clock_error
                        or detection.reason
                        or "wait for fresh, stationary view"
                    )
                    continue
                state = pressed_state
                p = {
                    "pose_id": identifier(),
                    "q": {s: list(state[s]["q"]) for s in ("left", "right")},
                    "kind": "capture" if capture else "via",
                    "split": ("validation" if (n + 1) % 5 == 0 else "training")
                    if capture
                    else None,
                    "teaching": {
                        "actual": state,
                        "origin_confirmed": capture,
                        "corners": detection.payload()["corners"] if capture else None,
                    },
                }
                candidate = deepcopy(route)
                candidate["waypoints"].append(p)
                try:
                    validate(candidate)
                except ValueError as error:
                    message = f"Not saved: {error}"
                    continue
                image_path = evidence_dir / f"{p['pose_id']}.png"
                if not cv2.imwrite(str(image_path), frame["image"]):
                    raise OSError("cannot save teaching preview")
                p["teaching"].update(
                    {
                        "image": str(image_path.relative_to(path.parent)),
                        "image_sha256": file_digest(image_path),
                        "frame": {k: v for k, v in frame.items() if k != "image"},
                    }
                )
                route["waypoints"].append(p)
                write_json(path, route)
                message = f"Saved {p['kind']} ({p['split'] or 'transit'})"
            elif key in (10, 13):
                try:
                    completed = finalize(route)
                except ValueError as error:
                    message = str(error)
                    continue
                write_json(path, completed)
                print(f"合法位姿 JSON 已保存：{path}")
                return completed
            elif key == 27:
                print(f"草稿已保存（尚不可回放）：{path}")
                return route
            sleep(0.005)
    finally:
        cv2.destroyWindow(window)


def record(hardware, route, output: Path):
    validate(route, replay=True)
    limits, board = MotionLimits(**route["motion"]), Board(**route["board"])
    kin = Kinematics.from_payload(route["kinematics"])
    if output.exists():
        raise ValueError("run directory already exists; choose a new run")
    output.mkdir(parents=True)
    (output / "images").mkdir()
    write_json(output / "route.json", route)
    run = {
        "schema_version": 1,
        "run_id": output.name,
        "status": "partial",
        "role": route["role"],
        "route_sha256": route_hash(route),
        "camera": hardware.camera.metadata,
        "observations": [],
        "error": None,
    }
    write_json(output / "run.json", run)
    window = f"ARX5 calibration | {route['role']} | RECORD"
    _window(window)
    control = None
    try:
        actual = hardware.arms.read()
        preflight_start(route, actual, limits)
        for side in ("left", "right"):
            kin.validate(actual[side]["q"])
        control = ReplayControl(hardware.arms, route["active_arm"], limits)
        control.start()
        for index, point in enumerate(route["waypoints"]):
            deadline = monotonic() + limits.segment_timeout_s
            expected_end = control.move(point["q"][route["active_arm"]])
            gate, capture_started, best = StableWindow(limits), None, None
            seen, samples = None, []
            detection = None
            while monotonic() < deadline:
                control.require_ok()
                hardware.supervisor.require_running()
                frame, sample = hardware.camera.latest(), hardware.arms.read()
                now = monotonic()
                stable = (
                    gate.update(sample, now, control.target()) and now >= expected_end
                )
                if capture_started is not None:
                    samples.append(sample)
                if stable and point["kind"] == "via":
                    break
                if not stable:
                    if capture_started is not None:
                        raise RuntimeError("arm moved during capture window")
                elif capture_started is None:
                    capture_started = now
                if frame["number"] != seen:
                    detection = detect(
                        frame["image"], board, reference=point["teaching"]["corners"]
                    )
                    seen = frame["number"]
                    within = (
                        not frame.get("clock_error")
                        and capture_started is not None
                        and capture_started + 0.1 <= frame["source_monotonic_s"] <= now
                        and now - frame["received_monotonic_s"] < 0.25
                    )
                    if detection.valid and within:
                        # Same physical pose should preserve taught image corner numbering.
                        distance = np.max(
                            np.linalg.norm(
                                detection.corners
                                - np.asarray(point["teaching"]["corners"]),
                                axis=1,
                            )
                        )
                        if distance > 0.10 * max(frame["image"].shape[:2]):
                            raise RuntimeError(
                                "board/setup differs from taught view; re-teach this route"
                            )
                        if best is None or detection.sharpness > best[0]:
                            best = (
                                detection.sharpness,
                                frame,
                                deepcopy(sample),
                                detection.payload(),
                            )
                status = (
                    "CAPTURE"
                    if capture_started
                    else "SETTLE"
                    if now >= expected_end
                    else "MOVE"
                )
                lines = [
                    f"{route['role']} | {index + 1}/{len(route['waypoints'])} | {status}",
                    f"max speed {limits.velocity_rad_s:.2f} rad/s | Esc stop",
                    frame.get("clock_error") or detection.reason,
                ]
                if (
                    _key(
                        window,
                        overlay(
                            frame["image"],
                            board,
                            detection,
                            lines,
                            stable and detection.valid and not frame.get("clock_error"),
                        ),
                    )
                    == 27
                ):
                    raise KeyboardInterrupt("operator stopped calibration")
                if capture_started and now - capture_started >= limits.capture_s:
                    if best is None:
                        raise RuntimeError(
                            "no valid fresh view in stationary capture window"
                        )
                    _, selected, actual, detected = best
                    raw_states = hardware.arms.evidence(capture_started - 0.2, now)
                    image_path = output / "images" / f"{point['pose_id']}.png"
                    if not cv2.imwrite(str(image_path), selected["image"]):
                        raise OSError("failed to persist PNG")
                    observation = {
                        "pose_id": point["pose_id"],
                        "split": point["split"],
                        "image": str(image_path.relative_to(output)),
                        "image_sha256": file_digest(image_path),
                        "frame": {k: v for k, v in selected.items() if k != "image"},
                        "actual": actual,
                        "window": {"start": capture_started, "end": now},
                        "window_samples": samples,
                        "detection": detected,
                        "T_base_link6": kin.fk(
                            actual[route["active_arm"]]["q"]
                        ).tolist(),
                        "raw_states": raw_states,
                    }
                    from .solve import check_observation

                    check_observation(route, observation)
                    observation_path = (
                        output / "observations" / f"{point['pose_id']}.json"
                    )
                    write_json(observation_path, observation)
                    run["observations"].append(
                        {
                            "file": str(observation_path.relative_to(output)),
                            "sha256": file_digest(observation_path),
                        }
                    )
                    write_json(output / "run.json", run)
                    control.require_ok()  # Do not advance if PNG encoding exhausted the motion watchdog.
                    break
                sleep(0.005)
            else:
                raise TimeoutError(f"checkpoint {point['pose_id']} timed out")
        run["status"] = "completed"
        return run
    except BaseException as error:
        run["error"] = str(error) or type(error).__name__
        raise
    finally:
        try:
            if control:
                control.stop()
        except BaseException as error:
            run["status"] = "failed"
            run["error"] = f"stop failed: {error}"
            raise
        finally:
            if control:
                write_json(
                    output / "control-trace.json", {"samples": list(control.trace)}
                )
            write_json(output / "run.json", run)
            cv2.destroyWindow(window)
