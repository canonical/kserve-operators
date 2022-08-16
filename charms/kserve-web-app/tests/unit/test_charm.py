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

    @patch("charm.KubernetesServicePatch", lambda x, y: None)
    @patch("charm.KServeWebAppCharm._update_layer")
    def test_main_ingress(self, update):
        self.harness.set_leader(True)
        rel_id = self.harness.add_relation("ingress", "app")
        self.harness.add_relation_unit(rel_id, "app/0")
        self.harness.update_relation_data(
            rel_id,
            "app",
            {"_supported_versions": "- v1"},
        )
        self.harness.begin_with_initial_hooks()

        relation_data = self.harness.get_relation_data(rel_id, self.harness.charm.app.name)
        data = {
            "prefix": "/kserve-endpoints/",
            "rewrite": "/",
            "service": "kserve-web-app",
            "port": 80,
        }

        assert data == yaml.safe_load(relation_data["data"])
