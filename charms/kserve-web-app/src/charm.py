#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
import traceback

from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import ChangeError, Layer

log = logging.getLogger(__name__)


class KServeWebAppCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.kserve_web_app_pebble_ready, self._on_kserve_web_app_ready)

        self._container_name = "kserve-web-app"
        self.container = self.unit.get_container(self._container_name)

        self.service_patcher = KubernetesServicePatch(
            self, [(self._container_name, self.model.config["port"])]
        )

    @property
    def _pebble_layer(self):
        """Return the Pebble layer for the workload."""
        return Layer(
            {
                "services": {
                    "kserve-web-app": {
                        "override": "replace",
                        "summary": "KServe Web App",
                        "command": "gunicorn -w 3 --bind 0.0.0.0:5000 --access-logfile - entrypoint:app",
                        "startup": "enabled",
                        "environment": {
                            "USERID_HEADER": "kubeflow-userid",
                            "APP_PREFIX": "/kserve-endpoints",
                            "APP_DISABLE_AUTH": True,
                        },
                    }
                }
            }
        )

    def _on_kserve_web_app_ready(self, event):
        """Define and start a workload using the Pebble API.

        Learn more about Pebble layers at https://github.com/canonical/pebble
        """
        self._update_layer()
        self.unit.status = ActiveStatus()

    def _update_layer(self) -> None:
        """Updates the Pebble configuration layer if changed."""
        current_layer = self.container.get_plan()
        new_layer = self._pebble_layer

        if current_layer.services != new_layer.services:
            self.container.add_layer(self._container_name, new_layer, combine=True)
            try:
                log.info("Pebble plan updated with new configuration, replanning")
                self.container.replan()
            except ChangeError as e:
                log.error(traceback.format_exc())
                self.unit.status = BlockedStatus("Failed to replan")
                raise e
                return


if __name__ == "__main__":
    main(KServeWebAppCharm)
