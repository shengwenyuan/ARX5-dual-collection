#!/usr/bin/env bash
set -euo pipefail

ros_distro=${ROS_DISTRO:-jazzy}

set +u
source "/opt/ros/${ros_distro}/setup.bash"
source /opt/arx_ws/install/setup.bash
source /opt/collection_ws/install/setup.bash
set -u

exec ros2 launch arx5_arm_adapter dual_arms.launch.py
