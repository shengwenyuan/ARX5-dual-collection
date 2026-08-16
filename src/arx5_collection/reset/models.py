from enum import Enum


class ResetState(str, Enum):
    WAITING = "reset_waiting"
    RESETTING = "resetting"
    COMPLETE = "reset_complete"
