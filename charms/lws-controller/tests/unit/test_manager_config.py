# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the manager-config and log-level charm config options."""

import dataclasses
from types import SimpleNamespace

import pytest
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus
from ops.model import ActiveStatus, BlockedStatus

from charm import _MANAGER_CONFIG, LWSControllerCharm

from .helpers import assert_status

VALID_OVERRIDE = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
leaderElection:
  leaderElect: true
internalCertManagement:
  enable: false
clientConnection:
  qps: 500
  burst: 500
"""

OVERRIDE_WITH_CERTDIR = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
internalCertManagement:
  enable: false
webhook:
  certDir: /tmp/k8s-webhook-server/serving-certs
"""

OVERRIDE_MISSING_INTERNAL_CERT = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
leaderElection:
  leaderElect: true
"""

OVERRIDE_INTERNAL_CERT_TRUE = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
internalCertManagement:
  enable: true
"""

OVERRIDE_WRONG_CERTDIR = """apiVersion: config.lws.x-k8s.io/v1alpha1
kind: Configuration
internalCertManagement:
  enable: false
webhook:
  certDir: /etc/some/other/path
"""


def _validate_manager_config(value: str) -> str:
    """Call the validator directly with a fake charm carrying ``value``."""
    fake_self = SimpleNamespace(config={"manager-config": value})
    return LWSControllerCharm._validated_manager_config(fake_self)


# --- manager-config validation -------------------------------------------------


def test_empty_manager_config_returns_default():
    """An unset manager-config falls back to the built-in default."""
    assert _validate_manager_config("") == _MANAGER_CONFIG


def test_valid_override_returned_verbatim():
    """A valid override is returned exactly as provided (no merging)."""
    assert _validate_manager_config(VALID_OVERRIDE) == VALID_OVERRIDE.strip()


def test_valid_override_with_correct_certdir():
    """A correct webhook.certDir passes validation."""
    assert _validate_manager_config(OVERRIDE_WITH_CERTDIR) == OVERRIDE_WITH_CERTDIR.strip()


def test_missing_internal_cert_management_blocks():
    """Omitting internalCertManagement is rejected."""
    with pytest.raises(ErrorWithStatus) as exc:
        _validate_manager_config(OVERRIDE_MISSING_INTERNAL_CERT)
    assert isinstance(exc.value.status, BlockedStatus)
    assert "internalCertManagement.enable" in exc.value.status.message


def test_internal_cert_management_true_blocks():
    """internalCertManagement.enable: true clashes with charm-managed certs."""
    with pytest.raises(ErrorWithStatus) as exc:
        _validate_manager_config(OVERRIDE_INTERNAL_CERT_TRUE)
    assert isinstance(exc.value.status, BlockedStatus)


def test_invalid_yaml_blocks():
    """Malformed YAML is rejected."""
    with pytest.raises(ErrorWithStatus) as exc:
        _validate_manager_config("foo: [unclosed")
    assert isinstance(exc.value.status, BlockedStatus)


def test_non_mapping_blocks():
    """A YAML document that is not a mapping is rejected."""
    with pytest.raises(ErrorWithStatus) as exc:
        _validate_manager_config("- a\n- b\n")
    assert isinstance(exc.value.status, BlockedStatus)


def test_wrong_certdir_blocks():
    """A webhook.certDir that does not match the charm's path is rejected."""
    with pytest.raises(ErrorWithStatus) as exc:
        _validate_manager_config(OVERRIDE_WRONG_CERTDIR)
    assert isinstance(exc.value.status, BlockedStatus)
    assert "certDir" in exc.value.status.message


# --- end-to-end reconcile ------------------------------------------------------


def test_config_changed_valid_override_active(ctx, base_state):
    """A valid manager-config override reconciles to Active."""
    state_in = dataclasses.replace(base_state, config={"manager-config": VALID_OVERRIDE})
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, ActiveStatus)


def test_config_changed_invalid_override_blocks_before_apply(ctx, base_state, mock_krh_apply):
    """An invalid manager-config blocks the unit before applying k8s resources."""
    state_in = dataclasses.replace(
        base_state, config={"manager-config": OVERRIDE_INTERNAL_CERT_TRUE}
    )
    out = ctx.run(ctx.on.config_changed(), state_in)
    assert_status(out, BlockedStatus, msg_substr="internalCertManagement.enable")
    mock_krh_apply.assert_not_called()
