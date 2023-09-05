from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in qwave_patches/__init__.py
from qwave_patches import __version__ as version

setup(
	name="qwave_patches",
	version=version,
	description="apk that patches some bughs in ERPNext",
	author="umer2001",
	author_email="umer2001.uf@gmail.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
