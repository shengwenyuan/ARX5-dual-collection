from glob import glob

from setuptools import find_packages, setup


package_name = "arx5_arm_adapter"

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
    description="Read-only adapter from official ARX5 status to collection ArmState.",
    license="Proprietary",
    entry_points={
        "console_scripts": [
            "arm_state_adapter = arx5_arm_adapter.adapter_node:main",
        ],
    },
)
