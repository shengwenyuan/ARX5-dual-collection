from .dagger_authority import run as dagger_authority
from .episode_filter import run as episode_filter
from .equal_eef_action_sampler import run as equal_eef_action_sampler
from .motion_segmenter import run as motion_segmenter
from .training_interval import run as training_interval
from .trajectory_labeler import run as trajectory_labeler


UNIT_RUNNERS = {
    "dagger_authority": dagger_authority,
    "episode_filter": episode_filter,
    "training_interval": training_interval,
    "equal_eef_action_sampler": equal_eef_action_sampler,
    "motion_segmenter": motion_segmenter,
    "trajectory_labeler": trajectory_labeler,
}
