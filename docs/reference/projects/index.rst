.. _reference-projects:

KServe
======

`KServe`_ is a Kubernetes platform for serving machine learning models. The
``kserve-controller`` charm installs its custom resources, webhooks and configuration,
and runs the controller that turns ``InferenceService`` resources into running model
servers.

.. list-table::
   :header-rows: 1

   * - Project
     - Version
     - Delivered by
   * - `KServe`_
     - |kserve_version|
     - ``kserve-controller``

The charm runs KServe in one of two modes, set by its ``deployment-mode`` option:

* ``serverless`` (default): serverless serving with scale to zero, behind an Istio
  sidecar gateway and Knative Serving.
* ``rawdeployment``: plain Kubernetes Deployments, without Knative.

The charm is published on the ``0.14/stable`` channel of `Charmhub`_.

Further reading
---------------

* `KServe documentation <https://kserve.github.io/website/>`_
* `KServe on GitHub <https://github.com/kserve/kserve>`_
