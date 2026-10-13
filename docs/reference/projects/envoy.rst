Envoy
=====

`Envoy Gateway`_ implements the Kubernetes Gateway API with the Envoy proxy, and
`Envoy AI Gateway`_ extends it with routing for inference workloads. Together they are
the single entry point to every model in the stack.

Versions: Envoy Gateway |envoy_gateway_version|, Envoy AI Gateway
|envoy_ai_gateway_version|

Role in the stack
-----------------

``envoy-controller-k8s``
   Runs the Envoy Gateway controller.

``envoy-ai-controller-k8s``
   Runs the AI Gateway controller and its external processor, which route requests to
   the right model pool. ``self-signed-certificates`` issues its TLS certificate.

``envoy-ingress-k8s``
   Creates the ``Gateway`` that clients connect to and exposes it through a
   ``LoadBalancer`` service. It shares the gateway's details with ``kserve-controller``
   over the ``gateway-metadata`` relation, so KServe can attach model routes to it.

Clients reach a model on ``http://<gateway-address>/<namespace>/<model-name>/``. The
gateway's status shows its address:

.. code-block:: bash

   juju status envoy-ingress-k8s

Further reading
---------------

* `envoy-ingress-k8s on Charmhub <https://charmhub.io/envoy-ingress-k8s>`_
* `Envoy Gateway documentation <https://gateway.envoyproxy.io/docs/>`_
* `Envoy AI Gateway documentation <https://aigateway.envoyproxy.io/docs/>`_
