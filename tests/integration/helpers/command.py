#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import subprocess


def run_command(command: list[str], check: bool = True, capture_output: bool = True) -> str:
    result = subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture_output,
    )
    return (result.stdout or "").strip()
