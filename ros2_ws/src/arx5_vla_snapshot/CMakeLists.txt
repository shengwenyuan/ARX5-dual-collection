cmake_minimum_required(VERSION 3.8)
project(arx5_vla_snapshot)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(arx5_collection_interfaces REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)

add_library(vla_snapshot_core src/snapshot_buffer.cpp)
target_compile_features(vla_snapshot_core PUBLIC cxx_std_17)
target_include_directories(vla_snapshot_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>
)
ament_target_dependencies(vla_snapshot_core
  arx5_collection_interfaces
  sensor_msgs
)

add_executable(vla_snapshot_source src/snapshot_node.cpp)
target_compile_features(vla_snapshot_source PUBLIC cxx_std_17)
ament_target_dependencies(vla_snapshot_source
  arx5_collection_interfaces
  rclcpp
  sensor_msgs
)
target_link_libraries(vla_snapshot_source vla_snapshot_core)

install(DIRECTORY include/ DESTINATION include)
install(
  TARGETS vla_snapshot_core vla_snapshot_source
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  ament_add_gtest(test_snapshot_buffer test/test_snapshot_buffer.cpp)
  target_link_libraries(test_snapshot_buffer vla_snapshot_core)
endif()

ament_export_include_directories(include)
ament_export_libraries(vla_snapshot_core)
ament_export_dependencies(arx5_collection_interfaces sensor_msgs)
ament_package()
