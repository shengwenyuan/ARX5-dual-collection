"""Route validation is offline and completes before any hardware is opened."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

import numpy as np

from .board import Board
from .geometry import Kinematics, observable
from .motion import MotionLimits, Segment, vector
from .profiles import validate_profile
from .storage import ROLES, digest, identifier


def new_route(role, station, board, kinematics, setup_id, profile):
    camera, arm, mode = ROLES[role]
    return {
        "schema_version": 1,
        "route_id": identifier(),
        "role": role,
        "mode": mode,
        "active_arm": arm,
        "camera_role": camera,
        "station": station,
        "setup_id": setup_id,
        "board_mount_id": identifier(),
        "joint_unit": "rad",
        "board": board.payload(),
        "kinematics": kinematics.payload(),
        "profile": validate_profile(profile),
        "motion": asdict(MotionLimits()),
        "draft": True,
        "waypoints": [],
    }


def route_hash(route):
    return digest(route)


def validate(route, expected_role=None, replay=False):
    required = {
        "schema_version",
        "route_id",
        "role",
        "mode",
        "active_arm",
        "camera_role",
        "station",
        "setup_id",
        "board_mount_id",
        "joint_unit",
        "board",
        "kinematics",
        "profile",
        "motion",
        "draft",
        "waypoints",
    }
    if (
        set(route) != required
        or route["schema_version"] != 1
        or route["joint_unit"] != "rad"
    ):
        raise ValueError("unsupported calibration route schema/units")
    if route["role"] not in ROLES or expected_role not in {None, route["role"]}:
        raise ValueError("route role does not match selected camera/arm")
    camera, arm, mode = ROLES[route["role"]]
    if (route["camera_role"], route["active_arm"], route["mode"]) != (
        camera,
        arm,
        mode,
    ):
        raise ValueError("inconsistent route roles/mode")
    for key in ("route_id", "setup_id", "board_mount_id"):
        if not isinstance(route[key], str) or not route[key].strip():
            raise ValueError(f"missing {key}")
    identity = route["station"]
    if (
        set(identity) != {"station_id", "arms", "cameras", "sdk_type"}
        or set(identity["arms"]) != {"left", "right"}
        or set(identity["cameras"]) != {"left", "right", "overview"}
        or identity["sdk_type"] != 2
        or not all(
            isinstance(s, str) and s.strip()
            for s in [
                identity["station_id"],
                *identity["arms"].values(),
                *identity["cameras"].values(),
            ]
        )
    ):
        raise ValueError("invalid X5 v2 station identity")
    validate_profile(route["profile"])
    Board(**route["board"])
    kin = Kinematics.from_payload(route["kinematics"])
    limits = MotionLimits(**route["motion"])
    if type(route["draft"]) is not bool or not isinstance(route["waypoints"], list):
        raise ValueError("invalid draft/waypoints")
    ids, previous, captures = set(), None, []
    other = "right" if arm == "left" else "left"
    parked = None
    for p in route["waypoints"]:
        if set(p) != {"pose_id", "q", "kind", "split", "teaching"} or p["kind"] not in {
            "capture",
            "via",
        }:
            raise ValueError("invalid waypoint schema")
        if not isinstance(p["pose_id"], str) or not p["pose_id"] or p["pose_id"] in ids:
            raise ValueError("duplicate/invalid pose_id")
        ids.add(p["pose_id"])
        if set(p["q"]) != {"left", "right"}:
            raise ValueError("both actual arm positions required")
        for side in ("left", "right"):
            kin.validate(p["q"][side])
        if parked is None:
            parked = vector(p["q"][other])
        if np.max(np.abs(parked - vector(p["q"][other]))) > limits.arrival_rad:
            raise ValueError("parked arm changed between taught waypoints")
        if previous is not None:
            Segment(previous["q"][arm], p["q"][arm], limits)
        previous = p
        if p["kind"] == "capture":
            if p["split"] not in {"training", "validation"}:
                raise ValueError("capture needs training/validation split")
            if not p["teaching"].get("origin_confirmed"):
                raise ValueError(
                    "mark physical A corner; confirm overlay before saving"
                )
            corners = np.asarray(p["teaching"].get("corners"), float)
            if (
                corners.shape != (route["board"]["rows"] * route["board"]["columns"], 2)
                or not np.isfinite(corners).all()
            ):
                raise ValueError("missing taught corner evidence")
            if any(
                np.max(np.abs(vector(p["q"][arm]) - vector(old["q"][arm]))) < 0.025
                for old in captures
            ):
                raise ValueError("duplicate or near-identical capture pose")
            captures.append(p)
        elif p["split"] is not None:
            raise ValueError("via point cannot enter training/validation")
    training = [p for p in captures if p["split"] == "training"]
    validation = [p for p in captures if p["split"] == "validation"]
    if replay:
        if route["draft"] or len(training) < 15 or len(validation) < 5:
            raise ValueError("finish route: at least 15 training and 5 held-out poses")
        observable([kin.fk(p["q"][arm]) for p in training])
    return {
        "role": route["role"],
        "captures": len(captures),
        "training": len(training),
        "validation": len(validation),
        "route_sha256": route_hash(route),
    }


def finalize(route):
    result = deepcopy(route)
    result["draft"] = False
    validate(result, replay=True)
    return result


def station_identity(station):
    return {
        "station_id": station.station_id,
        "sdk_type": station.sdk_type,
        "arms": {a.role: a.usb_serial for a in station.arms},
        "cameras": {c.role: c.serial_number for c in station.cameras},
    }
