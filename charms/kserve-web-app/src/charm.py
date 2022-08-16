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

from ops.charm import CharmBase
from charms.observability_libs.v0.kubernetes_service_patch import KubernetesServicePatch
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ChangeError, Layer
from serialized_data_interface import get_interface, NoVersionsListed

log = logging.getLogger(__name__)


class KServeWebAppCharm(CharmBase):
    """Charm the service."""


    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.kserve_web_app_pebble_ready,
                               self._on_kserve_web_app_ready)
        for event in [
            self.on.ingress_relation_changed,
            self.on.ingress_relation_joined,
        ]:
            self.framework.observe(event, self._handle_ingress_relation)

        self.framework.observe(self.on.config_changed, self._on_config_changed)

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
                        }
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

    def _handle_ingress_relation(self, event):
        ingress = self._get_interface("ingress")

        if ingress:
            for app_name, version in ingress.versions.items():
                data = {
                    "prefix": "/kserve-endpoints/",
                    "rewrite": "/",
                    "service": self.model.app.name,
                    "port": self.model.config["port"],
                }

                ingress.send_data(data, app_name)


    def _on_config_changed(self, event):
        """Event needs to be observed but the port config option, which is the
        only one, is handled in the init function, therefore we pass"""
        pass

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

    def _get_interface(self, interface_name):
        # Remove this abstraction when SDI adds .status attribute to NoVersionsListed,
        # NoCompatibleVersionsListed:
        # https://github.com/canonical/serialized-data-interface/issues/26
        try:
            try:
                interface = get_interface(self, interface_name)
            except NoVersionsListed as err:
                raise CheckFailedError(str(err), WaitingStatus)
        except CheckFailedError as err:
            log.debug("_get_interface ~ Checkfailederror catch")
            self.model.unit.status = err.status
            log.info(str(err.status))
            return

        return interface


class CheckFailedError(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(msg)


if __name__ == "__main__":
    main(KServeWebAppCharm)
