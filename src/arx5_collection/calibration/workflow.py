"""OpenCV Space-to-save teaching and independent replay/capture sessions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from time import monotonic, sleep

import cv2

from .board import Board, detect
from .geometry import Kinematics
from .motion import MotionLimits, stationary
from .replay import StableWindow, TeachFeedback
from .preview import (
    ROLE_NAMES,
    WINDOW_CLOSED,
    live_checks,
    route_checks,
    render,
    startup_image,
)
from .routes import finalize, validate
from .storage import archive_route, file_digest, identifier, write_json


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


def teach(hardware, route, path: Path, *, session, prepared_window=None):
    board = Board(**session["board"])
    kin = Kinematics.from_payload(route["kinematics"])
    limits = MotionLimits(**route["motion"])
    window = prepared_window or f"ARX5 calibration | {route['role']} | TEACH"
    gate = StableWindow(limits)
    feedback = TeachFeedback(hardware.arms, limits, gate, clock=monotonic)
    reference = None
    archive_route(path)
    write_json(path, route)
    hardware.arms.call("gravity_compensation")
    print("重力补偿示教：Space 保存；Backspace 删除上一条；关闭窗口完成。")
    print("路径只保存关节目标；本次示教图像和反馈另存为证据。")
    evidence_dir = path.parent / "teaching" / route["route_id"]
    evidence_dir.mkdir(parents=True, exist_ok=False)
    if prepared_window is None:
        _window(window)
    write_json(evidence_dir / "session.json", session)
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
            checks = live_checks(
                frame, detection, board, state, kin, limits, gate, stable, monotonic()
            )
            checks += route_checks(route, state, limits)
            ready = all(check.passed for check in checks)
            n = sum(p["kind"] == "capture" for p in route["waypoints"])
            footer = [
                "SPACE save | BACKSPACE delete last",
            ]
            key = _key(
                window,
                render(
                    frame["image"],
                    board,
                    detection,
                    f"{ROLE_NAMES[route['role']]} | saved {n} | {'READY' if ready else 'WAIT'}",
                    checks,
                    footer,
                ),
            )
            if key in (8, 127):
                if route["waypoints"]:
                    route["waypoints"].pop()
                    write_json(path, route)
                    print("已删除上一条位姿")
                else:
                    print("当前没有可删除的位姿")
            elif key == 32:
                # GUI rendering can also take time. Re-read and revalidate at the keypress.
                hardware.supervisor.require_running()
                pressed_state, pressed_stable = feedback.read()
                pressed_checks = live_checks(
                    frame,
                    detection,
                    board,
                    pressed_state,
                    kin,
                    limits,
                    gate,
                    pressed_stable,
                    monotonic(),
                )
                pressed_checks += route_checks(route, pressed_state, limits)
                failed = [check.label for check in pressed_checks if not check.passed]
                if not stationary(
                    pressed_state, {s: state[s]["q"] for s in ("left", "right")}, limits
                ):
                    failed.append("pose changed at keypress")
                if not ready or failed:
                    print(
                        "未保存："
                        + ", ".join(
                            (failed or [c.label for c in checks if not c.passed])[:3]
                        )
                    )
                    continue
                state = pressed_state
                p = {
                    "pose_id": identifier(),
                    "q": {s: list(state[s]["q"]) for s in ("left", "right")},
                    "kind": "capture",
                }
                candidate = deepcopy(route)
                candidate["waypoints"].append(p)
                try:
                    validate(candidate)
                except ValueError as error:
                    print(f"位姿校验未通过：{error}")
                    continue
                image_path = evidence_dir / f"{p['pose_id']}.png"
                if not cv2.imwrite(str(image_path), frame["image"]):
                    raise OSError("cannot save teaching preview")
                write_json(
                    evidence_dir / f"{p['pose_id']}.json",
                    {
                        "actual": state,
                        "corners": detection.payload()["corners"],
                        "image": image_path.name,
                        "image_sha256": file_digest(image_path),
                        "frame": {k: v for k, v in frame.items() if k != "image"},
                    },
                )
                route["waypoints"].append(p)
                write_json(path, route)
                print(f"已保存第 {n + 1} 条候选位姿")
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


def record(hardware, route, output: Path, *, session, prepared_window=None):
    from .capture import record_route

    return record_route(
        hardware,
        route,
        output,
        session=session,
        prepared_window=prepared_window,
        show=_key,
        open_window=_window,
    )
