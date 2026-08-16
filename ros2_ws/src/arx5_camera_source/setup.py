from glob import glob

from setuptools import find_packages, setup


package_name = "arx5_camera_source"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            [f"resource/{package_name}"],
        ),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="ARX5 Collection",
    maintainer_email="devnull@example.com",
    description="Independent RealSense D405 ROS 2 image source.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "d405_source = arx5_camera_source.camera_node:main",
        ],
    },
)
