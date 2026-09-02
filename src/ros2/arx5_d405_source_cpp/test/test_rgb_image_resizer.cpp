#include <algorithm>
#include <cstdint>
#include <stdexcept>

#include "arx5_d405_source_cpp/rgb_image_resizer.hpp"
#include "gtest/gtest.h"
#include "sensor_msgs/msg/image.hpp"

namespace
{

sensor_msgs::msg::Image rgb_image(std::uint32_t width, std::uint32_t height)
{
  sensor_msgs::msg::Image image;
  image.header.stamp.sec = 12;
  image.header.stamp.nanosec = 345;
  image.header.frame_id = "camera_left_color";
  image.width = width;
  image.height = height;
  image.encoding = "rgb8";
  image.step = width * 3;
  image.data.resize(static_cast<std::size_t>(image.step) * height);
  return image;
}

}  // namespace

TEST(RgbImageResizer, ProducesProfileDimensionsAndPreservesHeader)
{
  auto source = rgb_image(848, 480);
  std::fill(source.data.begin(), source.data.end(), 73);
  const auto original = source.data;

  const arx5_d405_source_cpp::RgbImageResizer resizer(640, 360);
  const auto output = resizer.resize(source);

  EXPECT_EQ(output.width, 640U);
  EXPECT_EQ(output.height, 360U);
  EXPECT_EQ(output.step, 1920U);
  EXPECT_EQ(output.data.size(), 640U * 360U * 3U);
  EXPECT_EQ(output.encoding, "rgb8");
  EXPECT_EQ(output.header, source.header);
  EXPECT_TRUE(std::all_of(output.data.begin(), output.data.end(),
    [](std::uint8_t value) {return value == 73;}));
  EXPECT_EQ(source.data, original);
}

TEST(RgbImageResizer, UsesAreaInterpolation)
{
  auto source = rgb_image(2, 2);
  const std::uint8_t values[] = {0, 100, 50, 150};
  for (std::size_t pixel = 0; pixel < 4; ++pixel) {
    for (std::size_t channel = 0; channel < 3; ++channel) {
      source.data[pixel * 3 + channel] = values[pixel];
    }
  }

  const auto output = arx5_d405_source_cpp::RgbImageResizer(1, 1).resize(source);

  ASSERT_EQ(output.data.size(), 3U);
  EXPECT_EQ(output.data[0], 75U);
  EXPECT_EQ(output.data[1], 75U);
  EXPECT_EQ(output.data[2], 75U);
}

TEST(RgbImageResizer, RejectsInvalidSourceContract)
{
  auto source = rgb_image(2, 2);
  source.encoding = "bgr8";
  const arx5_d405_source_cpp::RgbImageResizer resizer(1, 1);
  EXPECT_THROW(resizer.resize(source), std::invalid_argument);

  source.encoding = "rgb8";
  source.step += 1;
  EXPECT_THROW(resizer.resize(source), std::invalid_argument);
}

TEST(RgbImageResizer, RejectsInvalidTargetDimensions)
{
  EXPECT_THROW(arx5_d405_source_cpp::RgbImageResizer(0, 360), std::invalid_argument);
  EXPECT_THROW(arx5_d405_source_cpp::RgbImageResizer(640, 0), std::invalid_argument);
}
