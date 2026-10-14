Architecture
============

Charmed llm-d is a set of Juju charms that together turn a Kubernetes cluster into an
LLM inference platform. This page explains what each part does, how a request reaches
a model, and how the stack scales and reports what it is doing.

Components
----------

.. mermaid::

   flowchart LR
       client([Client]) -->|HTTP, OpenAI API| gateway[Envoy gateway]
       gateway --> scheduler[Router-scheduler]
       scheduler --> workers[vLLM workers]
       workers -->|metrics| collector[OpenTelemetry collector]
       collector -->|remote write| prometheus[(Prometheus in COS)]
       autoscaler[KEDA] -->|queries| prometheus
       autoscaler -->|scales| workers

The stack is split into three groups of charms.

**Serving control plane**:

``kserve-controller``
   The KServe controller, which serves ``InferenceService`` resources. For Charmed
   llm-d it is deployed with ``deployment-mode=standard``, the only mode that accepts
   a Gateway API gateway; it learns which one to attach models to through the
   ``gateway-metadata`` relation. ``LLMInferenceServices`` themselves have no deployment
   mode: they always run as plain Deployments.

``kserve-llmisvc``
   The controller for KServe's ``LLMInferenceService`` resource. It turns each
   ``LLMInferenceService`` into vLLM worker Deployments, a router-scheduler, and the
   gateway routes that expose them. It also collects the vLLM metrics, ships the
   alert rules and Grafana dashboards, and forwards logs.

``llm-integrator``
   One model per application. It renders an ``LLMInferenceService`` from a few
   configuration options and, when asked to, creates the KEDA ``ScaledObject`` that
   autoscales it. You deploy one ``llm-integrator`` application for each model.

**Dependencies**, platform services the serving control plane relies on:

``lws-controller``
   `LeaderWorkerSet`_, which ``kserve-llmisvc`` needs for multi-node workloads.

``keda-controller``
   `KEDA`_, the event-driven autoscaler. It is only used by models that autoscale.

``envoy-controller-k8s``, ``envoy-ai-controller-k8s``, ``envoy-ingress-k8s``
   `Envoy Gateway`_ and `Envoy AI Gateway`_. Together they provide the single entry
   point for all models and the inference-aware routing to them.
   ``self-signed-certificates`` issues the TLS certificate the AI gateway needs.

**Observability**: ``opentelemetry-collector-k8s`` runs next to the serving charms and
forwards their metrics, logs and dashboards to a `Canonical Observability Stack`_ (COS)
running in its own Juju model.

The `autoscaling-model-serving`_ Terraform solution deploys and connects all of the
above. Models are not part of the solution: you add them afterwards with
``llm-integrator``, or with your own ``LLMInferenceService`` resources.

How a request reaches a model
-----------------------------

Every model is served by vLLM, which exposes an OpenAI-compatible API. Requests flow as
follows:

#. The client calls the Envoy gateway on
   ``http://<gateway-address>/<namespace>/<model-name>/v1/...``. With
   ``llm-integrator`` the namespace is the Juju model and the model name is the
   application name.
#. The gateway matches the route ``kserve-llmisvc`` created for the model and hands
   the request to the model's router-scheduler (the `llm-d`_ inference scheduler).
#. The scheduler picks the vLLM worker best placed to answer, for example the one
   with the most free KV-cache, and the worker generates the response.

.. _prefill-decode:

Prefill and decode
~~~~~~~~~~~~~~~~~~

LLM inference has two phases. *Prefill* processes the whole prompt in parallel to build
its KV-cache, and is compute-bound. *Decode* then generates the answer one token at a
time and is limited by memory bandwidth. On a shared worker a long prompt can stall the
token stream of every other request.

With prefill/decode enabled, ``llm-integrator`` adds a separate prefill worker next to
the decode worker, and each pool can be sized and scaled on its own. A request then
flows as follows:

#. The router-scheduler picks a decode worker and a prefill worker for the request,
   and sends it to the decode worker.
#. In the decode worker, a routing sidecar container receives the request first. vLLM
   listens on another port behind it.
#. The sidecar sends the prompt to the chosen prefill worker, then forwards the request
   to its own vLLM, which generates the answer.

The current release does not transfer the KV-cache between the workers: the decode
worker recomputes the prompt.

How autoscaling works
---------------------

A model autoscales when its ``llm-integrator`` application has ``max-replicas`` greater
than ``min-replicas``. When they are equal the model runs a fixed number of replicas and
KEDA is not needed.

#. Each vLLM worker exports metrics such as the number of running and waiting
   requests and its KV-cache usage.
#. ``kserve-llmisvc`` collects these metrics, and the OpenTelemetry collector sends
   them to Prometheus in COS.
#. ``llm-integrator`` creates one KEDA ``ScaledObject`` per worker Deployment. Its
   query reads the chosen metric for that worker's pods from the Prometheus that
   ``llm-integrator`` is related to through ``prometheus-api``.
#. KEDA turns each ``ScaledObject`` into a Kubernetes HorizontalPodAutoscaler. The
   HPA adds replicas while the metric per replica is above the target, and removes
   them after it has stayed below for the scale-down delay.

Missing or unready dependencies never stop a model from serving. If KEDA or
Prometheus go away, the model keeps its current replicas and the charm reports what is
missing in its status.

A new replica must download the model before it can serve. For large models this takes
minutes, so the scale-down delay defaults to five minutes to avoid dropping replicas on
short dips in load.

Observability
-------------

All signals end up in COS:

* **Metrics**: the controller and the vLLM workers are scraped and written to
  Prometheus.
* **Dashboards**: ``kserve-llmisvc`` ships Grafana dashboards for the controller and
  for the vLLM workloads.
* **Alerts**: ``kserve-llmisvc`` ships Prometheus alert rules, for example for a full
  KV-cache or server errors.
* **Logs**: the charms forward their own logs, and the vLLM workers push their logs to
  Loki directly.

See :ref:`reference-observability` for the details.
