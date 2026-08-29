#include "arx5_d405_source_cpp/snapshot_socket_server.hpp"

#include <cerrno>
#include <cstddef>
#include <cstring>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace arx5_d405_source_cpp
{
namespace
{

std::runtime_error system_error(const std::string & action)
{
  return std::runtime_error(action + ": " + std::strerror(errno));
}

void close_socket(std::atomic<int> & descriptor) noexcept
{
  const auto value = descriptor.exchange(-1);
  if (value >= 0) {
    ::close(value);
  }
}

}  // namespace

SnapshotSocketServer::SnapshotSocketServer(std::string path, Handler handler)
: path_(std::move(path)), handler_(std::move(handler))
{
  if (path_.empty() || !handler_) {
    throw std::invalid_argument("snapshot socket path and handler are required");
  }
  if (path_.size() >= sizeof(sockaddr_un{}.sun_path)) {
    throw std::invalid_argument("snapshot socket path is too long");
  }
}

SnapshotSocketServer::~SnapshotSocketServer()
{
  stop();
}

void SnapshotSocketServer::start()
{
  if (running_.exchange(true)) {
    throw std::logic_error("snapshot socket server is already running");
  }
  ::unlink(path_.c_str());
  const auto descriptor = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (descriptor < 0) {
    running_ = false;
    throw system_error("cannot create snapshot socket");
  }
  listener_ = descriptor;
  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  std::memcpy(address.sun_path, path_.c_str(), path_.size() + 1);
  if (::bind(descriptor, reinterpret_cast<sockaddr *>(&address), sizeof(address)) != 0 ||
    ::chmod(path_.c_str(), 0600) != 0 || ::listen(descriptor, 1) != 0)
  {
    const auto error = system_error("cannot bind snapshot socket");
    close_socket(listener_);
    ::unlink(path_.c_str());
    running_ = false;
    throw error;
  }
  thread_ = std::thread([this]() {run();});
}

void SnapshotSocketServer::stop() noexcept
{
  if (!running_.exchange(false)) {
    return;
  }
  const auto connection = connection_.load();
  if (connection >= 0) {
    ::shutdown(connection, SHUT_RDWR);
  }
  const auto listener = listener_.load();
  if (listener >= 0) {
    ::shutdown(listener, SHUT_RDWR);
  }
  if (thread_.joinable()) {
    thread_.join();
  }
  close_socket(connection_);
  close_socket(listener_);
  ::unlink(path_.c_str());
}

void SnapshotSocketServer::run() noexcept
{
  while (running_) {
    const auto connection = ::accept4(listener_.load(), nullptr, nullptr, SOCK_CLOEXEC);
    if (connection < 0) {
      if (errno == EINTR) {
        continue;
      }
      break;
    }
    connection_ = connection;
    serve(connection);
    close_socket(connection_);
  }
}

void SnapshotSocketServer::serve(int connection) noexcept
{
  while (running_) {
    std::uint8_t request{};
    const auto received = ::recv(connection, &request, sizeof(request), 0);
    if (received <= 0) {
      return;
    }
    SnapshotSocketReply reply{};
    try {
      reply = handler_();
    } catch (...) {
      return;
    }
    const auto * data = reinterpret_cast<const std::byte *>(&reply);
    std::size_t sent = 0;
    while (sent < sizeof(reply)) {
      const auto count = ::send(
        connection, data + sent, sizeof(reply) - sent, MSG_NOSIGNAL);
      if (count <= 0) {
        return;
      }
      sent += static_cast<std::size_t>(count);
    }
  }
}

}  // namespace arx5_d405_source_cpp
