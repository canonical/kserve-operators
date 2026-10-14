Integrate with observability
============================

The ``llm-cos`` deployment from :doc:`index` already connects the serving stack to the
`Canonical Observability Stack`_ (COS). This guide explains what it connected, how to
open the dashboards, and how to connect the stack to a COS you already run.

What the deployment connects
----------------------------

The ``cos`` model offers three endpoints, and the ``kserve-llm`` model consumes them:

.. list-table::
   :header-rows: 1

   * - Offer
     - Carries
   * - ``prometheus-receive-remote-write``
     - Metrics, written to Prometheus.
   * - ``grafana-dashboards``
     - The dashboards the charms ship.
   * - ``loki-logging``
     - Logs, pushed to Loki.

In the ``kserve-llm`` model, ``opentelemetry-collector-k8s`` scrapes the metrics of
``kserve-controller``, ``kserve-llmisvc``, ``keda-controller`` and ``lws-controller``,
including the vLLM workload metrics that ``kserve-llmisvc`` collects. It forwards them,
together with the charms' logs, dashboards and alert rules, to those offers.

To see the cross-model relations, look at the ``SAAS`` section of:

.. code-block:: bash

   juju status -m kserve-llm --relations

Open Grafana
------------

Get the Grafana address and the admin password:

.. code-block:: bash

   juju run -m cos grafana/leader get-admin-password

Log in as ``admin`` and open the **KServe LLMISVC - vLLM Workloads** dashboard to see
requests, token throughput, latency and KV-cache usage for every model. The
**KServe LLMISVC - Controller** dashboard covers the controller itself.

See :ref:`reference-observability` for the metrics, dashboards, alerts and logs the
stack publishes.

Allow models to autoscale
-------------------------

Autoscaled models read their metrics from Prometheus through its ``prometheus-api``
endpoint. The ``llm-cos`` deployment does not offer it, so offer it once from the
``cos`` model:

.. code-block:: bash

   juju offer cos.prometheus:prometheus-api prometheus-api

:doc:`/how-to/run-a-model/index` shows how to relate a model to it.

Use an existing COS
-------------------

If COS already runs elsewhere, connect the serving stack to it through three offers
from the COS model. Create them if your COS was not deployed with them:

.. code-block:: bash

   juju offer cos.prometheus:receive-remote-write prometheus-receive-remote-write
   juju offer cos.grafana:grafana-dashboard grafana-dashboards
   juju offer cos.loki:logging loki-logging

Then list the offers to get their URLs:

.. terminal::

   juju find-offers -m cos

   Store          URL                                           Access  Interfaces
   concierge-k8s  admin/cos.grafana-dashboards                  admin   grafana_dashboard:grafana-dashboard
   concierge-k8s  admin/cos.loki-logging                        admin   loki_push_api:logging
   concierge-k8s  admin/cos.prometheus-receive-remote-write     admin   prometheus_remote_write:receive-remote-write

Deploy the ``llm`` product module instead of the ``llm-cos`` deployment, and pass it
those URLs. A minimal root module:

.. code-block:: terraform

   module "llm_serving" {
     source = "git::https://github.com/canonical/autoscaling-model-serving//terraform/products/llm?ref=track/0.3"

     model_name           = "kserve-llm"
     create_model         = true
     enable_observability = true
     dashboards_offer     = "admin/cos.grafana-dashboards"
     logging_offer        = "admin/cos.loki-logging"
     metrics_offer        = "admin/cos.prometheus-receive-remote-write"
   }
