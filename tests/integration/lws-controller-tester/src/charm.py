#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""lws-controller readiness provider test charm."""

import logging

import ops

logger = logging.getLogger(__name__)

_RELATION_NAME = "lws-controller"


class LWSControllerTesterCharm(ops.CharmBase):
    """Publish a fixed ``ready=true`` payload for integration tests."""

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
            self.unit.status = ops.BlockedStatus("Waiting for lws-controller relation")
            return

        if not self.unit.is_leader():
            self.unit.status = ops.ActiveStatus()
            return

        for relation in relations:
            relation.data[self.app].update(
                {
                    "ready": "true",
                    "namespace": self.model.name,
                }
            )

        logger.info("Published lws-controller ready=true for %d relation(s)", len(relations))
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(LWSControllerTesterCharm)
