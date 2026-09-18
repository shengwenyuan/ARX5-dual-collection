"""Compile the exact added C++ command/hold methods against a small hardware stub.

This exercises limits and gripper isolation, not a ROS/SDK or real-machine build.
"""

from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_cpp_guard_rejects_stale_jumps_and_preserves_gripper(tmp_path):
    compiler = shutil.which("c++") or shutil.which("clang++")
    if not compiler:
        pytest.skip("C++ compiler not installed")
    patch = (ROOT / "docker/patches/arx-x5-calibration-control.patch").read_text()
    additions = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    methods = additions.split("void X5Controller::freezeCalibration() {", 1)[1].split(
        "  if (calibration_mode_ && !manual_takeover_.load()", 1
    )[0]
    methods = "void X5Controller::freezeCalibration() {" + methods
    source = (
        r"""
#include <algorithm>
#include <atomic>
#include <array>
#include <cassert>
#include <chrono>
#include <cmath>
#include <memory>
#include <string>
#include <vector>
#include <limits>
namespace std_srvs { namespace srv { struct Trigger {struct Request{}; struct Response {bool success; std::string message;};}; }}
namespace arx5_arm_msg { namespace msg { struct RobotStatus {struct Header {double stamp=100.;} header; std::array<double,7> joint_pos{};}; }}
namespace rclcpp {
struct Time {double value; Time(double v):value(v){} struct Delta {double v; double seconds(){return v;}}; Delta operator-(Time b){return {value-b.value};}};
}
struct FakeClock {rclcpp::Time now(){return {100.};}};
struct InterfacesThread {
 enum state {POSITION_CONTROL};
 std::vector<double> actual=std::vector<double>(7,0.);
 std::vector<double> command;
 int gripper_calls=0;
 std::vector<double> getJointPositons(){return actual;}
 void setJointPositions(std::vector<double> q){command=q;}
 void setArmStatus(int){}
 void setCatch(double){++gripper_calls;}
};
struct X5Controller {
 std::atomic<bool> manual_takeover_{false};
 std::shared_ptr<InterfacesThread> interfaces_ptr_=std::make_shared<InterfacesThread>();
 std::vector<double> calibration_target_=std::vector<double>(6,0.);
 std::chrono::steady_clock::time_point calibration_last_command_=std::chrono::steady_clock::now();
 FakeClock clock; FakeClock* get_clock(){return &clock;}
 void freezeCalibration();
 void calibrationHold(const std::shared_ptr<std_srvs::srv::Trigger::Request>, std::shared_ptr<std_srvs::srv::Trigger::Response>);
 void calibrationCommand(const arx5_arm_msg::msg::RobotStatus&);
};
"""
        + methods
        + r"""
int main(){
 X5Controller c; arx5_arm_msg::msg::RobotStatus message;
 c.calibration_last_command_=std::chrono::steady_clock::now()-std::chrono::milliseconds(20);
 message.joint_pos[0]=.002;
 c.calibrationCommand(message);
 assert(!c.manual_takeover_ && c.interfaces_ptr_->command[0]==.002);
 message.joint_pos[0]=.5;
 c.calibrationCommand(message);
 assert(c.manual_takeover_ && c.interfaces_ptr_->command[0]==0.);
 c.manual_takeover_=false;
 message.joint_pos[0]=0.; message.header.stamp=99.;
 c.calibrationCommand(message); assert(c.manual_takeover_);
 c.manual_takeover_=false; message.header.stamp=100.;
 c.calibration_last_command_=std::chrono::steady_clock::now()-std::chrono::milliseconds(300);
 c.calibrationCommand(message); assert(c.manual_takeover_);
 c.manual_takeover_=false;
 c.calibration_last_command_=std::chrono::steady_clock::now();
 message.joint_pos[0]=std::numeric_limits<double>::quiet_NaN();
 c.calibrationCommand(message); assert(c.manual_takeover_);
 assert(c.interfaces_ptr_->gripper_calls==0);
 c.interfaces_ptr_->actual[0]=std::numeric_limits<double>::quiet_NaN();
 auto response=std::make_shared<std_srvs::srv::Trigger::Response>();
 c.calibrationHold(nullptr,response); assert(!response->success);
}
"""
    )
    path = tmp_path / "guard.cpp"
    path.write_text(source)
    executable = tmp_path / "guard"
    subprocess.run(
        [compiler, "-std=c++17", str(path), "-o", str(executable)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
