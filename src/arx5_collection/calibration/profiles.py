"""Explicit native capture profiles; persisted routes never change resolution silently."""

DEFAULT_PROFILE = {"width": 1280, "height": 720, "fps": 30, "format": "rgb8"}
LEGACY_PROFILE = {"width": 848, "height": 480, "fps": 30, "format": "rgb8"}


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != set(DEFAULT_PROFILE):
        raise ValueError("invalid calibration camera profile")
    if any(type(profile[k]) is not int for k in ("width", "height", "fps")):
        raise ValueError("camera dimensions and FPS must be integers")
    if profile not in (DEFAULT_PROFILE, LEGACY_PROFILE):
        raise ValueError(
            "calibration requires native RGB8 1280x720@30 or legacy 848x480@30"
        )
    return dict(profile)
