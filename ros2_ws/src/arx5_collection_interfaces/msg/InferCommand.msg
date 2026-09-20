# Host ROS system time sampled just before the paired publish call.
std_msgs/Header header
string episode_id
uint64 step_index
uint64 monotonic_time_ns
# Final Vendor targets: left joints 1..6, left gripper, right joints 1..6, right gripper.
# Joints in radians; grippers in raw arx5-gripper-v1 actuator coordinates.
float64[14] action
