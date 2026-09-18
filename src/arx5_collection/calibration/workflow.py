"""OpenCV Space-to-save teaching and independent replay/capture sessions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from time import monotonic, sleep

import cv2
import numpy as np

from .board import Board, detect
from .geometry import Kinematics
from .motion import MotionLimits, stationary
from .replay import ReplayControl, StableWindow, TeachFeedback, preflight_start
from .preview import ROLE_NAMES, WINDOW_CLOSED, live_checks, route_checks, render, startup_image
from .routes import finalize, route_hash, validate
from .storage import archive_route, file_digest, identifier, write_json
from .timing import capture_ready


def _window(name):
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(name, 1500, 850)


def prepare_window(role, stage):
    """Create and actually render the session window before opening hardware."""
    name = f"ARX5 calibration | {ROLE_NAMES[role]} | {stage.upper()}"
    _window(name)
    image = startup_image()
    cv2.imshow(name, image)
    if cv2.waitKey(30) & 0xFF == 27:
        raise KeyboardInterrupt("operator cancelled before opening hardware")
    return name


def _key(name, image):
    cv2.imshow(name, image)
    key = cv2.waitKey(1) & 0xFF
    try:
        visible = cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE)
    except cv2.error:
        visible = -1
    if visible < 1:
        return WINDOW_CLOSED
    return key


def park(hardware, held_board, prompt=input):
    """Keep controllers alive until the user has physically supported/parked the arms."""
    if held_board:
        prompt("请支撑并取下夹持的标定板，完成后按 Enter 切换重力补偿：")
    hardware.arms.call("gravity_compensation")
    prompt("已进入重力补偿。请手动将双臂归位并支撑妥当，完成后按 Enter 结束：")


def teach(hardware, route, path: Path, *, prepared_window=None):
    board = Board(**route["board"])
    kin = Kinematics.from_payload(route["kinematics"])
    limits = MotionLimits(**route["motion"])
    window = prepared_window or f"ARX5 calibration | {route['role']} | TEACH"
    gate = StableWindow(limits)
    feedback = TeachFeedback(hardware.arms, limits, gate, clock=monotonic)
    message, reference = "Move by hand, release and wait for each check", None
    archive_route(path)
    write_json(path, route)
    hardware.arms.call("gravity_compensation")
    print("重力补偿示教：Space 保存；Backspace 删除上一条；关闭窗口完成。")
    print("首次按屏幕 A 标记物理角点；之后每次 Space 确认 A 仍对应同一点。")
    evidence_dir = path.parent / "teaching" / route["route_id"]
    evidence_dir.mkdir(parents=True, exist_ok=False)
    if prepared_window is None:
        _window(window)
    last_frame, detection = None, None
    try:
        while True:
            hardware.supervisor.require_running()
            frame = hardware.camera.latest()
            if frame["number"] != last_frame:
                detection = detect(frame["image"], board, reference=reference)
                if detection.corners is not None:
                    reference = detection.corners
                last_frame = frame["number"]
            # Read AFTER the potentially slow detector, never age a pre-detection snapshot.
            hardware.supervisor.require_running()
            state, stable = feedback.read()
            checks = live_checks(frame, detection, board, state, kin, limits, gate, stable, monotonic())
            checks += route_checks(route, state, limits)
            ready = all(check.passed for check in checks)
            n = sum(p["kind"] == "capture" for p in route["waypoints"])
            footer = [
                "SPACE save | BACKSPACE delete last",
                "Close window to finish; confirm physical A before saving",
                message,
            ]
            key = _key(window, render(
                frame["image"], board, detection,
                f"{ROLE_NAMES[route['role']]} | saved {n} | {'READY' if ready else 'WAIT'}",
                checks, footer,
            ))
            if key in (8, 127):
                if route["waypoints"]:
                    route["waypoints"].pop()
                    write_json(path, route)
                    message = "Last pose deleted"
                else:
                    message = "No saved pose to delete"
            elif key == 32:
                # GUI rendering can also take time. Re-read and revalidate at the keypress.
                hardware.supervisor.require_running()
                pressed_state, pressed_stable = feedback.read()
                pressed_checks = live_checks(frame, detection, board, pressed_state, kin, limits, gate, pressed_stable, monotonic())
                pressed_checks += route_checks(route, pressed_state, limits)
                failed = [check.label for check in pressed_checks if not check.passed]
                if not stationary(pressed_state, {s: state[s]["q"] for s in ("left", "right")}, limits):
                    failed.append("pose changed at keypress")
                if not ready or failed:
                    message = "Not saved: " + ", ".join((failed or [c.label for c in checks if not c.passed])[:3])
                    continue
                state = pressed_state
                p = {
                    "pose_id": identifier(),
                    "q": {s: list(state[s]["q"]) for s in ("left", "right")},
                    "kind": "capture",
                    "split": "validation" if (n + 1) % 5 == 0 else "training",
                    "teaching": {
                        "actual": state,
                        "origin_confirmed": True,
                        "corners": detection.payload()["corners"],
                    },
                }
                candidate = deepcopy(route)
                candidate["waypoints"].append(p)
                try:
                    validate(candidate)
                except ValueError as error:
                    print(f"位姿校验未通过：{error}")
                    message = "Not saved: route check failed; see terminal"
                    continue
                image_path = evidence_dir / f"{p['pose_id']}.png"
                if not cv2.imwrite(str(image_path), frame["image"]):
                    raise OSError("cannot save teaching preview")
                p["teaching"].update({
                    "image": str(image_path.relative_to(path.parent)),
                    "image_sha256": file_digest(image_path),
                    "frame": {k: v for k, v in frame.items() if k != "image"},
                })
                route["waypoints"].append(p)
                write_json(path, route)
                message = f"Saved pose {n + 1} ({p['split']})"
            elif key == WINDOW_CLOSED:
                try:
                    completed = finalize(route)
                except ValueError as error:
                    print(f"草稿已保存（尚不可回放）：{path}；{error}")
                    return route
                write_json(path, completed)
                print(f"合法位姿 JSON 已保存：{path}")
                return completed
            sleep(0.005)
    finally:
        # HighGUI may already have destroyed the window via its close button.
        try:
            cv2.destroyWindow(window)
        except cv2.error:
            pass


def record(hardware, route, output: Path, *, prepared_window=None):
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
    window = prepared_window or f"ARX5 calibration | {route['role']} | RECORD"
    if prepared_window is None:
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
                frame = hardware.camera.latest()
                new_frame = frame["number"] != seen
                if new_frame:
                    detection = detect(frame["image"], board, reference=point["teaching"]["corners"])
                    seen = frame["number"]
                control.require_ok()
                sample = hardware.arms.read()
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
                if new_frame:
                    within = (
                        capture_ready(frame, now)
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
                status = "CAPTURE" if capture_started else "SETTLE" if now >= expected_end else "MOVE"
                checks = live_checks(frame, detection, board, sample, kin, limits, gate, stable, monotonic())
                if _key(window, render(
                    frame["image"], board, detection,
                    f"{ROLE_NAMES[route['role']]} | {index + 1}/{len(route['waypoints'])} | {status}",
                    checks,
                    [f"Max speed {limits.velocity_rad_s:.2f} rad/s", "Esc or close window to stop"],
                )) in (27, WINDOW_CLOSED):
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
            try:
                cv2.destroyWindow(window)
            except cv2.error:
                pass
