#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <unistd.h>

#include "arx5_d405_source_cpp/snapshot_shared_memory_writer.hpp"
#include "gtest/gtest.h"

namespace
{

sensor_msgs::msg::Image image(std::uint8_t first)
{
  sensor_msgs::msg::Image result;
  result.width = 2;
  result.height = 1;
  result.encoding = "rgb8";
  result.step = 6;
  result.data = {first, 1, 2, 3, 4, 5};
  return result;
}

arx5_collection_interfaces::msg::ArmState arm(double first)
{
  arx5_collection_interfaces::msg::ArmState result;
  result.joint_positions = {first, 1, 2, 3, 4, 5};
  result.gripper_position = 6;
  return result;
}

TEST(SnapshotSharedMemoryWriter, CommitsOneAtomicTriplet)
{
  const auto path =
    std::filesystem::temp_directory_path() /
    ("arx5-snapshot-writer-" + std::to_string(::getpid()));
  arx5_d405_source_cpp::SnapshotSharedMemoryWriter writer(path.string(), 2, 1);

  const auto descriptor = writer.commit(
    image(10), image(20), image(30), arm(40), arm(50), 60);

  EXPECT_EQ(descriptor.slot, 1U);
  EXPECT_EQ(descriptor.generation, 2U);
  std::ifstream arena(path, std::ios::binary);
  const auto slot_offset = 64 + descriptor.slot * (192 + 18);
  arena.seekg(static_cast<std::streamoff>(slot_offset));
  std::uint64_t generation = 0;
  arena.read(reinterpret_cast<char *>(&generation), sizeof(generation));
  EXPECT_EQ(generation, descriptor.generation);
  arena.seekg(static_cast<std::streamoff>(slot_offset + 192));
  std::array<std::uint8_t, 18> payload{};
  arena.read(reinterpret_cast<char *>(payload.data()), payload.size());
  EXPECT_EQ(payload[0], 10);
  EXPECT_EQ(payload[6], 20);
  EXPECT_EQ(payload[12], 30);
}

}  // namespace
