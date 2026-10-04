#!/usr/bin/env python3
# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Allocate YYYY.MM.N from GHCR tags; existing versions are never overwritten."""
from datetime import datetime, timezone
import os
import re
import subprocess
import sys


VERSION = re.compile(r"(20[0-9]{2})\.(0?[1-9]|1[0-2])\.([1-9][0-9]*)")


def canonical(tag):
    match = VERSION.fullmatch(tag)
    if not match:
        raise ValueError("Image version must use YYYY.MM.N")
    year, month, number = map(int, match.groups())
    return f"{year}.{month:02d}.{number}"


def select_version(tags, requested="", now=None, replace_existing=False):
    versions = {canonical(tag) for tag in tags if VERSION.fullmatch(tag)}
    if requested:
        version = canonical(requested)
    else:
        month = (now or datetime.now(timezone.utc)).strftime("%Y.%m.")
        numbers = [int(tag.rsplit(".", 1)[1]) for tag in versions if tag.startswith(month)]
        version = month + str(max(numbers, default=0) + 1)
    if version in versions and not (requested and replace_existing):
        raise ValueError(f"Refusing to overwrite existing image version: {version}")
    return version


def main():
    result = subprocess.run([
        "gh", "api", "--paginate",
        "users/safrano9999/packages/container/citadel/versions?per_page=100",
        "--jq", ".[].metadata.container.tags[]",
    ], capture_output=True, text=True, timeout=60)
    if result.returncode and "(HTTP 404)" not in result.stderr:
        raise RuntimeError("Cannot read GHCR versions; refusing to guess a version")
    tags = result.stdout.splitlines() if result.returncode == 0 else []
    print(select_version(tags, os.environ.get("IMAGE_TAG", ""),
                         replace_existing=os.environ.get("REPLACE_EXISTING") == "true"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
