KEDA
====

`KEDA`_ (Kubernetes Event-driven Autoscaling) scales Kubernetes workloads on external
metrics. It is optional: only autoscaled models use it.

Version: |keda_version|

Role in the stack
-----------------

``keda-controller`` runs the KEDA operator, its external metrics API server and its
admission webhooks in a single pod, and installs KEDA's custom resources.

An ``llm-integrator`` application with ``max-replicas`` greater than ``min-replicas``
creates a KEDA ``ScaledObject`` for each of its worker Deployments. KEDA reads the
model's vLLM metric from Prometheus and drives a HorizontalPodAutoscaler from it. See
:doc:`/explanation/architecture`.

``keda-controller`` does not create any ``ScaledObject`` itself, so you can also scale
your own workloads with it.

Further reading
---------------

* `keda-controller on Charmhub <https://charmhub.io/keda-controller>`_
* `KEDA documentation <https://keda.sh/docs/>`_
* `KEDA Prometheus scaler <https://keda.sh/docs/latest/scalers/prometheus/>`_
