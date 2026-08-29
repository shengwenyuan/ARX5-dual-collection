#include "arx5_d405_source_cpp/snapshot_shared_memory_writer.hpp"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include "arx5_vla_snapshot/snapshot_buffer.hpp"

namespace arx5_d405_source_cpp
{
namespace
{

constexpr std::array<char, 8> kMagic{'A', 'R', 'X', '5', 'R', 'G', 'B', '1'};
constexpr std::uint32_t kChannels = 3;
constexpr std::uint32_t kSlotCount = 2;

struct ArenaHeader
{
  std::array<char, 8> magic;
  std::uint32_t width;
  std::uint32_t height;
  std::uint32_t channels;
  std::uint32_t slot_count;
  std::array<std::byte, 40> reserved{};
};

struct SlotHeader
{
  std::uint64_t generation;
  std::int64_t cutoff_ns;
  std::int64_t camera_left_ns;
  std::int64_t camera_overview_ns;
  std::int64_t camera_right_ns;
  std::int64_t left_arm_ns;
  std::int64_t right_arm_ns;
  std::array<double, 7> left_arm;
  std::array<double, 7> right_arm;
  std::array<std::byte, 24> reserved{};
};

static_assert(sizeof(ArenaHeader) == 64);
static_assert(sizeof(SlotHeader) == 192);

std::runtime_error system_error(const std::string & action)
{
  return std::runtime_error(action + ": " + std::strerror(errno));
}

}  // namespace

SnapshotSharedMemoryWriter::SnapshotSharedMemoryWriter(
  std::string path, std::uint32_t width, std::uint32_t height)
: path_(std::move(path)),
  width_(width),
  height_(height),
  frame_bytes_(static_cast<std::size_t>(width) * height * kChannels),
  slot_stride_(sizeof(SlotHeader) + frame_bytes_ * 3),
  arena_bytes_(sizeof(ArenaHeader) + kSlotCount * slot_stride_)
{
  if (path_.empty() || width_ == 0 || height_ == 0) {
    throw std::invalid_argument("snapshot arena path and dimensions are required");
  }
  file_descriptor_ = ::open(path_.c_str(), O_CREAT | O_TRUNC | O_RDWR | O_CLOEXEC, 0600);
  if (file_descriptor_ < 0) {
    throw system_error("cannot create snapshot arena");
  }
  if (::ftruncate(file_descriptor_, static_cast<off_t>(arena_bytes_)) != 0) {
    const auto error = system_error("cannot size snapshot arena");
    ::close(file_descriptor_);
    file_descriptor_ = -1;
    ::unlink(path_.c_str());
    throw error;
  }
  auto * mapping = ::mmap(
    nullptr, arena_bytes_, PROT_READ | PROT_WRITE, MAP_SHARED, file_descriptor_, 0);
  if (mapping == MAP_FAILED) {
    const auto error = system_error("cannot map snapshot arena");
    ::close(file_descriptor_);
    file_descriptor_ = -1;
    ::unlink(path_.c_str());
    throw error;
  }
  arena_ = static_cast<std::byte *>(mapping);
  std::memset(arena_, 0, arena_bytes_);
  auto * header = reinterpret_cast<ArenaHeader *>(arena_);
  header->magic = kMagic;
  header->width = width_;
  header->height = height_;
  header->channels = kChannels;
  header->slot_count = kSlotCount;
}

SnapshotSharedMemoryWriter::~SnapshotSharedMemoryWriter()
{
  if (arena_ != nullptr) {
    ::munmap(arena_, arena_bytes_);
  }
  if (file_descriptor_ >= 0) {
    ::close(file_descriptor_);
  }
  ::unlink(path_.c_str());
}

SnapshotDescriptor SnapshotSharedMemoryWriter::commit(
  const sensor_msgs::msg::Image & camera_left,
  const sensor_msgs::msg::Image & camera_overview,
  const sensor_msgs::msg::Image & camera_right,
  const arx5_collection_interfaces::msg::ArmState & left_arm,
  const arx5_collection_interfaces::msg::ArmState & right_arm,
  std::int64_t cutoff_ns)
{
  ++sequence_;
  const auto generation = sequence_ * 2;
  const auto slot = static_cast<std::uint32_t>(sequence_ % kSlotCount);
  auto * slot_base = arena_ + sizeof(ArenaHeader) + slot * slot_stride_;
  auto * slot_header = reinterpret_cast<SlotHeader *>(slot_base);
  __atomic_store_n(&slot_header->generation, generation | 1, __ATOMIC_SEQ_CST);
  slot_header->cutoff_ns = cutoff_ns;
  slot_header->camera_left_ns = arx5_vla_snapshot::stamp_ns(camera_left.header.stamp);
  slot_header->camera_overview_ns = arx5_vla_snapshot::stamp_ns(camera_overview.header.stamp);
  slot_header->camera_right_ns = arx5_vla_snapshot::stamp_ns(camera_right.header.stamp);
  slot_header->left_arm_ns = arx5_vla_snapshot::stamp_ns(left_arm.header.stamp);
  slot_header->right_arm_ns = arx5_vla_snapshot::stamp_ns(right_arm.header.stamp);
  std::copy(left_arm.joint_positions.begin(), left_arm.joint_positions.end(),
    slot_header->left_arm.begin());
  slot_header->left_arm.back() = left_arm.gripper_position;
  std::copy(right_arm.joint_positions.begin(), right_arm.joint_positions.end(),
    slot_header->right_arm.begin());
  slot_header->right_arm.back() = right_arm.gripper_position;
  auto * payload = slot_base + sizeof(SlotHeader);
  copy_frame(camera_left, payload);
  copy_frame(camera_overview, payload + frame_bytes_);
  copy_frame(camera_right, payload + frame_bytes_ * 2);
  __atomic_store_n(&slot_header->generation, generation, __ATOMIC_RELEASE);
  return SnapshotDescriptor{slot, generation};
}

void SnapshotSharedMemoryWriter::copy_frame(
  const sensor_msgs::msg::Image & image, std::byte * destination) const
{
  if (image.encoding != "rgb8" || image.width != width_ || image.height != height_ ||
    image.step != width_ * kChannels || image.data.size() != frame_bytes_)
  {
    throw std::invalid_argument("snapshot RGB frame does not match the arena layout");
  }
  std::memcpy(destination, image.data.data(), frame_bytes_);
}

}  // namespace arx5_d405_source_cpp
