.. _reference-observability:

Observability
=============

``kserve-controller`` publishes metrics, alert rules and logs to the
`Canonical Observability Stack`_ (COS).

Metrics
-------

The charm's ``metrics-endpoint`` integration (``prometheus_scrape``) exposes the KServe
controller's metrics to Prometheus.

Alerts
------

The charm ships Prometheus alert rules through the same integration. They are in the
`prometheus_alert_rules
<https://github.com/canonical/kserve-operators/tree/track/0.15/charms/kserve-controller/src/prometheus_alert_rules>`_
directory:

``KubeflowServiceDown``
   A target of the charm has been down for 5 minutes (critical).

``KubeflowServiceIsNotStable``
   A unit has been unreachable at least 50% of the time over the last 10 minutes
   (warning).

Logs
----

The charm forwards the logs of its workload container to Loki through the optional
``logging`` integration (``loki_push_api``). In Loki they carry the ``juju_application``
label, for example ``{juju_application="kserve-controller"}``.
