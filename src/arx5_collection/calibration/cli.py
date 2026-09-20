"""Four camera/arm entries, three explicit stages, deterministic station-local paths."""

from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

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
        "--urdf",
        type=Path,
        help="current X5 model for teaching and replay compatibility",
    )
    parser.add_argument(
        "--mapping", type=Path, help="optional JSON with six signs and offsets (rad)"
    )
    parser.add_argument(
        "--new-setup",
        action="store_true",
        help="start a fresh local assembly batch for teaching or replay",
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
    from .routes import new_route, portable_route, station_identity, validate

    stage = args.stage
    if stage is None:
        choice = (
            input(
                "标定阶段：1 示教（重力补偿 + Space） / 2 自动采集并计算 / 3 离线重算 [1]："
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
    if args.new_setup and stage not in {"teach", "record"}:
        raise ValueError("--new-setup is only valid with --teach or --record")
    if stage == "check":
        print(
            json.dumps(
                validate(portable_route(read_json(path)), args.role, replay=True),
                indent=2,
            )
        )
        return 0
    if stage == "verify":
        from .solve import verify_result

        if args.result is None:
            raise ValueError("--verify requires --result")
        print(json.dumps(verify_result(args.result.resolve()), indent=2))
        return 0
    if stage == "solve":
        paths = (
            [p.expanduser().resolve() for p in args.runs]
            if args.runs
            else latest_run_paths(root, args.role)
        )
        return compute_results(root, args.role, paths)
    from arx5_collection.production.config import load_configured_station
    from arx5_collection.production.lifecycle import termination_as_interrupt

    from .geometry import Kinematics
    from .hardware import PROFILE, Hardware
    from .motion import MotionLimits
    from .workflow import park, prepare_window, record, teach

    station = load_configured_station(args.station_config)
    if station.sdk_type != 2:
        raise ValueError("this calibration runtime requires X5 sdk_type=2")
    settings = _settings(root, args.new_setup)
    mapping = read_json(args.mapping) if args.mapping else None
    model = Kinematics.load(args.urdf or default_urdf(), mapping)
    session = {
        "station": station_identity(station),
        "setup_id": settings["setup_id"],
        "board": settings["board"],
    }
    if stage == "teach":
        route = new_route(args.role, model, PROFILE)
        print(f"跨工作台位姿文件：{path}")
    else:
        route = portable_route(read_json(path))
        report = validate(route, args.role, replay=True)
        if route["kinematics"] != model.payload():
            raise ValueError(
                "route joint model/mapping differs from current robot; select the matching --urdf/--mapping"
            )
        print(
            f"回放 {report['captures']} 个候选点；速度上限 {route['motion']['velocity_rad_s']} rad/s。"
        )
        print(
            "使用本站棋盘设置与相机；无效视角会跳过。双臂将先限速到 HOME，再到路线首姿；保持夹爪目标。请清空运动路径并松开双臂。"
        )
    session_id = identifier()
    limits = MotionLimits(**route["motion"])
    # A failed GUI must be detected before starting CAN/controllers.
    import cv2

    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise RuntimeError(
            "OpenCV display unavailable: set DISPLAY/XAUTHORITY on the hardware host"
        )
    with termination_as_interrupt(), ExitStack() as stack:
        stack.callback(cv2.destroyAllWindows)
        window = prepare_window(args.role, stage)
        hardware = stack.enter_context(
            Hardware(
                station,
                args.role,
                root / "logs" / session_id,
                limits,
                profile=route["profile"],
            )
        )
        try:
            if stage == "teach":
                teach(hardware, route, path, session=session, prepared_window=window)
            else:
                output = root / "runs" / args.role / session_id
                capture = record(
                    hardware, route, output, session=session, prepared_window=window
                )
                write_json(
                    root / "latest-runs" / f"{args.role}.json",
                    {
                        "setup_id": session["setup_id"],
                        "run": str(output.relative_to(root)),
                    },
                )
                print(
                    f"遍历完成：{output}；有效/跳过：{capture['selection']['accepted']}/{capture['selection']['skipped']}"
                )
        except Exception as error:
            print(f"标定中断：{error}", file=sys.stderr, flush=True)
            raise
        finally:
            # Runs on completion, Esc, Ctrl-C and exceptions while controller services remain alive.
            park(hardware, route["mode"] == "eye_to_hand")
    if stage == "record":
        # Computation starts only after parking and releasing all robot hardware.
        return compute_results(root, args.role, latest_run_paths(root, args.role))
    return 0


def latest_run_paths(root, role):
    selected = read_json(root / "latest-runs" / f"{role}.json")
    paths = []
    for candidate in ROLES:
        pointer = root / "latest-runs" / f"{candidate}.json"
        if pointer.exists():
            value = read_json(pointer)
            if value["setup_id"] == selected["setup_id"]:
                paths.append(root / value["run"])
    return paths


def compute_results(root, role, paths):
    from .solve import solve_runs

    if role not in [read_json(p / "route.json")["role"] for p in paths]:
        raise ValueError("selected role has no input run")
    output = root / "results" / identifier()
    try:
        result = solve_runs(paths, output)
    except ValueError as error:
        print(f"标定 valid=false：{error}；诊断目录：{output}", file=sys.stderr)
        return 2
    print(
        f"标定 valid={str(result['valid']).lower()}；范围：{result['status']}；世界系 overview_optical；结果：{output}"
    )
    return 0
