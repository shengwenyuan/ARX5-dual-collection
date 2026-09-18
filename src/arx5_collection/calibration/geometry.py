"""Explicit T_A_B transforms, URDF FK, and both hand-eye directions."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import cv2
import numpy as np


def transform(value) -> np.ndarray:
    t = np.asarray(value, dtype=float)
    if t.shape != (4, 4) or not np.isfinite(t).all():
        raise ValueError("expected finite 4x4 transform")
    r = t[:3, :3]
    if not (
        np.allclose(t[3], [0, 0, 0, 1], atol=1e-9)
        and np.allclose(r.T @ r, np.eye(3), atol=1e-6)
        and abs(np.linalg.det(r) - 1) < 1e-6
    ):
        raise ValueError("invalid rigid rotation")
    return t


def inverse(value) -> np.ndarray:
    t = transform(value)
    out = np.eye(4)
    out[:3, :3] = t[:3, :3].T
    out[:3, 3] = -out[:3, :3] @ t[:3, 3]
    return out


def rigid(rotation_vector, translation) -> np.ndarray:
    t = np.eye(4)
    t[:3, :3] = cv2.Rodrigues(np.asarray(rotation_vector, dtype=float))[0]
    t[:3, 3] = translation
    return transform(t)


def mean_transform(values) -> np.ndarray:
    u, _, vt = np.linalg.svd(sum(t[:3, :3] for t in values))
    t = np.eye(4)
    t[:3, :3] = u @ np.diag([1, 1, np.linalg.det(u @ vt)]) @ vt
    t[:3, 3] = np.mean([v[:3, 3] for v in values], axis=0)
    return transform(t)


def errors(expected, actual) -> dict:
    delta = inverse(expected) @ transform(actual)
    return {
        "translation_m": float(np.linalg.norm(delta[:3, 3])),
        "rotation_rad": float(np.linalg.norm(cv2.Rodrigues(delta[:3, :3])[0])),
    }


def observable(poses) -> dict:
    if len(poses) < 6:
        raise ValueError("at least six independent poses required")
    relative = [inverse(poses[0]) @ t for t in poses[1:]]
    s = np.linalg.svd(
        [cv2.Rodrigues(t[:3, :3])[0].ravel() for t in relative], compute_uv=False
    )
    b = np.linalg.svd(
        np.vstack([t[:3, :3] - np.eye(3) for t in relative]), compute_uv=False
    )
    if (
        s[0] < 0.2
        or s[1] < 0.03
        or s[1] / s[0] < 0.02
        or b[-1] / max(b[0], 1e-12) < 0.02
    ):
        raise ValueError(
            "poses lack independent rotation axes; teach more varied tilts"
        )
    return {
        "rotation_singular_values": s.tolist(),
        "translation_singular_values": b.tolist(),
    }


def handeye(mode: str, robot, board) -> tuple[np.ndarray, np.ndarray, dict]:
    if mode not in {"eye_in_hand", "eye_to_hand"} or len(robot) != len(board):
        raise ValueError("invalid hand-eye inputs")
    a = [transform(t) if mode == "eye_in_hand" else inverse(t) for t in robot]
    z = [transform(t) for t in board]
    quality = observable(a)
    r, p = cv2.calibrateHandEye(
        [t[:3, :3] for t in a],
        [t[:3, 3] for t in a],
        [t[:3, :3] for t in z],
        [t[:3, 3] for t in z],
        method=cv2.CALIB_HAND_EYE_PARK,
    )
    x = np.eye(4)
    x[:3, :3], x[:3, 3] = r, p.ravel()
    transform(x)
    y = mean_transform([ai @ x @ zi for ai, zi in zip(a, z)])
    return x, y, quality


class Kinematics:
    """Six revolute joints from base_link to link6; raw joint mapping is explicit."""

    def __init__(self, xml: str, signs=None, offsets=None):
        self.xml = xml
        self.signs = np.asarray(signs if signs is not None else [1] * 6, dtype=float)
        self.offsets = np.asarray(
            offsets if offsets is not None else [0] * 6, dtype=float
        )
        if (
            self.signs.shape != (6,)
            or self.offsets.shape != (6,)
            or not np.isin(self.signs, [-1, 1]).all()
            or not np.isfinite(self.offsets).all()
        ):
            raise ValueError("invalid six-joint sign/offset mapping")
        root = ET.fromstring(xml)
        by_child = {j.find("child").get("link"): j for j in root.findall("joint")}
        chain, child = [], "link6"
        while child != "base_link":
            if child not in by_child or len(chain) > 16:
                raise ValueError("URDF does not contain base_link to link6 chain")
            joint = by_child[child]
            chain.append(joint)
            child = joint.find("parent").get("link")
        self.joints = list(reversed(chain))
        if len(self.joints) != 6 or any(
            j.get("type") != "revolute" for j in self.joints
        ):
            raise ValueError("expected six revolute X5 joints")
        self.names = [j.get("name") for j in self.joints]
        self.origins, self.axes, self.lower, self.upper = [], [], [], []
        for joint in self.joints:
            origin = joint.find("origin")
            xyz = np.fromstring(origin.get("xyz", "0 0 0"), sep=" ")
            roll, pitch, yaw = np.fromstring(origin.get("rpy", "0 0 0"), sep=" ")
            t = (
                rigid([0, 0, yaw], xyz)
                @ rigid([0, pitch, 0], [0, 0, 0])
                @ rigid([roll, 0, 0], [0, 0, 0])
            )
            axis = np.fromstring(joint.find("axis").get("xyz"), sep=" ")
            if (
                len(axis) != 3
                or not np.isfinite(axis).all()
                or np.linalg.norm(axis) < 1e-9
            ):
                raise ValueError("invalid URDF axis")
            self.origins.append(t)
            self.axes.append(axis / np.linalg.norm(axis))
            limit = joint.find("limit")
            self.lower.append(float(limit.get("lower")))
            self.upper.append(float(limit.get("upper")))
        if not np.isfinite([self.lower, self.upper]).all() or np.any(
            np.array(self.lower) >= self.upper
        ):
            raise ValueError("invalid URDF limits")

    @classmethod
    def load(cls, path: Path, mapping=None):
        return cls(path.read_text(), **(mapping or {}))

    def validate(self, raw) -> np.ndarray:
        if np.asarray(raw).dtype.kind not in "fiu" or any(
            isinstance(v, (bool, np.bool_)) for v in np.asarray(raw, dtype=object).flat
        ):
            raise ValueError("joint angles must be numbers, not strings or booleans")
        q = np.asarray(raw, dtype=float)
        if q.shape != (6,) or not np.isfinite(q).all():
            raise ValueError("six finite actual joint angles required")
        q = q * self.signs + self.offsets
        if np.any(q < np.array(self.lower) - 1e-6) or np.any(
            q > np.array(self.upper) + 1e-6
        ):
            raise ValueError("joint position exceeds mapped URDF limits")
        return q

    def fk(self, raw) -> np.ndarray:
        t = np.eye(4)
        for origin, axis, angle in zip(self.origins, self.axes, self.validate(raw)):
            t = t @ origin @ rigid(axis * angle, [0, 0, 0])
        return transform(t)

    def payload(self):
        return {
            "urdf_xml": self.xml,
            "signs": self.signs.tolist(),
            "offsets": self.offsets.tolist(),
            "joint_names": self.names,
            "end_frame": "link6",
            "base_frame": "base_link",
        }

    @classmethod
    def from_payload(cls, value):
        result = cls(value["urdf_xml"], value["signs"], value["offsets"])
        if value != result.payload():
            raise ValueError("kinematics payload does not match its URDF/mapping")
        return result
