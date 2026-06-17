#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared constants for the bundle integration tests."""

# Kubernetes namespaces used across the bundle setup and assertions.
NAMESPACE_DEFAULT = "default"
NAMESPACE_KUBEFLOW = "kubeflow"
NAMESPACE_ENVOY_GATEWAY = "envoy-gateway-system"
NAMESPACE_ENVOY_AI_GATEWAY = "envoy-ai-gateway-system"
NAMESPACE_LWS = "lws-system"

# kserve-llmisvc workload and example defaults.
LLMISVC_APP_NAME = "kserve-llmisvc"
LLMISVC_NAME = "test-llm-scheduler-small"
LLMISVC_MODEL_NAME = "EleutherAI/pythia-70m"

# Metrics ports exposed by the llmisvc service and the local ports we forward
# them to when probing the Prometheus endpoints directly.
LLMISVC_CONTROLLER_METRICS_PORT = 8080
LLMISVC_AGGREGATED_METRICS_PORT = 15090
LOCAL_CONTROLLER_METRICS_PORT = 28080
LOCAL_AGGREGATED_METRICS_PORT = 25090
