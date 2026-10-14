KEDA
====

`KEDA`_ (Kubernetes Event-driven Autoscaling) scales Kubernetes workloads on external
metrics. It is optional: only autoscaled models use it.

Role in the stack
-----------------

``keda-controller`` runs the KEDA operator, its external metrics API server and its
admission webhooks in a single pod, and installs KEDA's custom resources.

An ``llm-integrator`` application with ``max-replicas`` greater than ``min-replicas``
creates a KEDA ``ScaledObject`` for each of its worker Deployments. KEDA reads the
model's vLLM metric from Prometheus and drives a HorizontalPodAutoscaler from it. See
:doc:`/explanation/architecture`.

With ``max-replicas`` equal to ``min-replicas``, the default, KEDA is not used: the
charm deletes any ``ScaledObject`` it created and runs each worker with a fixed
``min-replicas`` replicas. The ``keda`` and ``prometheus-api`` relations are then not
needed.

``keda-controller`` does not create any ``ScaledObject`` itself, so you can also scale
your own workloads with it.

Further reading
---------------

* `keda-controller on Charmhub <https://charmhub.io/keda-controller>`_
* `KEDA documentation <https://keda.sh/docs/>`_
* `KEDA Prometheus scaler <https://keda.sh/docs/latest/scalers/prometheus/>`_
