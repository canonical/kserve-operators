# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest
from unittest.mock import Mock

from ops.model import ActiveStatus
from ops.testing import Harness

from charm import OperatorTemplateCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(OperatorTemplateCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()
    
    def test_placeholder():
        assert False


