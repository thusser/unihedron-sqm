#!/usr/bin/env python3
from setuptools import setup, find_packages

setup(
    name="unihedron-sqm",
    version="0.1",
    description="Web interface for Unihedron Sky Quality Meters (SQM)",
    author="Tim-Oliver Husser",
    author_email="thusser@uni-goettingen.de",
    packages=find_packages(),
    entry_points={"console_scripts": ["sqm-web=sqm.web:main"]},
    package_data={"sqm": ["*.html", "static_html/*.css"]},
    include_package_data=True,
    install_requires=["pyserial", "tornado", "apscheduler", "numpy", "influxdb_client", "astropy"],
)
