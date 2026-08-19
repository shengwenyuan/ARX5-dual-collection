uint8 TAKEOVER_REQUESTED=1
uint8 HUMAN_ACTIVE=2
uint8 RESUME_REQUESTED=3
uint8 POLICY_ACTIVE=4
uint8 FAULT_HOLD=5

std_msgs/Header header
uint64 sequence
uint64 monotonic_time_ns
uint64 intervention_id
uint64 control_epoch
uint8 event_type
string reason
