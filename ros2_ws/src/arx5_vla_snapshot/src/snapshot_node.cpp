#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "arx5_collection_interfaces/msg/arm_state.hpp"
#include "arx5_collection_interfaces/srv/get_vla_snapshot.hpp"
#include "arx5_vla_snapshot/snapshot_buffer.hpp"
#include "rclcpp/rclcpp.hpp"
#include "sensor_msgs/msg/image.hpp"

namespace arx5_vla_snapshot
{

class VlaSnapshotSource : public rclcpp::Node
{
public:
  VlaSnapshotSource()
  : Node("vla_snapshot_source"),
    buffer_(positive_size_parameter("camera_history_size", 4),
      positive_size_parameter("arm_history_size", 128)),
    constraints_{
      positive_milliseconds_parameter("max_camera_span_ms", 40.0),
      positive_milliseconds_parameter("max_arm_age_ms", 2.0),
      positive_milliseconds_parameter("max_snapshot_age_ms", 100.0)}
  {
    const auto image_qos = rclcpp::SensorDataQoS().keep_last(1);
    const auto arm_qos = rclcpp::SensorDataQoS().keep_last(1);

    camera_left_subscription_ = create_data_subscription<sensor_msgs::msg::Image>(
      "/sensors/camera_left/color/image_raw", image_qos,
      [this](sensor_msgs::msg::Image::ConstSharedPtr message) {
        buffer_.add_camera_left(std::move(message));
      });
    camera_overview_subscription_ = create_data_subscription<sensor_msgs::msg::Image>(
      "/sensors/camera_overview/color/image_raw", image_qos,
      [this](sensor_msgs::msg::Image::ConstSharedPtr message) {
        buffer_.add_camera_overview(std::move(message));
      });
    camera_right_subscription_ = create_data_subscription<sensor_msgs::msg::Image>(
      "/sensors/camera_right/color/image_raw", image_qos,
      [this](sensor_msgs::msg::Image::ConstSharedPtr message) {
        buffer_.add_camera_right(std::move(message));
      });
    left_arm_subscription_ =
      create_data_subscription<arx5_collection_interfaces::msg::ArmState>(
      "/embodiments/left_arm/state", arm_qos,
      [this](arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message) {
        buffer_.add_left_arm(std::move(message));
      });
    right_arm_subscription_ =
      create_data_subscription<arx5_collection_interfaces::msg::ArmState>(
      "/embodiments/right_arm/state", arm_qos,
      [this](arx5_collection_interfaces::msg::ArmState::ConstSharedPtr message) {
        buffer_.add_right_arm(std::move(message));
      });

    service_group_ = create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    callback_groups_.push_back(service_group_);
    service_ = create_service<arx5_collection_interfaces::srv::GetVlaSnapshot>(
      "/dagger/get_snapshot",
      std::bind(
        &VlaSnapshotSource::handle_request, this,
        std::placeholders::_1, std::placeholders::_2),
      rclcpp::ServicesQoS(), service_group_);
    RCLCPP_INFO(get_logger(), "VLA snapshot service ready");
  }

private:
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

  template<typename MessageT, typename CallbackT>
  typename rclcpp::Subscription<MessageT>::SharedPtr create_data_subscription(
    const std::string & topic,
    const rclcpp::QoS & qos,
    CallbackT callback)
  {
    auto group = create_callback_group(rclcpp::CallbackGroupType::MutuallyExclusive);
    callback_groups_.push_back(group);
    rclcpp::SubscriptionOptions options;
    options.callback_group = group;
    return create_subscription<MessageT>(topic, qos, std::move(callback), options);
  }

  void handle_request(
    const std::shared_ptr<arx5_collection_interfaces::srv::GetVlaSnapshot::Request>,
    std::shared_ptr<arx5_collection_interfaces::srv::GetVlaSnapshot::Response> response)
  {
    const auto result = buffer_.select(get_clock()->now().nanoseconds(), constraints_);
    if (!result.snapshot) {
      response->ready = false;
      response->failure_code = failure_code_name(result.failure.code);
      response->observed_ns = result.failure.observed_ns;
      response->limit_ns = result.failure.limit_ns;
      response->detail = result.failure.detail;
      return;
    }
    const auto & snapshot = *result.snapshot;
    response->ready = true;
    response->failure_code.clear();
    response->observed_ns = -1;
    response->limit_ns = -1;
    response->detail.clear();
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

  SnapshotBuffer buffer_;
  SnapshotConstraints constraints_;
  std::vector<rclcpp::CallbackGroup::SharedPtr> callback_groups_;
  rclcpp::CallbackGroup::SharedPtr service_group_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr camera_left_subscription_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr camera_overview_subscription_;
  rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr camera_right_subscription_;
  rclcpp::Subscription<arx5_collection_interfaces::msg::ArmState>::SharedPtr
    left_arm_subscription_;
  rclcpp::Subscription<arx5_collection_interfaces::msg::ArmState>::SharedPtr
    right_arm_subscription_;
  rclcpp::Service<arx5_collection_interfaces::srv::GetVlaSnapshot>::SharedPtr service_;
};

}  // namespace arx5_vla_snapshot

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<arx5_vla_snapshot::VlaSnapshotSource>();
  rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 6);
  executor.add_node(node);
  executor.spin();
  rclcpp::shutdown();
  return 0;
}
