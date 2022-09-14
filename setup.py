import setuptools
import subprocess
import os

with open("README.md", "r") as fh:
    long_description = fh.read()

requirements = [
    'osmium',
    'pyparsing',
    'pyyaml',
    'shapely',
    'requests',
    'landez'
]

setuptools.setup(
    name="osm-export-tool",
    version="0.1.0",
    author="Maxar Technologies",
    author_email="foundationgeoint-packaging@maxar.com",
    description="Convert OpenStreetMap data into GIS and mobile mapping file formats.",
    license="BSD-3-Clause",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/radiant-maxar/osm-export-tool-python",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
    ],
    scripts=['bin/osm-export-tool'],
    install_requires = requirements,
    requires_python='>=3.0',
    package_data={'osm_export_tool':['mappings/*.yml']}
)
