# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import patch

import yaml
from ops.model import ActiveStatus
from ops.testing import Harness

from charm import KServeWebAppCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(KServeWebAppCharm)
        self.addCleanup(self.harness.cleanup)

    @patch("charm.KubernetesServicePatch", lambda x, y: None)
    def test_pebble_ready(self):
        self.harness.begin()
        self.harness.container_pebble_ready("kserve-web-app")
        self.assertTrue(self.harness.charm.container.get_service("kserve-web-app").is_running())
        self.assertEqual(self.harness.charm.unit.status, ActiveStatus())
