#include "arx5_d405_source_cpp/rgb_image_resizer.hpp"

#include <cstring>
#include <limits>
#include <stdexcept>

#include "opencv2/core.hpp"
#include "opencv2/imgproc.hpp"

namespace arx5_d405_source_cpp
{
namespace
{

constexpr std::uint32_t kRgbChannels = 3;

std::size_t checked_payload_size(std::uint32_t width, std::uint32_t height)
{
  if (width > std::numeric_limits<std::uint32_t>::max() / kRgbChannels) {
    throw std::overflow_error("RGB image dimensions exceed addressable memory");
  }
  const auto row_size = static_cast<std::size_t>(width) * kRgbChannels;
  if (height > std::numeric_limits<std::size_t>::max() / row_size) {
    throw std::overflow_error("RGB image dimensions exceed addressable memory");
  }
  return row_size * height;
}

}  // namespace

RgbImageResizer::RgbImageResizer(std::uint32_t width, std::uint32_t height)
: width_(width), height_(height)
{
  if (width_ == 0 || height_ == 0) {
    throw std::invalid_argument("snapshot image dimensions must be positive");
  }
  checked_payload_size(width_, height_);
}

sensor_msgs::msg::Image RgbImageResizer::resize(
  const sensor_msgs::msg::Image & source) const
{
  if (source.encoding != "rgb8") {
    throw std::invalid_argument("snapshot image encoding must be rgb8");
  }
  if (source.width == 0 || source.height == 0) {
    throw std::invalid_argument("snapshot source dimensions must be positive");
  }
  const auto source_step = checked_payload_size(source.width, 1);
  const auto source_size = checked_payload_size(source.width, source.height);
  if (source.step != source_step || source.data.size() != source_size) {
    throw std::invalid_argument("snapshot source must be tightly packed RGB8");
  }

  // cv::Mat has no const-data view; cv::resize treats its InputArray as read-only.
  const cv::Mat source_view(
    static_cast<int>(source.height), static_cast<int>(source.width), CV_8UC3,
    const_cast<std::uint8_t *>(source.data.data()), source.step);
  cv::Mat resized;
  cv::resize(
    source_view, resized,
    cv::Size(static_cast<int>(width_), static_cast<int>(height_)),
    0.0, 0.0, cv::INTER_AREA);
  if (!resized.isContinuous()) {
    throw std::runtime_error("OpenCV returned a non-contiguous RGB image");
  }

  sensor_msgs::msg::Image output;
  output.header = source.header;
  output.height = height_;
  output.width = width_;
  output.encoding = source.encoding;
  output.is_bigendian = source.is_bigendian;
  output.step = static_cast<std::uint32_t>(checked_payload_size(width_, 1));
  output.data.resize(checked_payload_size(width_, height_));
  std::memcpy(output.data.data(), resized.data, output.data.size());
  return output;
}

}  // namespace arx5_d405_source_cpp
