Alerts
======

``kserve-llmisvc`` ships Prometheus alert rules through its ``metrics-endpoint``
integration. Prometheus in COS evaluates them and Alertmanager routes the alerts that
fire.

The rules cover two areas:

Controller
   The ``LLMInferenceService`` controller is down or restarting, fails to reconcile, or
   has no active leader.

Workloads
   vLLM workers run out of KV-cache, preempt requests, or return server errors.

The rules, with their thresholds and descriptions, are in the `prometheus_alert_rules
<https://github.com/canonical/kserve-operators/tree/main/charms/kserve-llmisvc/src/prometheus_alert_rules>`_
directory of the ``kserve-llmisvc`` charm.

To see which rules Prometheus loaded and which alerts are firing, open **Alerting** in
Grafana, or Alertmanager in the ``cos`` model.
