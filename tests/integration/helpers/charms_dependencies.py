# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""External (Charmhub) charm dependencies for the bundle integration tests.

The kserve charms under test are built locally and resolved via ``--charms-path``.
The charms below are the third-party dependencies deployed from Charmhub; their
deploy coordinates (channel, trust, config) are centralised here so they can be
bumped in one place.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CharmDependency:
    """Coordinates for deploying a Charmhub charm dependency."""

    charm: str
    channel: str
    trust: bool = False
    config: Optional[dict] = None


# Envoy gateway charm stack. envoy-ingress-k8s creates the Gateway and provides
# gateway-metadata to kserve-controller; the charms bring the full gateway stack
# themselves, so no Helm/CRD pre-installation is needed.
ENVOY_CONTROLLER = CharmDependency("envoy-controller-k8s", "latest/edge", trust=True)
ENVOY_AI_CONTROLLER = CharmDependency("envoy-ai-controller-k8s", "latest/edge", trust=True)
ENVOY_INGRESS = CharmDependency("envoy-ingress-k8s", "latest/edge", trust=True)
SELF_SIGNED_CERTIFICATES = CharmDependency("self-signed-certificates", "latest/stable")
