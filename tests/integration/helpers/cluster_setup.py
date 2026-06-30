#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Cluster prerequisites for the bundle integration tests.

These helpers install the third-party CRDs/controllers (Gateway API, the
inference extension, Envoy Gateway, Envoy AI Gateway and LeaderWorkerSet) and
create the shared Gateway resource the charms route through.

Resource operations go through lightkube. The two exceptions are intentional:
Helm chart installs (``helm``) and applying upstream multi-document install
bundles served from remote URLs (``kubectl apply -f <url>``), neither of which
lightkube performs.
"""

import logging

from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Namespace

from .constants import (
    NAMESPACE_ENVOY_AI_GATEWAY,
    NAMESPACE_ENVOY_GATEWAY,
)
from .k8s import apply_yaml, get_client, wait_for_crd_established, wait_for_deployment_available
from .kubectl import helm, kubectl

logger = logging.getLogger(__name__)


def install_gateway_api_crds(version: str) -> None:
    logger.info("Installing Gateway API CRDs (version %s)...", version)
    kubectl(
        [
            "apply",
            "-f",
            (
                "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
                f"{version}/standard-install.yaml"
            ),
        ]
    )
    logger.info("Waiting for GatewayClass CRD to be established...")
    wait_for_crd_established("gatewayclasses.gateway.networking.k8s.io")
    logger.info("Gateway API CRDs installed")


def install_gie_crds(version: str) -> None:
    logger.info("Installing Gateway API Inference Extension CRDs (version %s)...", version)
    kubectl(
        [
            "apply",
            "-f",
            (
                "https://github.com/kubernetes-sigs/gateway-api-inference-extension/"
                f"releases/download/{version}/manifests.yaml"
            ),
        ]
    )
    wait_for_crd_established("inferencepools.inference.networking.x-k8s.io")
    logger.info("Gateway API Inference Extension CRDs installed")


def install_envoy_gateway(envoy_gateway_version: str, envoy_ai_gateway_version: str) -> None:
    logger.info("Installing Envoy Gateway (version %s)...", envoy_gateway_version)
    helm(
        [
            "upgrade",
            "-i",
            "eg",
            "oci://docker.io/envoyproxy/gateway-helm",
            "--version",
            envoy_gateway_version,
            "--namespace",
            NAMESPACE_ENVOY_GATEWAY,
            "--create-namespace",
            "-f",
            (
                "https://raw.githubusercontent.com/envoyproxy/ai-gateway/"
                f"{envoy_ai_gateway_version}/manifests/envoy-gateway-values.yaml"
            ),
            "-f",
            (
                "https://raw.githubusercontent.com/envoyproxy/ai-gateway/"
                f"{envoy_ai_gateway_version}/examples/inference-pool/"
                "envoy-gateway-values-addon.yaml"
            ),
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    logger.info("Waiting for Envoy Gateway controller to be ready...")
    wait_for_deployment_available(NAMESPACE_ENVOY_GATEWAY, "envoy-gateway")
    logger.info("Envoy Gateway installed")


def install_envoy_ai_gateway(envoy_ai_gateway_version: str) -> None:
    helm(
        [
            "upgrade",
            "-i",
            "aieg-crd",
            "oci://docker.io/envoyproxy/ai-gateway-crds-helm",
            "--version",
            envoy_ai_gateway_version,
            "--namespace",
            NAMESPACE_ENVOY_AI_GATEWAY,
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    helm(
        [
            "upgrade",
            "-i",
            "aieg",
            "oci://docker.io/envoyproxy/ai-gateway-helm",
            "--version",
            envoy_ai_gateway_version,
            "--namespace",
            NAMESPACE_ENVOY_AI_GATEWAY,
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
    )
    wait_for_deployment_available(NAMESPACE_ENVOY_AI_GATEWAY, "ai-gateway-controller")


def ensure_gateway(kserve_namespace: str, gateway_name: str, gateway_namespace: str) -> None:
    logger.info("Creating namespace '%s'...", kserve_namespace)
    get_client().apply(Namespace(metadata=ObjectMeta(name=kserve_namespace)))

    logger.info("Creating Gateway '%s' in namespace '%s'...", gateway_name, gateway_namespace)
    gateway_manifest = f"""apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: {gateway_name}
  namespace: {gateway_namespace}
spec:
  gatewayClassName: envoy
  listeners:
  - name: http
    protocol: HTTP
    port: 80
    allowedRoutes:
      namespaces:
        from: All
  infrastructure:
    labels:
      serving.kserve.io/gateway: {gateway_name}
"""
    apply_yaml(gateway_manifest)
    logger.info("Gateway resource created")
