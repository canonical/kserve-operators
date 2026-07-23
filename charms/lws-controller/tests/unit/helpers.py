# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Small utility helpers for lws-controller unit tests."""

from typing import Optional, Type

from ops import pebble
from ops.model import StatusBase
from ops.testing import State


def assert_status(state: State, status_cls: Type[StatusBase], msg_substr: Optional[str] = None):
    """Assert ``state.unit_status`` is of ``status_cls`` and contains ``msg_substr``."""
    assert isinstance(state.unit_status, status_cls), (
        f"Expected {status_cls.__name__}, got {type(state.unit_status).__name__}: "
        f"{state.unit_status}"
    )
    if msg_substr is not None:
        assert msg_substr in state.unit_status.message, (
            f"Expected substring {msg_substr!r} in status message, "
            f"got {state.unit_status.message!r}"
        )


def get_layer(state: State, container_name: str) -> pebble.Layer:
    """Return the combined pebble plan for ``container_name`` in ``state``."""
    container = state.get_container(container_name)
    return container.plan
