"""Explicit native capture profiles; persisted routes never change resolution silently."""

# Match collection's native D405 stream, not its optional resized policy snapshot.
DEFAULT_PROFILE = {"width": 848, "height": 480, "fps": 30, "format": "rgb8"}


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != set(DEFAULT_PROFILE):
        raise ValueError("invalid calibration camera profile")
    if any(type(profile[k]) is not int for k in ("width", "height", "fps")):
        raise ValueError("camera dimensions and FPS must be integers")
    if profile != DEFAULT_PROFILE:
        raise ValueError(
            "calibration requires collection-native RGB8 848x480@30; re-teach old 720p routes"
        )
    return dict(profile)
