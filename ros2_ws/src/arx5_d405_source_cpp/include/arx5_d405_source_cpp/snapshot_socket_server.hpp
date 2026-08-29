#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <thread>

namespace arx5_d405_source_cpp
{

struct SnapshotSocketReply
{
  std::uint32_t status;
  std::uint32_t slot;
  std::uint64_t generation;
  std::int64_t observed_ns;
  std::int64_t limit_ns;
};

static_assert(sizeof(SnapshotSocketReply) == 32);

class SnapshotSocketServer
{
public:
  using Handler = std::function<SnapshotSocketReply()>;

  SnapshotSocketServer(std::string path, Handler handler);
  SnapshotSocketServer(const SnapshotSocketServer &) = delete;
  SnapshotSocketServer & operator=(const SnapshotSocketServer &) = delete;
  ~SnapshotSocketServer();

  void start();
  void stop() noexcept;

private:
  void run() noexcept;
  void serve(int connection) noexcept;

  std::string path_;
  Handler handler_;
  std::atomic<bool> running_{false};
  std::atomic<int> listener_{-1};
  std::atomic<int> connection_{-1};
  std::thread thread_;
};

}  // namespace arx5_d405_source_cpp
