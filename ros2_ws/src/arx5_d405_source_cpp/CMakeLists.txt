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
find_package(Threads REQUIRED)
find_path(OPENCV_INCLUDE_DIR opencv2/core.hpp PATH_SUFFIXES opencv4)
find_library(OPENCV_CORE_LIBRARY opencv_core)
find_library(OPENCV_IMGPROC_LIBRARY opencv_imgproc)
if(NOT OPENCV_INCLUDE_DIR OR NOT OPENCV_CORE_LIBRARY OR NOT OPENCV_IMGPROC_LIBRARY)
  message(FATAL_ERROR "OpenCV core and imgproc development files are required")
endif()

add_library(rgb_image_resizer STATIC src/rgb_image_resizer.cpp)
target_compile_features(rgb_image_resizer PUBLIC cxx_std_17)
target_include_directories(rgb_image_resizer PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_include_directories(rgb_image_resizer SYSTEM PRIVATE ${OPENCV_INCLUDE_DIR})
ament_target_dependencies(rgb_image_resizer sensor_msgs)
target_link_libraries(
  rgb_image_resizer
  ${OPENCV_CORE_LIBRARY}
  ${OPENCV_IMGPROC_LIBRARY}
)

add_library(snapshot_shared_memory_writer STATIC src/snapshot_shared_memory_writer.cpp)
target_compile_features(snapshot_shared_memory_writer PUBLIC cxx_std_17)
target_include_directories(snapshot_shared_memory_writer PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
ament_target_dependencies(
  snapshot_shared_memory_writer
  arx5_collection_interfaces
  arx5_vla_snapshot
  sensor_msgs
)

add_library(snapshot_socket_server STATIC src/snapshot_socket_server.cpp)
target_compile_features(snapshot_socket_server PUBLIC cxx_std_17)
target_include_directories(snapshot_socket_server PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(snapshot_socket_server Threads::Threads)

add_executable(multi_d405_source src/multi_d405_source.cpp)
target_compile_features(multi_d405_source PUBLIC cxx_std_17)
ament_target_dependencies(multi_d405_source
  arx5_collection_interfaces
  arx5_vla_snapshot
  rclcpp
  sensor_msgs
)
target_link_libraries(
  multi_d405_source
  rgb_image_resizer
  snapshot_shared_memory_writer
  snapshot_socket_server
  realsense2::realsense2
)

install(
  TARGETS multi_d405_source
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_rgb_image_resizer test/test_rgb_image_resizer.cpp)
  target_link_libraries(test_rgb_image_resizer rgb_image_resizer)
  ament_add_gtest(
    test_snapshot_shared_memory_writer
    test/test_snapshot_shared_memory_writer.cpp
  )
  target_link_libraries(
    test_snapshot_shared_memory_writer
    snapshot_shared_memory_writer
  )
  ament_add_gtest(test_snapshot_socket_server test/test_snapshot_socket_server.cpp)
  target_link_libraries(test_snapshot_socket_server snapshot_socket_server)
endif()

ament_package()
