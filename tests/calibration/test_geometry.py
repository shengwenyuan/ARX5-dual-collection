import numpy as np
import pytest

from arx5_collection.calibration.geometry import (
    handeye,
    inverse,
    rigid,
    observable,
    Kinematics,
)
from arx5_collection.calibration.solve import world_transforms, camera_world


def test_both_handeye_directions_recover_planted_transforms():
    rng = np.random.default_rng(39)
    a = [rigid(rng.normal(0, 0.5, 3), rng.normal(0, 0.2, 3)) for _ in range(25)]
    x = rigid([0.3, -0.2, 0.1], [0.2, -0.5, 0.8])
    y = rigid([-0.2, 0.1, 0.4], [0.1, 0.2, 0.3])
    for mode in ("eye_in_hand", "eye_to_hand"):
        z = [
            inverse(x) @ (inverse(ai) if mode == "eye_in_hand" else ai) @ y for ai in a
        ]
        actual_x, actual_y, quality = handeye(mode, a, z)
        np.testing.assert_allclose(actual_x, x, atol=1e-9)
        np.testing.assert_allclose(actual_y, y, atol=1e-9)
        assert quality["rotation_singular_values"][1] > 0


def test_single_axis_is_not_observable():
    with pytest.raises(ValueError, match="rotation axes"):
        observable([rigid([0, 0, i * 0.05], [i * 0.03, 0, 0]) for i in range(20)])


def test_fk_is_independent_of_meshes_and_respects_signs_and_limits(kin):
    home = [0, 0.948, 0.858, -0.573, 0, 0]
    assert np.isclose(np.linalg.det(kin.fk(home)[:3, :3]), 1)
    changed = Kinematics(kin.xml, [-1, 1, 1, 1, 1, 1], [0] * 6)
    np.testing.assert_allclose(changed.fk([0.1, *home[1:]]), kin.fk([-0.1, *home[1:]]))
    with pytest.raises(ValueError, match="limits"):
        kin.fk([0, -10, 0, 0, 0, 0])


def test_overview_is_world_and_wrists_follow_actual_joints(kin):
    xl = rigid([0.1, 0.2, 0.3], [0.2, 0.4, 0.6])
    xr = rigid([-0.2, 0.3, -0.1], [-0.5, 0.4, 0.6])
    wl = rigid([0.4, 0.1, 0.2], [0.03, 0, 0.02])
    wr = rigid([0.2, 0.1, 0.4], [0, 0.03, 0.02])
    solutions = {
        "overview-left": {"X": xl},
        "overview-right": {"X": xr},
        "left-wrist": {"X": wl},
        "right-wrist": {"X": wr},
    }
    result = {"transforms": world_transforms(solutions)}
    np.testing.assert_allclose(
        result["transforms"]["T_overview_left_base"], inverse(xl)
    )
    np.testing.assert_allclose(
        result["transforms"]["T_left_base_right_base"], xl @ inverse(xr)
    )
    q = [0, 0.948, 0.858, -0.573, 0, 0]
    np.testing.assert_allclose(camera_world(result, "overview", q, kin), np.eye(4))
    np.testing.assert_allclose(
        camera_world(result, "left", q, kin), inverse(xl) @ kin.fk(q) @ wl
    )
    np.testing.assert_allclose(
        camera_world(result, "right", q, kin), inverse(xr) @ kin.fk(q) @ wr
    )
    q[0] = 0.1
    assert not np.allclose(
        camera_world(result, "left", q, kin),
        camera_world(result, "left", [0, *q[1:]], kin),
    )
