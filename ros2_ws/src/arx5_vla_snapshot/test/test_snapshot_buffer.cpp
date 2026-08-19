#include <cstdint>
#include <memory>

#include "arx5_collection_interfaces/msg/arm_state.hpp"
#include "arx5_vla_snapshot/snapshot_buffer.hpp"
#include "gtest/gtest.h"
#include "sensor_msgs/msg/image.hpp"

namespace
{

void set_stamp(std_msgs::msg::Header & header, std::int64_t stamp_ns)
{
  header.stamp.sec = static_cast<std::int32_t>(stamp_ns / 1'000'000'000LL);
  header.stamp.nanosec = static_cast<std::uint32_t>(stamp_ns % 1'000'000'000LL);
}

sensor_msgs::msg::Image::SharedPtr image(std::int64_t stamp_ns)
{
  auto message = std::make_shared<sensor_msgs::msg::Image>();
  set_stamp(message->header, stamp_ns);
  message->width = 2;
  message->height = 2;
  message->encoding = "yuyv";
  message->step = 4;
  message->data.resize(8);
  return message;
}

arx5_collection_interfaces::msg::ArmState::SharedPtr arm(std::int64_t stamp_ns)
{
  auto message = std::make_shared<arx5_collection_interfaces::msg::ArmState>();
  set_stamp(message->header, stamp_ns);
  return message;
}

void add_complete_group(
  arx5_vla_snapshot::SnapshotBuffer & buffer,
  std::int64_t left_ns,
  std::int64_t overview_ns,
  std::int64_t right_ns,
  std::int64_t arm_ns)
{
  buffer.add_camera_left(image(left_ns));
  buffer.add_camera_overview(image(overview_ns));
  buffer.add_camera_right(image(right_ns));
  buffer.add_left_arm(arm(arm_ns));
  buffer.add_right_arm(arm(arm_ns));
}

}  // namespace

TEST(SnapshotBuffer, ReportsMissingBuffers)
{
  arx5_vla_snapshot::SnapshotBuffer buffer;
  const auto result = buffer.select(1'000, {});
  EXPECT_FALSE(result.snapshot.has_value());
  EXPECT_EQ(result.failure.code, arx5_vla_snapshot::FailureCode::kBuffersNotReady);
}

TEST(SnapshotBuffer, AcceptsWorstCaseIndependentThirtyHertzPhase)
{
  arx5_vla_snapshot::SnapshotBuffer buffer;
  add_complete_group(
    buffer, 984'000'000, 1'000'000'000, 1'016'000'000, 1'015'000'000);

  const auto result = buffer.select(1'020'000'000, {});

  ASSERT_TRUE(result.snapshot.has_value());
  EXPECT_EQ(result.snapshot->cutoff_ns, 1'016'000'000);
  EXPECT_EQ(
    arx5_vla_snapshot::stamp_ns(result.snapshot->left_arm->header.stamp),
    1'015'000'000);
}

TEST(SnapshotBuffer, ReportsCameraSpanWithObservedValue)
{
  arx5_vla_snapshot::SnapshotBuffer buffer;
  add_complete_group(buffer, 900, 1'000, 995, 995);
  const arx5_vla_snapshot::SnapshotConstraints constraints{20, 15, 100};

  const auto result = buffer.select(1'010, constraints);

  EXPECT_FALSE(result.snapshot.has_value());
  EXPECT_EQ(result.failure.code, arx5_vla_snapshot::FailureCode::kCameraSpanExceeded);
  EXPECT_EQ(result.failure.observed_ns, 100);
  EXPECT_EQ(result.failure.limit_ns, 20);
}

TEST(SnapshotBuffer, NeverUsesPostCutoffArmState)
{
  arx5_vla_snapshot::SnapshotBuffer buffer;
  buffer.add_camera_left(image(1'000));
  buffer.add_camera_overview(image(1'000));
  buffer.add_camera_right(image(1'000));
  buffer.add_left_arm(arm(999));
  buffer.add_left_arm(arm(1'001));
  buffer.add_right_arm(arm(998));
  buffer.add_right_arm(arm(1'002));

  const auto result = buffer.select(
    1'010, arx5_vla_snapshot::SnapshotConstraints{20, 15, 100});

  ASSERT_TRUE(result.snapshot.has_value());
  EXPECT_EQ(
    arx5_vla_snapshot::stamp_ns(result.snapshot->left_arm->header.stamp), 999);
  EXPECT_EQ(
    arx5_vla_snapshot::stamp_ns(result.snapshot->right_arm->header.stamp), 998);
}
