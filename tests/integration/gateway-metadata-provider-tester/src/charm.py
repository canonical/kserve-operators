#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Gateway metadata provider test charm."""

import json
import logging

import ops

logger = logging.getLogger(__name__)

_RELATION_NAME = "gateway-metadata"
_METADATA = {
    "namespace": "kubeflow",
    "gateway_name": "kserve-ingress-gateway",
    "deployment_name": "envoy-ai-gateway",
    "service_account": "envoy-ai-gateway",
}


class GatewayMetadataProviderTesterCharm(ops.CharmBase):
    """Publish fixed gateway metadata for integration tests."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        for event in [
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.update_status,
            self.on.leader_elected,
            self.on[_RELATION_NAME].relation_joined,
            self.on[_RELATION_NAME].relation_changed,
            self.on[_RELATION_NAME].relation_broken,
        ]:
            self.framework.observe(event, self._on_event)

    def _on_event(self, _):
        relations = self.model.relations.get(_RELATION_NAME, [])
        if not relations:
            self.unit.status = ops.BlockedStatus("Waiting for gateway-metadata relation")
            return

        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus()
            return

        payload = json.dumps(_METADATA, sort_keys=True)
        for relation in relations:
            relation.data[self.app]["metadata"] = payload

        logger.info("Published gateway metadata for %d relation(s)", len(relations))
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(GatewayMetadataProviderTesterCharm)
