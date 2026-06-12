#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Cluster prerequisites for the bundle integration tests.

These helpers install the third-party CRDs/controllers (Gateway API, the
inference extension, Envoy Gateway, Envoy AI Gateway and LeaderWorkerSet) and
create the shared Gateway resource the charms route through.
"""

import logging

from .constants import (
    NAMESPACE_ENVOY_AI_GATEWAY,
    NAMESPACE_ENVOY_GATEWAY,
    NAMESPACE_LWS,
)
from .kubectl import (
    helm,
    kubectl,
    kubectl_apply_stdin,
    rollout_status,
    wait_for_crd_established,
)

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
    rollout_status(NAMESPACE_ENVOY_GATEWAY, "deploy/envoy-gateway")
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
    rollout_status(NAMESPACE_ENVOY_AI_GATEWAY, "deploy/ai-gateway-controller")


def install_lws(version: str) -> None:
    """Install LeaderWorkerSet CRDs/controller via Helm."""
    logger.info("Installing LWS via Helm (version %s)...", version)
    helm(
        [
            "upgrade",
            "-i",
            "lws",
            "oci://registry.k8s.io/lws/charts/lws",
            "--version",
            version,
            "--namespace",
            NAMESPACE_LWS,
            "--create-namespace",
            "--wait",
            "--timeout",
            "300s",
        ]
    )

    rollout_status(NAMESPACE_LWS, "deploy/lws-controller-manager")
    wait_for_crd_established("leaderworkersets.leaderworkerset.x-k8s.io")
    logger.info("LWS installed")


def ensure_gateway(kserve_namespace: str, gateway_name: str, gateway_namespace: str) -> None:
    logger.info("Creating namespace '%s'...", kserve_namespace)
    ns_yaml = kubectl(["create", "namespace", kserve_namespace, "--dry-run=client", "-o", "yaml"])
    kubectl_apply_stdin(ns_yaml)

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
    kubectl_apply_stdin(gateway_manifest)

    logger.info("Gateway resource created, checking status...")
    kubectl(["-n", gateway_namespace, "get", "gateway", gateway_name, "-o", "wide"])
