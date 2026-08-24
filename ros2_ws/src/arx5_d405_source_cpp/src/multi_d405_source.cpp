#include <librealsense2/rs.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "arx5_collection_interfaces/msg/arm_state.hpp"
#include "arx5_collection_interfaces/msg/stream_status.hpp"
#include "arx5_collection_interfaces/srv/get_vla_snapshot.hpp"
#include "arx5_vla_snapshot/snapshot_buffer.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"

namespace arx5_d405_source_cpp
{
namespace
{

using namespace std::chrono_literals;
using Image = sensor_msgs::msg::Image;
using ArmState = arx5_collection_interfaces::msg::ArmState;
using StreamStatus = arx5_collection_interfaces::msg::StreamStatus;
using GetVlaSnapshot = arx5_collection_interfaces::srv::GetVlaSnapshot;

constexpr int kRequiredWidth = 848;
constexpr int kRequiredHeight = 480;
constexpr int kRequiredFps = 30;
constexpr std::size_t kRgbBytesPerPixel = 3;
constexpr std::size_t kDepthBytesPerPixel = 2;

std::int64_t timestamp_ns(double timestamp_ms)
{
  if (!std::isfinite(timestamp_ms) || timestamp_ms < 0.0) {
    throw std::runtime_error("RealSense returned an invalid Global Time timestamp");
  }
  return static_cast<std::int64_t>(timestamp_ms * 1'000'000.0);
}

std::string device_info(const rs2::device & device, rs2_camera_info key)
{
  return device.supports(key) ? device.get_info(key) : "unknown";
}

void require_d405_usb3_global_time(const rs2::device & device, const std::string & serial)
{
  const auto name = device_info(device, RS2_CAMERA_INFO_NAME);
  if (name.find("D405") == std::string::npos) {
    throw std::runtime_error("RealSense " + serial + " is not a D405: " + name);
  }
  const auto usb = device_info(device, RS2_CAMERA_INFO_USB_TYPE_DESCRIPTOR);
  if (usb.rfind("3.", 0) != 0) {
    throw std::runtime_error("RealSense " + serial + " requires USB3, detected " + usb);
  }

  std::size_t supported = 0;
  for (auto && sensor : device.query_sensors()) {
    if (!sensor.supports(RS2_OPTION_GLOBAL_TIME_ENABLED)) {
      continue;
    }
    if (sensor.get_option(RS2_OPTION_GLOBAL_TIME_ENABLED) != 1.0F) {
      sensor.set_option(RS2_OPTION_GLOBAL_TIME_ENABLED, 1.0F);
    }
    if (sensor.get_option(RS2_OPTION_GLOBAL_TIME_ENABLED) != 1.0F) {
      throw std::runtime_error("RealSense " + serial + " failed to enable Global Time");
    }
    ++supported;
  }
  if (supported == 0) {
    throw std::runtime_error("RealSense " + serial + " does not expose Global Time");
  }
}

Image::SharedPtr image_message(
  const rs2::video_frame & frame,
  const std::string & encoding,
  std::size_t bytes_per_pixel,
  std::int64_t stamp_ns,
  const std::string & frame_id)
{
  const auto width = static_cast<std::size_t>(frame.get_width());
  const auto height = static_cast<std::size_t>(frame.get_height());
  const auto step = static_cast<std::size_t>(frame.get_stride_in_bytes());
  if (width == 0 || height == 0 || step < width * bytes_per_pixel) {
    throw std::runtime_error("RealSense frame has an invalid image layout");
  }
  const auto payload_size = step * height;
  const auto * payload = static_cast<const std::uint8_t *>(frame.get_data());
  if (payload == nullptr) {
    throw std::runtime_error("RealSense frame payload is null");
  }

  auto message = std::make_shared<Image>();
  message->header.stamp.sec = static_cast<std::int32_t>(stamp_ns / 1'000'000'000LL);
  message->header.stamp.nanosec = static_cast<std::uint32_t>(stamp_ns % 1'000'000'000LL);
  message->header.frame_id = frame_id;
  message->height = static_cast<std::uint32_t>(height);
  message->width = static_cast<std::uint32_t>(width);
  message->encoding = encoding;
  message->is_bigendian = 0;
  message->step = static_cast<std::uint32_t>(step);
  message->data.resize(payload_size);
  std::memcpy(message->data.data(), payload, payload_size);
  return message;
}

struct CameraFrames
{
  std::string role;
  Image::SharedPtr color;
  Image::SharedPtr depth;
  std::int64_t stamp_ns;
};

class CameraWorker
{
public:
  using FrameSink = std::function<void(CameraFrames)>;
  using FailureSink = std::function<void(const std::string &)>;

  CameraWorker(
    std::string role,
    std::string serial,
    int width,
    int height,
    int fps,
    FrameSink frame_sink,
    FailureSink failure_sink)
  : role_(std::move(role)),
    serial_(std::move(serial)),
    width_(width),
    height_(height),
    fps_(fps),
    frame_sink_(std::move(frame_sink)),
    failure_sink_(std::move(failure_sink)),
    align_(RS2_STREAM_COLOR)
  {
  }

  CameraWorker(const CameraWorker &) = delete;
  CameraWorker & operator=(const CameraWorker &) = delete;

  ~CameraWorker()
  {
    stop();
  }

  void start()
  {
    rs2::config config;
    config.enable_device(serial_);
    config.enable_stream(
      RS2_STREAM_COLOR, width_, height_, RS2_FORMAT_RGB8, fps_);
    config.enable_stream(
      RS2_STREAM_DEPTH, width_, height_, RS2_FORMAT_Z16, fps_);

    try {
      const auto profile = pipeline_.start(
        config, [this](rs2::frame frame) {frame_queue_.enqueue(std::move(frame));});
      started_.store(true);
      require_d405_usb3_global_time(profile.get_device(), serial_);
      rs2::frame stale;
      while (frame_queue_.poll_for_frame(&stale)) {
      }
      running_.store(true);
      thread_ = std::thread([this]() {run();});
    } catch (...) {
      stop();
      throw;
    }
  }

  void stop() noexcept
  {
    running_.store(false);
    if (started_.exchange(false)) {
      try {
        pipeline_.stop();
      } catch (...) {
      }
    }
    if (thread_.joinable()) {
      thread_.join();
    }
  }

private:
  void run()
  {
    try {
      while (running_.load()) {
        auto frames = frame_queue_.wait_for_frame(5000).as<rs2::frameset>();
        if (!running_.load()) {
          break;
        }
        frames = align_.process(frames);
        const auto color = frames.get_color_frame();
        const auto depth = frames.get_depth_frame();
        if (!color || !depth) {
          throw std::runtime_error("incomplete aligned RGB-D frameset");
        }
        if (color.get_frame_timestamp_domain() != RS2_TIMESTAMP_DOMAIN_GLOBAL_TIME) {
          throw std::runtime_error("color frame is not in Global Time domain");
        }
        const auto stamp = timestamp_ns(color.get_timestamp());
        const auto frame_id = "camera_" + role_ + "_color_optical_frame";
        frame_sink_(CameraFrames{
          role_,
          image_message(color, "rgb8", kRgbBytesPerPixel, stamp, frame_id),
          image_message(depth, "16UC1", kDepthBytesPerPixel, stamp, frame_id),
          stamp});
      }
    } catch (const std::exception & error) {
      if (running_.exchange(false)) {
        failure_sink_("D405 " + role_ + " (" + serial_ + ") failed: " + error.what());
      }
    }
  }

  std::string role_;
  std::string serial_;
  int width_;
  int height_;
  int fps_;
  FrameSink frame_sink_;
  FailureSink failure_sink_;
  rs2::pipeline pipeline_;
  rs2::frame_queue frame_queue_{1};
  rs2::align align_;
  std::atomic<bool> started_{false};
  std::atomic<bool> running_{false};
  std::thread thread_;
};

class StreamTracker
{
public:
  StreamTracker(std::string stream_id, std::string topic)
  : stream_id_(std::move(stream_id)), topic_(std::move(topic))
  {
  }

  void observe(std::int64_t stamp_ns)
  {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto now = std::chrono::steady_clock::now();
    if (last_stamp_ns_ >= 0) {
      const auto gap = stamp_ns - last_stamp_ns_;
      if (gap <= 0) {
        ++non_monotonic_count_;
      } else {
        ++window_gap_count_;
        window_duration_ns_ += gap;
        window_max_gap_ns_ = std::max(window_max_gap_ns_, gap);
      }
    }
    ++total_count_;
    ++window_count_;
    last_stamp_ns_ = stamp_ns;
    last_arrival_ = now;
    has_arrival_ = true;
  }

  StreamStatus snapshot(const rclcpp::Time & now)
  {
    std::lock_guard<std::mutex> lock(mutex_);
    StreamStatus message;
    message.header.stamp = now;
    message.stream_id = stream_id_;
    message.topic = topic_;
    message.total_count = total_count_;
    message.window_count = window_count_;
    message.window_duration_s = static_cast<double>(window_duration_ns_) / 1e9;
    message.observed_hz = window_gap_count_ > 0 && window_duration_ns_ > 0 ?
      static_cast<double>(window_gap_count_) / message.window_duration_s : 0.0;
    message.max_gap_ms = static_cast<double>(window_max_gap_ns_) / 1e6;
    if (last_stamp_ns_ >= 0) {
      message.last_message_stamp.sec = static_cast<std::int32_t>(
        last_stamp_ns_ / 1'000'000'000LL);
      message.last_message_stamp.nanosec = static_cast<std::uint32_t>(
        last_stamp_ns_ % 1'000'000'000LL);
    }
    message.silence_s = has_arrival_ ?
      std::chrono::duration<double>(std::chrono::steady_clock::now() - last_arrival_).count() :
      0.0;
    message.non_monotonic_count = non_monotonic_count_;
    window_count_ = 0;
    window_gap_count_ = 0;
    window_duration_ns_ = 0;
    window_max_gap_ns_ = 0;
    return message;
  }

private:
  std::string stream_id_;
  std::string topic_;
  std::mutex mutex_;
  std::uint64_t total_count_{0};
  std::uint64_t window_count_{0};
  std::uint64_t window_gap_count_{0};
  std::uint64_t non_monotonic_count_{0};
  std::int64_t window_duration_ns_{0};
  std::int64_t window_max_gap_ns_{0};
  std::int64_t last_stamp_ns_{-1};
  bool has_arrival_{false};
  std::chrono::steady_clock::time_point last_arrival_;
};

struct CameraEndpoint
{
  explicit CameraEndpoint(rclcpp::Node & node, const std::string & role)
  : role(role),
    color_topic("/sensors/camera_" + role + "/color/image_raw"),
    depth_topic("/sensors/camera_" + role + "/aligned_depth/image_raw"),
    color_publisher(node.create_publisher<Image>(color_topic, rclcpp::QoS(2).reliable())),
    depth_publisher(node.create_publisher<Image>(depth_topic, rclcpp::QoS(2).reliable())),
    color_tracker("camera_" + role + "_color", color_topic),
    depth_tracker("camera_" + role + "_aligned_depth", depth_topic)
  {
  }

  std::string role;
  std::string color_topic;
  std::string depth_topic;
  rclcpp::Publisher<Image>::SharedPtr color_publisher;
  rclcpp::Publisher<Image>::SharedPtr depth_publisher;
  StreamTracker color_tracker;
  StreamTracker depth_tracker;
};

class MultiD405Source : public rclcpp::Node
{
public:
  MultiD405Source()
  : Node("multi_d405_source"),
    width_(declare_parameter<int>("width", kRequiredWidth)),
    height_(declare_parameter<int>("height", kRequiredHeight)),
    fps_(declare_parameter<int>("fps", kRequiredFps)),
    snapshot_enabled_(declare_parameter<bool>("enable_snapshot_service", false)),
    buffer_(positive_size_parameter("camera_history_size", 4),
      positive_size_parameter("arm_history_size", 128)),
    constraints_{
      positive_milliseconds_parameter("max_camera_span_ms", 40.0),
      positive_milliseconds_parameter("max_arm_age_ms", 2.0),
      positive_milliseconds_parameter("max_snapshot_age_ms", 100.0)},
    status_publisher_(create_publisher<StreamStatus>(
      "/monitoring/stream_status", rclcpp::QoS(32).reliable()))
  {
    if (width_ != kRequiredWidth || height_ != kRequiredHeight || fps_ != kRequiredFps) {
      throw std::invalid_argument("D405 stream is fixed at 848x480@30");
    }
    const auto serial_left = required_serial("serial_left");
    const auto serial_overview = required_serial("serial_overview");
    const auto serial_right = required_serial("serial_right");
    if (serial_left == serial_overview || serial_left == serial_right ||
      serial_overview == serial_right)
    {
      throw std::invalid_argument("D405 serial numbers must be unique");
    }

    endpoints_.push_back(std::make_unique<CameraEndpoint>(*this, "left"));
    endpoints_.push_back(std::make_unique<CameraEndpoint>(*this, "overview"));
    endpoints_.push_back(std::make_unique<CameraEndpoint>(*this, "right"));
    add_worker("left", serial_left);
    add_worker("overview", serial_overview);
    add_worker("right", serial_right);

    status_timer_ = create_wall_timer(1s, [this]() {publish_status();});
    if (snapshot_enabled_) {
      configure_snapshot_endpoint();
    }
  }

  ~MultiD405Source() override
  {
    stop();
  }

  void start()
  {
    try {
      for (auto & worker : workers_) {
        worker->start();
      }
    } catch (...) {
      stop();
      throw;
    }
    RCLCPP_INFO(
      get_logger(), "three D405 pipelines ready; snapshot_service=%s",
      snapshot_enabled_ ? "enabled" : "disabled");
  }

  void stop() noexcept
  {
    for (auto iterator = workers_.rbegin(); iterator != workers_.rend(); ++iterator) {
      (*iterator)->stop();
    }
  }

private:
  std::string required_serial(const std::string & name)
  {
    const auto value = declare_parameter<std::string>(name, "");
    if (value.empty()) {
      throw std::invalid_argument(name + " must not be empty");
    }
    return value;
  }

  std::size_t positive_size_parameter(const std::string & name, int default_value)
  {
    const auto value = declare_parameter<int>(name, default_value);
    if (value <= 0) {
      throw std::invalid_argument(name + " must be positive");
    }
    return static_cast<std::size_t>(value);
  }

  std::int64_t positive_milliseconds_parameter(
    const std::string & name, double default_value)
  {
    const auto value = declare_parameter<double>(name, default_value);
    if (value <= 0.0) {
      throw std::invalid_argument(name + " must be positive");
    }
    return static_cast<std::int64_t>(value * 1'000'000.0);
  }

  CameraEndpoint & endpoint(const std::string & role)
  {
    for (auto & candidate : endpoints_) {
      if (candidate->role == role) {
        return *candidate;
      }
    }
    throw std::logic_error("unknown camera role: " + role);
  }

  void add_worker(const std::string & role, const std::string & serial)
  {
    workers_.push_back(std::make_unique<CameraWorker>(
      role, serial, width_, height_, fps_,
      [this](CameraFrames frames) {on_frames(std::move(frames));},
      [this](const std::string & detail) {on_camera_failure(detail);}));
  }

  void on_frames(CameraFrames frames)
  {
    auto & target = endpoint(frames.role);
    if (snapshot_enabled_) {
      if (frames.role == "left") {
        buffer_.add_camera_left(frames.color);
      } else if (frames.role == "overview") {
        buffer_.add_camera_overview(frames.color);
      } else {
        buffer_.add_camera_right(frames.color);
      }
    }
    target.color_publisher->publish(*frames.color);
    target.depth_publisher->publish(*frames.depth);
    target.color_tracker.observe(frames.stamp_ns);
    target.depth_tracker.observe(frames.stamp_ns);
  }

  void on_camera_failure(const std::string & detail)
  {
    RCLCPP_FATAL(get_logger(), "%s", detail.c_str());
    rclcpp::shutdown();
  }

  void publish_status()
  {
    const auto now = get_clock()->now();
    for (auto & target : endpoints_) {
      status_publisher_->publish(target->color_tracker.snapshot(now));
      status_publisher_->publish(target->depth_tracker.snapshot(now));
    }
  }

  template<typename MessageT, typename CallbackT>
  typename rclcpp::Subscription<MessageT>::SharedPtr arm_subscription(
    const std::string & topic, CallbackT callback)
  {
    auto group = create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    callback_groups_.push_back(group);
    rclcpp::SubscriptionOptions options;
    options.callback_group = group;
    return create_subscription<MessageT>(
      topic, rclcpp::SensorDataQoS().keep_last(1), std::move(callback), options);
  }

  void configure_snapshot_endpoint()
  {
    left_arm_subscription_ = arm_subscription<ArmState>(
      "/embodiments/left_arm/state",
      [this](ArmState::ConstSharedPtr message) {buffer_.add_left_arm(std::move(message));});
    right_arm_subscription_ = arm_subscription<ArmState>(
      "/embodiments/right_arm/state",
      [this](ArmState::ConstSharedPtr message) {buffer_.add_right_arm(std::move(message));});
    service_group_ = create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    callback_groups_.push_back(service_group_);
    snapshot_service_ = create_service<GetVlaSnapshot>(
      "/dagger/get_snapshot",
      std::bind(
        &MultiD405Source::handle_snapshot, this,
        std::placeholders::_1, std::placeholders::_2),
      rclcpp::ServicesQoS(), service_group_);
  }

  void handle_snapshot(
    const std::shared_ptr<GetVlaSnapshot::Request>,
    std::shared_ptr<GetVlaSnapshot::Response> response)
  {
    const auto result = buffer_.select(get_clock()->now().nanoseconds(), constraints_);
    if (!result.snapshot) {
      response->ready = false;
      response->failure_code = arx5_vla_snapshot::failure_code_name(result.failure.code);
      response->observed_ns = result.failure.observed_ns;
      response->limit_ns = result.failure.limit_ns;
      response->detail = result.failure.detail;
      return;
    }
    const auto & snapshot = *result.snapshot;
    response->ready = true;
    response->observed_ns = -1;
    response->limit_ns = -1;
    response->observation_cutoff.sec = static_cast<std::int32_t>(
      snapshot.cutoff_ns / 1'000'000'000LL);
    response->observation_cutoff.nanosec = static_cast<std::uint32_t>(
      snapshot.cutoff_ns % 1'000'000'000LL);
    response->camera_left = *snapshot.camera_left;
    response->camera_overview = *snapshot.camera_overview;
    response->camera_right = *snapshot.camera_right;
    response->left_arm = *snapshot.left_arm;
    response->right_arm = *snapshot.right_arm;
  }

  int width_;
  int height_;
  int fps_;
  bool snapshot_enabled_;
  arx5_vla_snapshot::SnapshotBuffer buffer_;
  arx5_vla_snapshot::SnapshotConstraints constraints_;
  rclcpp::Publisher<StreamStatus>::SharedPtr status_publisher_;
  std::vector<std::unique_ptr<CameraEndpoint>> endpoints_;
  std::vector<std::unique_ptr<CameraWorker>> workers_;
  rclcpp::TimerBase::SharedPtr status_timer_;
  std::vector<rclcpp::CallbackGroup::SharedPtr> callback_groups_;
  rclcpp::CallbackGroup::SharedPtr service_group_;
  rclcpp::Subscription<ArmState>::SharedPtr left_arm_subscription_;
  rclcpp::Subscription<ArmState>::SharedPtr right_arm_subscription_;
  rclcpp::Service<GetVlaSnapshot>::SharedPtr snapshot_service_;
};

}  // namespace
}  // namespace arx5_d405_source_cpp

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  try {
    auto node = std::make_shared<arx5_d405_source_cpp::MultiD405Source>();
    node->start();
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 4);
    executor.add_node(node);
    executor.spin();
    node->stop();
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
    return 0;
  } catch (const std::exception & error) {
    std::fprintf(stderr, "multi D405 source failed: %s\n", error.what());
    if (rclcpp::ok()) {
      rclcpp::shutdown();
    }
    return 1;
  }
}
