Logs
====

Logs are pushed to the Loki of COS through the ``logging`` integration.

Charm logs
   ``kserve-controller``, ``kserve-llmisvc``, ``keda-controller`` and
   ``lws-controller`` forward the logs of their workload containers. In Loki they carry
   the ``juju_application`` label, for example ``juju_application="kserve-llmisvc"``.

vLLM worker logs
   When ``kserve-llmisvc`` is related to Loki, it gives the vLLM workers the Loki push
   address in their ``LOKI_URL`` environment variable, and the workers push their logs
   there. They carry the ``app="vllm"`` label.

To browse them, open **Explore** in Grafana, choose the Loki data source and query, for
example:

.. code-block:: text

   {juju_application="kserve-llmisvc"}
   {app="vllm"}
