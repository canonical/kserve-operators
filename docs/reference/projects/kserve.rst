KServe
======

`KServe`_ is a Kubernetes platform for serving machine learning models. Charmed llm-d
uses it in standard (RawDeployment) mode, without Knative or Istio, and relies on its
``LLMInferenceService`` resource to serve LLMs.

Role in the stack
-----------------

* ``kserve-controller`` runs the KServe controller. It manages KServe's core resources
  and attaches models to the Envoy gateway it learns about through the
  ``gateway-metadata`` relation.
* ``kserve-llmisvc`` runs the controller for ``LLMInferenceService``. For each one it
  creates:

  * the vLLM worker Deployment ``<name>-kserve``, and with prefill/decode the
    ``<name>-kserve-prefill`` Deployment;
  * the router-scheduler that picks a worker for each request;
  * the gateway route that exposes the model under ``/<namespace>/<name>/``.

* ``llm-integrator`` renders one ``LLMInferenceService`` from its configuration.

The same KServe charms also run classic KServe ``InferenceService`` workloads, in
standard or Knative mode, as part of
`Charmed Kubeflow <https://canonical.com/mlops/kubeflow>`_. That setup is not covered
here; see the `Charmed Kubeflow documentation`_.

Further reading
---------------

* `KServe documentation <https://kserve.github.io/website/>`_
* `KServe on GitHub <https://github.com/kserve/kserve>`_
