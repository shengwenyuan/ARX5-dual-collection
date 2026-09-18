"""Chessboard detection, visible candidate feedback and explicit corner orientation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

import cv2
import numpy as np


@dataclass(frozen=True)
class Board:
    columns: int
    rows: int
    square_mm: float
    min_coverage: float = 0.015
    min_sharpness: float = 40.0

    def __post_init__(self):
        if (
            type(self.columns) is not int
            or type(self.rows) is not int
            or not (3 <= self.columns <= 30 and 3 <= self.rows <= 30)
        ):
            raise ValueError(
                "enter inner-corner columns/rows (3..30), not square counts"
            )
        if not math.isfinite(self.square_mm) or not 0 < self.square_mm <= 200:
            raise ValueError("square edge must be in mm (0..200)")
        if (
            not 0 < self.min_coverage < 0.5
            or not math.isfinite(self.min_sharpness)
            or self.min_sharpness <= 0
        ):
            raise ValueError("invalid board image thresholds")

    def points(self):
        points = np.zeros((self.columns * self.rows, 3), np.float32)
        points[:, :2] = (
            np.mgrid[: self.columns, : self.rows].T.reshape(-1, 2)
            * self.square_mm
            / 1000
        )
        return points

    def payload(self):
        return asdict(self)


@dataclass
class Detection:
    corners: np.ndarray | None
    coverage: float
    sharpness: float
    reason: str

    @property
    def valid(self):
        return self.corners is not None and not self.reason

    def payload(self):
        return {
            "corners": None
            if self.corners is None
            else self.corners.reshape(-1, 2).tolist(),
            "coverage": self.coverage,
            "sharpness": self.sharpness,
            "reason": self.reason,
        }


def detect(image, board: Board, reference=None, reverse=False) -> Detection:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCornersSB(
        gray, (board.columns, board.rows), flags=cv2.CALIB_CB_NORMALIZE_IMAGE
    )
    if not found:
        return Detection(None, 0.0, 0.0, "board not fully detected")
    corners = corners.reshape(-1, 2)
    if reference is not None:
        reference = np.asarray(reference, np.float32).reshape(-1, 2)
        if reference.shape != corners.shape:
            raise ValueError("corner reference shape mismatch")
        options = [corners, corners[::-1].copy()]
        if board.rows == board.columns:
            grid = corners.reshape(board.rows, board.columns, 2)
            options.extend(np.rot90(grid, n).reshape(-1, 2).copy() for n in (1, 3))
        corners = min(options, key=lambda value: np.linalg.norm(value - reference))
    if reverse:
        corners = corners[::-1].copy()
    hull = cv2.convexHull(corners)
    coverage = cv2.contourArea(hull) / gray.size
    mask = np.zeros(gray.shape, np.uint8)
    cv2.fillConvexPoly(mask, hull.astype(np.int32), 255)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F)[mask > 0].var())
    border = bool(
        (corners < 8).any()
        or (corners[:, 0] > gray.shape[1] - 8).any()
        or (corners[:, 1] > gray.shape[0] - 8).any()
    )
    reason = (
        "board too small"
        if coverage < board.min_coverage
        else "blurred board"
        if sharpness < board.min_sharpness
        else "board at image edge"
        if border
        else ""
    )
    return Detection(corners, float(coverage), sharpness, reason)


def overlay(image, board, detection, messages, ready):
    out = image.copy()
    if detection.corners is not None:
        cv2.drawChessboardCorners(
            out, (board.columns, board.rows), detection.corners.reshape(-1, 1, 2), True
        )
        for label, index in (
            ("A(0,0)", 0),
            ("+X", board.columns - 1),
            ("+Y", (board.rows - 1) * board.columns),
        ):
            xy = tuple(np.rint(detection.corners[index]).astype(int))
            cv2.putText(
                out, label, xy, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 0, 255), 2
            )
    color = (0, 220, 0) if ready else (0, 100, 255)
    for index, line in enumerate(messages):
        cv2.putText(
            out,
            line,
            (12, 24 + index * 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (0, 0, 0),
            4,
        )
        cv2.putText(
            out, line, (12, 24 + index * 23), cv2.FONT_HERSHEY_SIMPLEX, 0.52, color, 1
        )
    return out


def next_orientation(corners, board):
    if board.rows == board.columns:
        return (
            np.rot90(corners.reshape(board.rows, board.columns, 2))
            .reshape(-1, 2)
            .copy()
        )
    return corners[::-1].copy()
