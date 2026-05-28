# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Direct branch tests for small internal helper methods."""

from types import SimpleNamespace

import pytest
from charmed_kubeflow_chisme.exceptions import GenericCharmRuntimeError
from ops.model import ModelError
from ops.pebble import PathError

from charm import KServeLLMISVCCharm


class _ContainerPushFails:
    def can_connect(self):
        return True

    def push(self, *_args, **_kwargs):
        raise PathError("push", "boom")


class _ContainerDisconnected:
    def can_connect(self):
        return False


class _ContainerServiceMissing:
    def can_connect(self):
        return True

    def get_service(self, _name):
        raise ModelError("missing")


def test_upload_certs_path_error_raises_runtime_error():
    """Path/protocol push failures should surface as GenericCharmRuntimeError."""
    fake_self = SimpleNamespace(model=SimpleNamespace(unit=SimpleNamespace(status=None)))

    def _check_connection(container):
        return KServeLLMISVCCharm._check_container_connection(fake_self, container)

    fake_self._check_container_connection = _check_connection
    certs_store = SimpleNamespace(key="k", cert="c", ca="a")

    with pytest.raises(GenericCharmRuntimeError, match="Failed to push certs"):
        KServeLLMISVCCharm._upload_certs_to_container(
            fake_self,
            _ContainerPushFails(),
            "/tmp/k8s-webhook-server/serving-certs",
            certs_store,
        )


def test_restart_controller_service_short_circuits_on_missing_service():
    """Restart helper should return cleanly when pebble service is absent."""
    fake_self = SimpleNamespace(
        controller_container=_ContainerServiceMissing(),
        _controller_container_name="llmisvc-controller",
    )

    KServeLLMISVCCharm._restart_controller_service(fake_self)


def test_restart_controller_service_short_circuits_when_container_disconnected():
    """Restart helper should return cleanly when controller container is unreachable."""
    fake_self = SimpleNamespace(
        controller_container=_ContainerDisconnected(),
        _controller_container_name="llmisvc-controller",
    )

    KServeLLMISVCCharm._restart_controller_service(fake_self)
