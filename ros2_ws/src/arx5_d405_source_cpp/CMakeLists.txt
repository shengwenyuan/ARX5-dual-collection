cmake_minimum_required(VERSION 3.8)
project(arx5_d405_source_cpp)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(arx5_collection_interfaces REQUIRED)
find_package(arx5_vla_snapshot REQUIRED)
find_package(rclcpp REQUIRED)
find_package(realsense2 REQUIRED)
find_package(sensor_msgs REQUIRED)

add_executable(multi_d405_source src/multi_d405_source.cpp)
target_compile_features(multi_d405_source PUBLIC cxx_std_17)
ament_target_dependencies(multi_d405_source
  arx5_collection_interfaces
  arx5_vla_snapshot
  rclcpp
  sensor_msgs
)
target_link_libraries(multi_d405_source realsense2::realsense2)

install(
  TARGETS multi_d405_source
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)

ament_package()
