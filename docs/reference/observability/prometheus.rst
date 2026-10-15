Prometheus
==========

All metrics end up in the Prometheus of COS. In the Juju model that holds the serving
charms, ``opentelemetry-collector-k8s`` scrapes the charms and writes the samples to
Prometheus.

Metrics sources
---------------

.. list-table::
   :header-rows: 1

   * - Charm
     - Metrics
   * - ``kserve-llmisvc``
     - The ``LLMInferenceService`` controller's metrics, and the metrics of every vLLM
       worker in the cluster, collected by its ``metrics-proxy`` container.
   * - ``kserve-controller``
     - The KServe controller's metrics.
   * - ``keda-controller``
     - The KEDA operator, metrics API server and admission webhooks.

vLLM metrics
------------

vLLM workers export metrics with the ``vllm:`` prefix. ``kserve-llmisvc`` adds two
labels that identify the worker:

* ``k8s_namespace``: the namespace of the worker pod, which is the Juju model for models
  deployed with ``llm-integrator``.
* ``k8s_pod_name``: the worker pod. Pods of the ``<name>-kserve`` Deployment are decode
  (or combined) workers; pods of ``<name>-kserve-prefill`` are prefill workers.

The metrics used for autoscaling are:

.. list-table::
   :header-rows: 1

   * - Metric
     - Meaning
     - ``autoscaling-metric``
   * - ``vllm:num_requests_running``
     - Requests being processed.
     - ``num-requests`` (together with the next one)
   * - ``vllm:num_requests_waiting``
     - Requests queued for processing.
     - ``num-requests-waiting``
   * - ``vllm:kv_cache_usage_perc``
     - Fraction of the KV-cache in use, from 0 to 1.
     - ``kv-cache-usage``

For example, to see the running requests of every model:

.. code-block:: text

   sum by (k8s_namespace, k8s_pod_name) (vllm:num_requests_running)

The vLLM documentation lists `all its metrics
<https://docs.vllm.ai/en/latest/design/metrics/>`_.
