.. _reference-projects:

Underlying projects and dependencies
====================================

Charmed llm-d packages several open-source projects as Juju charms. This page lists the
versions shipped with the current release; the pages below describe each project's
role in the stack.

Versions
--------

.. list-table::
   :header-rows: 1

   * - Project
     - Version
     - Delivered by
   * - `KServe`_
     - |kserve_version|
     - ``kserve-controller``, ``kserve-llmisvc``
   * - `vLLM`_
     - |vllm_version|
     - ``llm-integrator`` (default runtime images)
   * - `llm-d`_ inference scheduler
     - |llmd_version|
     - ``kserve-llmisvc``
   * - `KEDA`_
     - |keda_version|
     - ``keda-controller``
   * - `LeaderWorkerSet`_
     - |lws_version|
     - ``lws-controller``
   * - `Envoy Gateway`_
     - |envoy_gateway_version|
     - ``envoy-controller-k8s``, ``envoy-ingress-k8s``
   * - `Envoy AI Gateway`_
     - |envoy_ai_gateway_version|
     - ``envoy-ai-controller-k8s``
   * - `Canonical Observability Stack`_
     - Channel |cos_channel|
     - COS Lite charms

The stack is tested on the following platform:

.. list-table::
   :header-rows: 1

   * - Component
     - Version
   * - Ubuntu
     - |ubuntu_version|
   * - `Canonical Kubernetes`_
     - Channel |k8s_channel|
   * - `Juju`_
     - Channel |juju_channel|
   * - `Terraform`_
     - |terraform_version| or later, with the `Juju Terraform provider`_
       |juju_provider_version| or later
   * - `NVIDIA GPU Operator`_
     - Latest Helm chart, installed with Helm |helm_version|
   * - `autoscaling-model-serving`_
     - Branch |solution_track|

The charms are published on the |charm_channel| channel of `Charmhub`_.

Projects
--------

.. toctree::
   :maxdepth: 1

   kserve
   vllm
   keda
   lws
   envoy
   cos
