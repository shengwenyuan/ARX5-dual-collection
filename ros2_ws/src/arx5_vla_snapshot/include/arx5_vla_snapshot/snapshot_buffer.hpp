#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "arx5_collection_interfaces/msg/arm_state.hpp"
#include "sensor_msgs/msg/image.hpp"

namespace arx5_vla_snapshot
{

enum class FailureCode
{
  kNone,
  kBuffersNotReady,
  kCameraSpanExceeded,
  kSnapshotStale,
  kLeftArmStale,
  kRightArmStale,
};

struct SnapshotConstraints
{
  std::int64_t max_camera_span_ns{40'000'000};
  std::int64_t max_arm_age_ns{2'000'000};
  std::int64_t max_snapshot_age_ns{100'000'000};
};

struct SelectionFailure
{
  FailureCode code{FailureCode::kNone};
  std::int64_t observed_ns{-1};
  std::int64_t limit_ns{-1};
  std::string detail;
};

struct VlaSnapshot
{
  sensor_msgs::msg::Image::ConstSharedPtr camera_left;
  sensor_msgs::msg::Image::ConstSharedPtr camera_overview;
  sensor_msgs::msg::Image::ConstSharedPtr camera_right;
  arx5_collection_interfaces::msg::ArmState::ConstSharedPtr left_arm;
  arx5_collection_interfaces::msg::ArmState::ConstSharedPtr right_arm;
  std::int64_t cutoff_ns;
};

struct SelectionResult
{
  std::optional<VlaSnapshot> snapshot;
  SelectionFailure failure;
};

class SnapshotBuffer
{
public:
  explicit SnapshotBuffer(
    std::size_t camera_history_size = 4,
    std::size_t arm_history_size = 128);

  void add_camera_left(sensor_msgs::msg::Image::ConstSharedPtr message);
  void add_camera_overview(sensor_msgs::msg::Image::ConstSharedPtr message);
  void add_camera_right(sensor_msgs::msg::Image::ConstSharedPtr message);
  void add_left_arm(
    arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message);
  void add_right_arm(
    arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message);

  SelectionResult select(
    std::int64_t now_ns,
    const SnapshotConstraints & constraints) const;

private:
  template<typename MessageT>
  static void push(
    std::deque<std::shared_ptr<const MessageT>> & target,
    std::shared_ptr<const MessageT> message,
    std::size_t limit);

  std::size_t camera_history_size_;
  std::size_t arm_history_size_;
  mutable std::mutex mutex_;
  std::deque<sensor_msgs::msg::Image::ConstSharedPtr> camera_left_;
  std::deque<sensor_msgs::msg::Image::ConstSharedPtr> camera_overview_;
  std::deque<sensor_msgs::msg::Image::ConstSharedPtr> camera_right_;
  std::deque<arx5_collection_interfaces::msg::ArmState::ConstSharedPtr> left_arm_;
  std::deque<arx5_collection_interfaces::msg::ArmState::ConstSharedPtr> right_arm_;
};

std::int64_t stamp_ns(const builtin_interfaces::msg::Time & stamp);
std::string failure_code_name(FailureCode code);

}  // namespace arx5_vla_snapshot
