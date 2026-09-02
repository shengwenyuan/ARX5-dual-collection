#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

#include "arx5_collection_interfaces/msg/arm_state.hpp"
#include "sensor_msgs/msg/image.hpp"

namespace arx5_d405_source_cpp
{

struct SnapshotDescriptor
{
  std::uint32_t slot;
  std::uint64_t generation;
};

class SnapshotSharedMemoryWriter
{
public:
  SnapshotSharedMemoryWriter(std::string path, std::uint32_t width, std::uint32_t height);
  SnapshotSharedMemoryWriter(const SnapshotSharedMemoryWriter &) = delete;
  SnapshotSharedMemoryWriter & operator=(const SnapshotSharedMemoryWriter &) = delete;
  ~SnapshotSharedMemoryWriter();

  SnapshotDescriptor commit(
    const sensor_msgs::msg::Image & camera_left,
    const sensor_msgs::msg::Image & camera_overview,
    const sensor_msgs::msg::Image & camera_right,
    const arx5_collection_interfaces::msg::ArmState & left_arm,
    const arx5_collection_interfaces::msg::ArmState & right_arm,
    std::int64_t cutoff_ns);

private:
  void copy_frame(const sensor_msgs::msg::Image & image, std::byte * destination) const;

  std::string path_;
  std::uint32_t width_;
  std::uint32_t height_;
  std::size_t frame_bytes_;
  std::size_t slot_stride_;
  std::size_t arena_bytes_;
  int file_descriptor_{-1};
  std::byte * arena_{nullptr};
  std::uint64_t sequence_{0};
};

}  // namespace arx5_d405_source_cpp
