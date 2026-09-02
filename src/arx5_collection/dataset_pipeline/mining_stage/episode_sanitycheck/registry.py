from .arm_signal_check import run as arm_signal_check
from .alignment_report import run as alignment_report
from .frame_alignment import run as frame_alignment
from .mcap_check import run as mcap_check
from .metadata_check import run as metadata_check
from .timeline_check import run as timeline_check


UNIT_RUNNERS = {
    "metadata_check": metadata_check,
    "mcap_check": mcap_check,
    "timeline_check": timeline_check,
    "arm_signal_check": arm_signal_check,
    "frame_alignment": frame_alignment,
    "alignment_report": alignment_report,
}
