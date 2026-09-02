#include <cstdint>
#include <cstring>
#include <filesystem>
#include <string>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "arx5_d405_source_cpp/snapshot_socket_server.hpp"
#include "gtest/gtest.h"

TEST(SnapshotSocketServer, ServesFixedReply)
{
  const auto path = std::filesystem::temp_directory_path() /
    ("arx5-snapshot-socket-" + std::to_string(::getpid()));
  arx5_d405_source_cpp::SnapshotSocketServer server(
    path.string(), []() {
      return arx5_d405_source_cpp::SnapshotSocketReply{0, 1, 2, -1, -1};
    });
  server.start();

  const auto connection = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
  ASSERT_GE(connection, 0);
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  std::memcpy(address.sun_path, path.c_str(), path.string().size() + 1);
  ASSERT_EQ(
    ::connect(connection, reinterpret_cast<sockaddr *>(&address), sizeof(address)),
    0);
  const std::uint8_t request = 1;
  ASSERT_EQ(::send(connection, &request, sizeof(request), 0), 1);
  arx5_d405_source_cpp::SnapshotSocketReply reply{};
  ASSERT_EQ(::recv(connection, &reply, sizeof(reply), MSG_WAITALL), sizeof(reply));

  EXPECT_EQ(reply.status, 0U);
  EXPECT_EQ(reply.slot, 1U);
  EXPECT_EQ(reply.generation, 2U);
  ::close(connection);
  server.stop();
  EXPECT_FALSE(std::filesystem::exists(path));
}
