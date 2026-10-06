#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Container images used by the integration tests, read from the charms' pinned defaults."""

import json
from pathlib import Path

_CHARMS_DIR = Path(__file__).resolve().parents[3] / "charms"


def _default_images(charm_name: str) -> dict:
    return json.loads((_CHARMS_DIR / charm_name / "src/default-custom-images.json").read_text())


STORAGE_INITIALIZER_IMAGE = _default_images("kserve-controller")["configmap__storageInitializer"]
# Manual examples use llm-integrator's pinned vLLM image so nodes pull a single image.
VLLM_IMAGE = _default_images("llm-integrator")["vllm"]
