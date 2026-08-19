#include "arx5_vla_snapshot/snapshot_buffer.hpp"

#include <algorithm>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace arx5_vla_snapshot
{
namespace
{

template<typename MessageT>
std::shared_ptr<const MessageT> nearest_not_after(
  const std::deque<std::shared_ptr<const MessageT>> & messages,
  std::int64_t anchor_ns,
  std::int64_t now_ns)
{
  std::shared_ptr<const MessageT> best;
  auto best_delta = std::numeric_limits<std::int64_t>::max();
  auto best_stamp = std::numeric_limits<std::int64_t>::min();
  for (const auto & message : messages) {
    const auto candidate_ns = stamp_ns(message->header.stamp);
    if (candidate_ns > now_ns) {
      continue;
    }
    const auto delta = std::llabs(candidate_ns - anchor_ns);
    if (delta < best_delta || (delta == best_delta && candidate_ns > best_stamp)) {
      best = message;
      best_delta = delta;
      best_stamp = candidate_ns;
    }
  }
  return best;
}

template<typename MessageT>
std::shared_ptr<const MessageT> latest_not_after(
  const std::deque<std::shared_ptr<const MessageT>> & messages,
  std::int64_t cutoff_ns)
{
  for (auto iterator = messages.rbegin(); iterator != messages.rend(); ++iterator) {
    if (stamp_ns((*iterator)->header.stamp) <= cutoff_ns) {
      return *iterator;
    }
  }
  return nullptr;
}

struct CameraGroup
{
  sensor_msgs::msg::Image::ConstSharedPtr left;
  sensor_msgs::msg::Image::ConstSharedPtr overview;
  sensor_msgs::msg::Image::ConstSharedPtr right;
  std::int64_t cutoff_ns;
  std::int64_t span_ns;
};

}  // namespace

std::int64_t stamp_ns(const builtin_interfaces::msg::Time & stamp)
{
  return static_cast<std::int64_t>(stamp.sec) * 1'000'000'000LL +
         static_cast<std::int64_t>(stamp.nanosec);
}

std::string failure_code_name(FailureCode code)
{
  switch (code) {
    case FailureCode::kNone:
      return "";
    case FailureCode::kBuffersNotReady:
      return "buffers_not_ready";
    case FailureCode::kCameraSpanExceeded:
      return "camera_span_exceeded";
    case FailureCode::kSnapshotStale:
      return "snapshot_stale";
    case FailureCode::kLeftArmStale:
      return "left_arm_stale";
    case FailureCode::kRightArmStale:
      return "right_arm_stale";
  }
  throw std::logic_error("unknown snapshot failure code");
}

SnapshotBuffer::SnapshotBuffer(
  std::size_t camera_history_size,
  std::size_t arm_history_size)
: camera_history_size_(camera_history_size),
  arm_history_size_(arm_history_size)
{
  if (camera_history_size_ < 2 || arm_history_size_ == 0) {
    throw std::invalid_argument("snapshot history sizes are invalid");
  }
}

template<typename MessageT>
void SnapshotBuffer::push(
  std::deque<std::shared_ptr<const MessageT>> & target,
  std::shared_ptr<const MessageT> message,
  std::size_t limit)
{
  if (!message) {
    throw std::invalid_argument("snapshot message must not be null");
  }
  target.push_back(std::move(message));
  while (target.size() > limit) {
    target.pop_front();
  }
}

void SnapshotBuffer::add_camera_left(sensor_msgs::msg::Image::ConstSharedPtr message)
{
  std::lock_guard<std::mutex> lock(mutex_);
  push(camera_left_, std::move(message), camera_history_size_);
}

void SnapshotBuffer::add_camera_overview(sensor_msgs::msg::Image::ConstSharedPtr message)
{
  std::lock_guard<std::mutex> lock(mutex_);
  push(camera_overview_, std::move(message), camera_history_size_);
}

void SnapshotBuffer::add_camera_right(sensor_msgs::msg::Image::ConstSharedPtr message)
{
  std::lock_guard<std::mutex> lock(mutex_);
  push(camera_right_, std::move(message), camera_history_size_);
}

void SnapshotBuffer::add_left_arm(
  arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message)
{
  std::lock_guard<std::mutex> lock(mutex_);
  push(left_arm_, std::move(message), arm_history_size_);
}

void SnapshotBuffer::add_right_arm(
  arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message)
{
  std::lock_guard<std::mutex> lock(mutex_);
  push(right_arm_, std::move(message), arm_history_size_);
}

SelectionResult SnapshotBuffer::select(
  std::int64_t now_ns,
  const SnapshotConstraints & constraints) const
{
  if (constraints.max_camera_span_ns <= 0 || constraints.max_arm_age_ns <= 0 ||
    constraints.max_snapshot_age_ns <= 0)
  {
    throw std::invalid_argument("snapshot timing limits must be positive");
  }

  std::lock_guard<std::mutex> lock(mutex_);
  std::string missing;
  const auto add_missing = [&missing](const std::string & name) {
      if (!missing.empty()) {
        missing += ',';
      }
      missing += name;
    };
  if (camera_left_.empty()) {add_missing("camera_left");}
  if (camera_overview_.empty()) {add_missing("camera_overview");}
  if (camera_right_.empty()) {add_missing("camera_right");}
  if (left_arm_.empty()) {add_missing("left_arm");}
  if (right_arm_.empty()) {add_missing("right_arm");}
  if (!missing.empty()) {
    return {std::nullopt, {FailureCode::kBuffersNotReady, -1, -1, "missing=" + missing}};
  }

  std::vector<CameraGroup> fresh_groups;
  auto newest_age_ns = std::numeric_limits<std::int64_t>::min();
  for (auto iterator = camera_overview_.rbegin(); iterator != camera_overview_.rend(); ++iterator) {
    const auto & overview = *iterator;
    const auto overview_ns = stamp_ns(overview->header.stamp);
    const auto left = nearest_not_after(camera_left_, overview_ns, now_ns);
    const auto right = nearest_not_after(camera_right_, overview_ns, now_ns);
    if (!left || !right) {
      continue;
    }
    const auto left_ns = stamp_ns(left->header.stamp);
    const auto right_ns = stamp_ns(right->header.stamp);
    const auto cutoff_ns = std::max({left_ns, overview_ns, right_ns});
    const auto oldest_ns = std::min({left_ns, overview_ns, right_ns});
    const auto age_ns = now_ns - cutoff_ns;
    if (newest_age_ns == std::numeric_limits<std::int64_t>::min()) {
      newest_age_ns = age_ns;
    }
    if (age_ns < 0 || age_ns > constraints.max_snapshot_age_ns) {
      continue;
    }
    fresh_groups.push_back({left, overview, right, cutoff_ns, cutoff_ns - oldest_ns});
  }
  if (fresh_groups.empty()) {
    return {
      std::nullopt,
      {FailureCode::kSnapshotStale, newest_age_ns,
        constraints.max_snapshot_age_ns, ""}};
  }

  std::vector<CameraGroup> aligned_groups;
  auto minimum_span_ns = std::numeric_limits<std::int64_t>::max();
  for (const auto & group : fresh_groups) {
    minimum_span_ns = std::min(minimum_span_ns, group.span_ns);
    if (group.span_ns <= constraints.max_camera_span_ns) {
      aligned_groups.push_back(group);
    }
  }
  if (aligned_groups.empty()) {
    return {
      std::nullopt,
      {FailureCode::kCameraSpanExceeded, minimum_span_ns,
        constraints.max_camera_span_ns, ""}};
  }

  SelectionFailure arm_failure;
  for (const auto & group : aligned_groups) {
    const auto left_arm = latest_not_after(left_arm_, group.cutoff_ns);
    if (!left_arm) {
      if (arm_failure.code == FailureCode::kNone) {
        arm_failure = {
          FailureCode::kLeftArmStale, -1, constraints.max_arm_age_ns,
          "no state at or before camera cutoff"};
      }
      continue;
    }
    const auto left_age_ns = group.cutoff_ns - stamp_ns(left_arm->header.stamp);
    if (left_age_ns > constraints.max_arm_age_ns) {
      if (arm_failure.code == FailureCode::kNone) {
        arm_failure = {
          FailureCode::kLeftArmStale, left_age_ns, constraints.max_arm_age_ns, ""};
      }
      continue;
    }
    const auto right_arm = latest_not_after(right_arm_, group.cutoff_ns);
    if (!right_arm) {
      if (arm_failure.code == FailureCode::kNone) {
        arm_failure = {
          FailureCode::kRightArmStale, -1, constraints.max_arm_age_ns,
          "no state at or before camera cutoff"};
      }
      continue;
    }
    const auto right_age_ns = group.cutoff_ns - stamp_ns(right_arm->header.stamp);
    if (right_age_ns > constraints.max_arm_age_ns) {
      if (arm_failure.code == FailureCode::kNone) {
        arm_failure = {
          FailureCode::kRightArmStale, right_age_ns, constraints.max_arm_age_ns, ""};
      }
      continue;
    }
    return {
      VlaSnapshot{
        group.left, group.overview, group.right, left_arm, right_arm,
        group.cutoff_ns},
      {}};
  }
  return {std::nullopt, arm_failure};
}

}  // namespace arx5_vla_snapshot
