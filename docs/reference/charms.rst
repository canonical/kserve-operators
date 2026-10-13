Charms
======

Charmed llm-d is made of the charms below. Their Charmhub pages list every
configuration option, integration and action, and are generated from the charms
themselves.

Charms from this project
------------------------

These charms are developed in the `kserve-operators`_ repository, except
``lws-controller``, which has its own `lws-controller-operator`_ repository.

.. list-table::
   :header-rows: 1

   * - Charm
     - Purpose
   * - `kserve-controller <https://charmhub.io/kserve-controller>`_
     - KServe control plane.
   * - `kserve-llmisvc <https://charmhub.io/kserve-llmisvc>`_
     - Controller for ``LLMInferenceService``, plus the vLLM metrics, alert rules and
       dashboards.
   * - `llm-integrator <https://charmhub.io/llm-integrator>`_
     - Serves one model from Juju configuration, optionally autoscaled.
   * - `keda-controller <https://charmhub.io/keda-controller>`_
     - KEDA autoscaler.
   * - `lws-controller <https://charmhub.io/lws-controller>`_
     - LeaderWorkerSet controller, required by ``kserve-llmisvc``.

Dependencies
------------

.. list-table::
   :header-rows: 1

   * - Charm
     - Purpose
   * - `envoy-controller-k8s <https://charmhub.io/envoy-controller-k8s>`_
     - Envoy Gateway controller.
   * - `envoy-ai-controller-k8s <https://charmhub.io/envoy-ai-controller-k8s>`_
     - Envoy AI Gateway controller.
   * - `envoy-ingress-k8s <https://charmhub.io/envoy-ingress-k8s>`_
     - The gateway that serves the models.
   * - `self-signed-certificates <https://charmhub.io/self-signed-certificates>`_
     - TLS certificate for the AI Gateway.
   * - `opentelemetry-collector-k8s <https://charmhub.io/opentelemetry-collector-k8s>`_
     - Forwards metrics, logs and dashboards to COS.
   * - `s3-integrator <https://charmhub.io/s3-integrator>`_
     - Optional. S3 credentials for models stored in a bucket.

Integrations
------------

The Terraform solution creates these integrations. ``llm-integrator`` applications add
their own when you deploy them.

.. mermaid::

   flowchart TB
       ai[envoy-ai-controller-k8s] -->|tls-certificates| tls[self-signed-certificates]
       envoy[envoy-controller-k8s] -->|envoy-extension-server| ai
       kserve[kserve-controller] -->|gateway-metadata| ingress[envoy-ingress-k8s]
       llmisvc[kserve-llmisvc] -->|kserve-controller| kserve
       llmisvc -->|lws-controller| lws[lws-controller]
       integrator[llm-integrator] -->|kserve-llmisvc| llmisvc
       integrator -.->|keda| keda[keda-controller]
       integrator -.->|prometheus-api| prometheus[Prometheus in COS]
       integrator -.->|s3-credentials| bucket[s3-integrator]

Dashed integrations are optional: ``keda`` and ``prometheus-api`` are only needed by
autoscaled models, and ``s3-credentials`` by models stored in S3.
