# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Small utility helpers for keda unit tests."""

from ops import pebble
from ops.testing import State


def get_layer(state: State, container_name: str) -> pebble.Layer:
    """Return the combined pebble plan for ``container_name`` in ``state``."""
    container = state.get_container(container_name)
    return container.plan
