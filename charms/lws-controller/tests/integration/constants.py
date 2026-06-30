# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

from pathlib import Path

import yaml
from lightkube.generic_resource import create_namespaced_resource

APP_NAME = "lws-controller"

# Path to the charm's metadata, used to resolve the OCI image resources the
# charm needs at deploy time.
METADATA = yaml.safe_load((Path(__file__).resolve().parents[2] / "metadata.yaml").read_text())

# Map every oci-image resource declared in metadata.yaml to its upstream source
# so the locally-built charm can be deployed with the same images it ships with.
IMAGE_RESOURCES = {
    name: data["upstream-source"]
    for name, data in METADATA.get("resources", {}).items()
    if data.get("type") == "oci-image"
}

# The CRD the controller is responsible for reconciling.
LWS_CRD_NAME = "leaderworkersets.leaderworkerset.x-k8s.io"

# Generic lightkube resource for the LeaderWorkerSet CRD. Registering it lets
# lightkube load, apply and delete LeaderWorkerSet custom resources.
LeaderWorkerSet = create_namespaced_resource(
    group="leaderworkerset.x-k8s.io",
    version="v1",
    kind="LeaderWorkerSet",
    plural="leaderworkersets",
)

# Cluster-scoped webhook configurations the charm installs. They must be removed
# when the application is removed.
MUTATING_WEBHOOK_NAME = "lws-mutating-webhook-configuration"
VALIDATING_WEBHOOK_NAME = "lws-validating-webhook-configuration"

# Sample LeaderWorkerSet custom resource applied to verify the controller
# reconciles workloads.
LWS_SAMPLE_CR_PATH = Path(__file__).parent / "crs" / "leaderworkerset.yaml"
LWS_SAMPLE_CR_NAME = "lws-sample"
