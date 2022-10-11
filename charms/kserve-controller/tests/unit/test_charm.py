# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import patch

import yaml
from ops.model import ActiveStatus
from ops.testing import Harness

from charm import KServeControllerCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(KServeControllerCharm)
        self.addCleanup(self.harness.cleanup)

    @patch("charm.KubernetesServicePatch", lambda x, y: None)
    def test_pebble_ready(self):
        self.harness.begin_with_initial_hooks()
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())
