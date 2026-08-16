from setuptools import find_packages, setup


package_name = "arx5_monitoring"

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
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="ARX5 Collection",
    maintainer_email="devnull@example.com",
    description="Lightweight source-side telemetry for ARX5 collection streams.",
    license="Proprietary",
)
