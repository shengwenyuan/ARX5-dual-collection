#pragma once

#include <cstdint>

#include "sensor_msgs/msg/image.hpp"

namespace arx5_d405_source_cpp
{

class RgbImageResizer
{
public:
  RgbImageResizer(std::uint32_t width, std::uint32_t height);

  sensor_msgs::msg::Image resize(const sensor_msgs::msg::Image & source) const;

private:
  std::uint32_t width_;
  std::uint32_t height_;
};

}  // namespace arx5_d405_source_cpp
