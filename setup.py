#!/usr/bin/env python3
"""Fallback setuptools build configuration for offline/local builds."""

from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).resolve().parent


setup(
    name="tsundoku",
    version="1.0.0",
    description="A terminal-first read-it-later tool for turning links into actionable work.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="cassette, aka maps",
    author_email="hello@cassette.help",
    url="https://github.com/mistermaps/tsundoku",
    project_urls={
        "Homepage": "https://cassette.help",
        "Repository": "https://github.com/mistermaps/tsundoku",
    },
    license="MIT",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    python_requires=">=3.10",
    entry_points={"console_scripts": ["tsundoku=tsundoku.workflows:main"]},
)
