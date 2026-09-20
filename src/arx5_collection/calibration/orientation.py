"""Resolve chessboard symmetry using robot rotations, without workstation pixel priors.

A common board-frame rotation changes only the board origin, not the hand-eye X.
Training orientations are selected from pairwise rotation-angle invariants. Held-out
orientations are then assigned against training only; they never update the fit.
"""

import cv2
import numpy as np

from .geometry import inverse


def corner_orders(corners, board):
    corners = np.asarray(corners, np.float32)
    orders = [corners, corners[::-1].copy()]
    if board.rows == board.columns:
        grid = corners.reshape(board.rows, board.columns, 2)
        orders += [np.rot90(grid, k).reshape(-1, 2).copy() for k in (1, 3)]
    return orders


def angle(rotation):
    return float(np.linalg.norm(cv2.Rodrigues(rotation)[0]))


def align_orientations(route, observations, board, intrinsic):
    from .solve import board_pose

    candidates = []
    for observation in observations:
        options = []
        for corners in corner_orders(observation["detection"]["corners"], board):
            try:
                pose, px = board_pose(board, corners, intrinsic)
                options.append((pose, px, corners))
            except ValueError:
                continue
        if not options:
            raise ValueError(f"no unambiguous PnP for pose {observation['pose_id']}")
        candidates.append(options)
    operators = [
        np.asarray(o["T_base_link6"])
        if route["mode"] == "eye_in_hand"
        else inverse(o["T_base_link6"])
        for o in observations
    ]
    training = [i for i, o in enumerate(observations) if o["split"] == "training"]
    costs = {}
    for i in range(len(observations)):
        for j in range(i):
            expected = angle(operators[j][:3, :3].T @ operators[i][:3, :3])
            cost = np.array(
                [
                    [
                        (angle(zj[0][:3, :3] @ zi[0][:3, :3].T) - expected) ** 2
                        for zj in candidates[j]
                    ]
                    for zi in candidates[i]
                ]
            )
            costs[i, j], costs[j, i] = cost, cost.T
    # Each anchor fixes only the global board-frame symmetry. Multiple deterministic
    # seeds avoid relying on one nearly frontal/ambiguous pair.
    solutions = []
    for anchor in training:
        labels = {anchor: 0}
        for i in training:
            if i != anchor:
                labels[i] = int(np.argmin(costs[i, anchor][:, 0]))
        for _ in range(20):
            changed = False
            for i in training:
                if i == anchor:
                    continue
                scores = sum(costs[i, j][:, labels[j]] for j in training if j != i)
                choice = int(np.argmin(scores))
                changed |= choice != labels[i]
                labels[i] = choice
            if not changed:
                break
        energy = sum(
            costs[i, j][labels[i], labels[j]]
            for i in training
            for j in training
            if i > j
        )
        solutions.append((energy, labels))
    _, labels = min(solutions, key=lambda v: v[0])
    for i in range(len(observations)):
        references = [j for j in training if j != i]
        scores = np.array(
            [
                np.mean([costs[i, j][k, labels[j]] for j in references])
                for k in range(len(candidates[i]))
            ]
        )
        order = np.argsort(scores)
        if i not in labels:
            labels[i] = int(order[0])
        if len(order) > 1 and np.sqrt(scores[order[1]]) - np.sqrt(
            scores[order[0]]
        ) < np.deg2rad(5):
            raise ValueError(
                f"ambiguous chessboard orientation at {observations[i]['pose_id']}; need more rotational diversity"
            )
    return [candidates[i][labels[i]] for i in range(len(observations))], {
        "method": "robot pairwise rotation invariants; training-only orientation fit",
        "selected_symmetry": {
            o["pose_id"]: labels[i] for i, o in enumerate(observations)
        },
    }
