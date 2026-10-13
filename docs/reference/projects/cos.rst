Canonical Observability Stack
=============================

The `Canonical Observability Stack`_ (COS) collects metrics, logs and alerts, and shows
them in Grafana. The stack uses COS Lite: Prometheus, Loki, Grafana, Alertmanager and
Traefik.

Channel: |cos_channel|

Role in the stack
-----------------

COS runs in its own ``cos`` Juju model. The serving model connects to it through
cross-model offers, and ``opentelemetry-collector-k8s`` in the serving model forwards
all signals to them. See :doc:`/how-to/deploy/integrate-observability`.

Prometheus in COS also feeds autoscaling: autoscaled models read their metric from it
through the ``prometheus-api`` endpoint.

Further reading
---------------

* `COS documentation <https://documentation.ubuntu.com/observability/>`_
* `opentelemetry-collector-k8s on Charmhub <https://charmhub.io/opentelemetry-collector-k8s>`_
