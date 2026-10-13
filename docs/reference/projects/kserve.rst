KServe
======

`KServe`_ is a Kubernetes platform for serving machine learning models. Charmed KServe
uses it in standard (RawDeployment) mode, without Knative or Istio, and relies on its
``LLMInferenceService`` resource to serve LLMs.

Version: |kserve_version|

Role in the stack
-----------------

``kserve-controller``
   Runs the KServe controller. It manages KServe's core resources and attaches models
   to the Envoy gateway it learns about through the ``gateway-metadata`` relation.

``kserve-llmisvc``
   Runs the controller for ``LLMInferenceService``. For each one it creates:

   * the vLLM worker Deployment ``<name>-kserve``, and with prefill/decode the
     ``<name>-kserve-prefill`` Deployment;
   * the router-scheduler that picks a worker for each request;
   * the gateway route that exposes the model under ``/<namespace>/<name>/``.

``llm-integrator``
   Renders one ``LLMInferenceService`` from its configuration.

Charmed KServe also supports classic KServe ``InferenceService`` workloads with Knative,
as part of `Charmed Kubeflow <https://canonical.com/mlops/kubeflow>`_. That mode is not covered
by this documentation.

Further reading
---------------

* `KServe documentation <https://kserve.github.io/website/>`_
* `KServe on GitHub <https://github.com/kserve/kserve>`_
