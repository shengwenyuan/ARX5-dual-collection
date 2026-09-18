"""Four camera/arm entries, three explicit stages, deterministic station-local paths."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .storage import DEFAULT_ROOT, ROLES, identifier, read_json, route_path, write_json


def add_parser(subcommands):
    parser = subcommands.add_parser(
        "cali", help="teach, capture and solve four-route camera calibration"
    )
    roles = parser.add_mutually_exclusive_group(required=True)
    for role in ROLES:
        roles.add_argument(f"--{role}", dest="role", action="store_const", const=role)
    stages = parser.add_mutually_exclusive_group()
    for stage in ("teach", "record", "solve", "check", "verify"):
        stages.add_argument(
            f"--{stage}", dest="stage", action="store_const", const=stage
        )
    parser.add_argument(
        "--station-config",
        type=Path,
        default=Path("/var/lib/arx5-collection/station.json"),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(os.environ.get("ARX5_CALI_ROOT", str(DEFAULT_ROOT))),
    )
    parser.add_argument("--poses", type=Path, help="override role-specific route JSON")
    parser.add_argument(
        "--runs", type=Path, nargs="+", help="explicit completed runs for offline solve"
    )
    parser.add_argument(
        "--result", type=Path, help="result directory for offline verification"
    )
    parser.add_argument(
        "--urdf", type=Path, help="X5 model used when teaching a new route"
    )
    parser.add_argument(
        "--mapping", type=Path, help="optional JSON with six signs and offsets (rad)"
    )
    parser.add_argument(
        "--new-setup",
        action="store_true",
        help="teach a fresh assembly batch after remounting",
    )
    return parser


def default_urdf():
    candidates = [
        Path(__file__).resolve().parents[3] / "config/calibration/X5.urdf",
        Path(sys.prefix) / "share/arx5-collection/calibration/X5.urdf",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError("X5 URDF missing; provide --urdf or reinstall package data")


def _settings(root, new_setup):
    from .board import Board

    path = root / "setup.json"
    if path.exists() and not new_setup:
        value = read_json(path)
        Board(**value["board"])
        return value
    print("首次设置：请输入棋盘内角点数量和准确单格尺寸。尺寸用 mm。")
    columns = int(input("内角点列数："))
    rows = int(input("内角点行数："))
    square = float(input("单格边长（mm）："))
    board = Board(columns, rows, square)
    value = {"schema_version": 1, "setup_id": identifier(), "board": board.payload()}
    write_json(path, value)
    return value


def run(args):
    try:
        return _run(args)
    except ImportError as error:
        raise RuntimeError(
            "calibration dependencies unavailable; install '.[calibration]' or build the calibration image"
        ) from error
    except (KeyError, TypeError) as error:
        raise ValueError(f"invalid calibration data: {error}") from error


def _run(args):
    # Imports remain lazy: collect/DAgger and cali --help need no numerical/GUI packages.
    from .routes import new_route, validate, station_identity

    stage = args.stage
    if stage is None:
        choice = (
            input(
                "标定阶段：1 示教（重力补偿 + Space） / 2 自动采集 / 3 离线计算 [1]："
            ).strip()
            or "1"
        )
        try:
            stage = {"1": "teach", "2": "record", "3": "solve"}[choice]
        except KeyError as error:
            raise ValueError("请选择 1、2 或 3") from error
    root = args.data_root.expanduser().resolve()
    path = (
        args.poses.expanduser().resolve() if args.poses else route_path(root, args.role)
    )
    if args.new_setup and stage != "teach":
        raise ValueError("--new-setup is only valid with --teach")
    if stage == "check":
        print(json.dumps(validate(read_json(path), args.role, replay=True), indent=2))
        return 0
    if stage == "verify":
        from .solve import verify_result

        if args.result is None:
            raise ValueError("--verify requires --result")
        print(json.dumps(verify_result(args.result.resolve()), indent=2))
        return 0
    if stage == "solve":
        from .solve import solve_runs

        if args.runs:
            paths = [p.expanduser().resolve() for p in args.runs]
        else:
            selected = read_json(root / "latest-runs" / f"{args.role}.json")
            paths = []
            for role in ROLES:
                pointer = root / "latest-runs" / f"{role}.json"
                if pointer.exists():
                    value = read_json(pointer)
                    if value["setup_id"] == selected["setup_id"]:
                        paths.append(root / value["run"])
        if args.role not in [read_json(p / "route.json")["role"] for p in paths]:
            raise ValueError("selected role has no input run")
        output = root / "results" / identifier()
        result = solve_runs(paths, output)
        print(
            f"计算完成：{output}\n范围：{result['status']}；世界系 overview_optical。未自动激活到生产。"
        )
        return 0
    from arx5_collection.production.config import load_configured_station
    from arx5_collection.production.lifecycle import termination_as_interrupt
    from .board import Board
    from .geometry import Kinematics
    from .hardware import Hardware, PROFILE
    from .motion import MotionLimits
    from .workflow import park, record, teach

    station = load_configured_station(args.station_config)
    if station.sdk_type != 2:
        raise ValueError("this calibration runtime requires X5 sdk_type=2")
    if stage == "teach":
        settings = _settings(root, args.new_setup)
        mapping = read_json(args.mapping) if args.mapping else None
        model = Kinematics.load(args.urdf or default_urdf(), mapping)
        route = new_route(
            args.role,
            station_identity(station),
            Board(**settings["board"]),
            model,
            settings["setup_id"],
            PROFILE,
        )
        print(f"位姿文件：{path}")
    else:
        route = read_json(path)
        report = validate(route, args.role, replay=True)  # Before opening any hardware.
        if route["station"] != station_identity(station):
            raise ValueError("route belongs to another station/device binding")
        if (root / "setup.json").exists() and read_json(root / "setup.json")[
            "setup_id"
        ] != route["setup_id"]:
            raise ValueError("route belongs to a previous assembly setup; re-teach")
        print(
            f"回放 {report['captures']} 个采集点；速度上限 {route['motion']['velocity_rad_s']} rad/s。"
        )
        print("请确认板固定方式、装配和路径仍与示教一致，并将双臂放到首个记录姿态。")
    session_id = identifier()
    limits = MotionLimits(**route["motion"])
    # A failed GUI must be detected before starting CAN/controllers.
    import cv2

    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "OpenCV display unavailable: set DISPLAY/XAUTHORITY on the hardware host"
        )
    cv2.namedWindow("ARX5 calibration startup", cv2.WINDOW_NORMAL)
    cv2.destroyWindow("ARX5 calibration startup")
    with (
        termination_as_interrupt(),
        Hardware(station, args.role, root / "logs" / session_id, limits) as hardware,
    ):
        try:
            if stage == "teach":
                teach(hardware, route, path)
            else:
                output = root / "runs" / args.role / session_id
                record(hardware, route, output)
                write_json(
                    root / "latest-runs" / f"{args.role}.json",
                    {
                        "setup_id": route["setup_id"],
                        "run": str(output.relative_to(root)),
                    },
                )
                print(f"采集完成：{output}；离线计算请重新运行入口并选择 3。")
        finally:
            # Runs on completion, Esc, Ctrl-C and exceptions while controller services remain alive.
            park(hardware, route["mode"] == "eye_to_hand")
    return 0
