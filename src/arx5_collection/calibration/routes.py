"""Portable joint routes. Device identity and visual evidence belong to a run."""

from copy import deepcopy
from dataclasses import asdict

import numpy as np

from .geometry import Kinematics, observable
from .motion import MotionLimits, Segment, vector
from .profiles import validate_profile
from .storage import ROLES, digest, identifier

ROUTE_FIELDS = {
    "schema_version",
    "route_id",
    "role",
    "mode",
    "active_arm",
    "camera_role",
    "joint_unit",
    "kinematics",
    "profile",
    "motion",
    "draft",
    "waypoints",
}


def new_route(role, kinematics, profile):
    camera, arm, mode = ROLES[role]
    return {
        "schema_version": 2,
        "route_id": identifier(),
        "role": role,
        "mode": mode,
        "active_arm": arm,
        "camera_role": camera,
        "joint_unit": "rad",
        "kinematics": kinematics.payload(),
        "profile": validate_profile(profile),
        "motion": asdict(MotionLimits()),
        "draft": True,
        "waypoints": [],
    }


def portable_route(value):
    """Import old routes without transferring station claims or measured pixels."""
    if value.get("schema_version") == 2:
        return deepcopy(value)
    if value.get("schema_version") != 1:
        raise ValueError("unsupported calibration route schema")
    result = {k: deepcopy(value[k]) for k in ROUTE_FIELDS if k != "waypoints"}
    result["schema_version"] = 2
    result["waypoints"] = [
        {k: deepcopy(p[k]) for k in ("pose_id", "q", "kind")}
        for p in value["waypoints"]
    ]
    validate(result)
    return result


def route_hash(route):
    return digest(route)


def validate(route, expected_role=None, replay=False):
    if (
        set(route) != ROUTE_FIELDS
        or route["schema_version"] != 2
        or route["joint_unit"] != "rad"
    ):
        raise ValueError("unsupported portable route schema/units")
    if route["role"] not in ROLES or expected_role not in {None, route["role"]}:
        raise ValueError("route role does not match selected camera/arm")
    camera, arm, mode = ROLES[route["role"]]
    if (route["camera_role"], route["active_arm"], route["mode"]) != (
        camera,
        arm,
        mode,
    ):
        raise ValueError("inconsistent route roles/mode")
    if not isinstance(route["route_id"], str) or not route["route_id"].strip():
        raise ValueError("missing route_id")
    validate_profile(route["profile"])
    kin = Kinematics.from_payload(route["kinematics"])
    limits = MotionLimits(**route["motion"])
    if type(route["draft"]) is not bool or not isinstance(route["waypoints"], list):
        raise ValueError("invalid draft/waypoints")
    ids, previous, captures = set(), None, []
    other = "right" if arm == "left" else "left"
    parked = None
    for point in route["waypoints"]:
        if set(point) != {"pose_id", "q", "kind"} or point["kind"] not in {
            "capture",
            "via",
        }:
            raise ValueError("invalid portable waypoint schema")
        pose_id = point["pose_id"]
        if not isinstance(pose_id, str) or not pose_id or pose_id in ids:
            raise ValueError("duplicate/invalid pose_id")
        ids.add(pose_id)
        if set(point["q"]) != {"left", "right"}:
            raise ValueError("both arm positions required")
        for side in ("left", "right"):
            kin.validate(point["q"][side])
        if parked is None:
            parked = vector(point["q"][other])
        if np.max(np.abs(parked - vector(point["q"][other]))) > limits.arrival_rad:
            raise ValueError("parked arm changed between taught waypoints")
        if previous is not None:
            Segment(previous["q"][arm], point["q"][arm], limits)
        previous = point
        if point["kind"] == "capture":
            if any(
                np.max(np.abs(vector(point["q"][arm]) - vector(old["q"][arm]))) < 0.025
                for old in captures
            ):
                raise ValueError("duplicate or near-identical capture pose")
            captures.append(point)
    if replay:
        if route["draft"] or len(captures) < 20:
            raise ValueError("finish route: at least 20 candidate capture poses")
        observable([kin.fk(p["q"][arm]) for p in captures])
    return {
        "role": route["role"],
        "captures": len(captures),
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


def validate_session(session):
    from .board import Board

    identity = session["station"]
    if (
        set(identity) != {"station_id", "arms", "cameras", "sdk_type"}
        or set(identity["arms"]) != {"left", "right"}
        or set(identity["cameras"]) != {"left", "right", "overview"}
        or identity["sdk_type"] != 2
        or not all(
            isinstance(v, str) and v.strip()
            for v in [
                identity["station_id"],
                *identity["arms"].values(),
                *identity["cameras"].values(),
            ]
        )
    ):
        raise ValueError("invalid current station identity")
    if not isinstance(session["setup_id"], str) or not session["setup_id"]:
        raise ValueError("missing current setup_id")
    Board(**session["board"])


def split_observations(observations):
    """Choose held-out views by acquisition order, before seeing fit residuals."""
    count = len(observations)
    validation_count = min(count, max(5, int(np.ceil(count * 0.2))))
    heldout = (
        set(np.linspace(0, count - 1, validation_count, dtype=int)) if count else set()
    )
    for index, observation in enumerate(observations):
        observation["split"] = "validation" if index in heldout else "training"
    return {
        "accepted": count,
        "training": count - validation_count,
        "validation": validation_count,
    }
