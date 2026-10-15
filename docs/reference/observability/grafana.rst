Grafana
=======

``kserve-llmisvc`` ships Grafana dashboards through its ``grafana-dashboard``
integration. They appear in the Grafana of COS once the stack is connected to it.

.. list-table::
   :header-rows: 1

   * - Dashboard
     - Shows
   * - KServe LLMISVC - vLLM Workloads
     - Per model and worker: running and waiting requests, token throughput, latency
       such as time to first token, and KV-cache and prefix-cache usage.
   * - KServe LLMISVC - Controller
     - Health and reconciliation activity of the ``LLMInferenceService`` controller.

The dashboard definitions are in the `grafana_dashboards
<https://github.com/canonical/kserve-operators/tree/main/charms/kserve-llmisvc/src/grafana_dashboards>`_
directory of the ``kserve-llmisvc`` charm.

To log in to Grafana, get its address and admin password:

.. code-block:: bash

   juju run -m cos grafana/leader get-admin-password
