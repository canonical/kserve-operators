.. _home:

Charmed llm-d documentation
===========================

Charmed llm-d serves open-weight large language models (LLMs) on your own Kubernetes
cluster and GPUs. It deploys `KServe`_ and the `llm-d`_ inference scheduler with
`Juju`_: models run on `vLLM`_ behind an `Envoy Gateway`_, scale with `KEDA`_ on live
inference metrics, and report metrics, logs and dashboards to the
`Canonical Observability Stack`_ (COS).

The whole stack, including COS, is deployed with a single Terraform module. A model is
then one charm and a few configuration options away: point it at a Hugging Face or S3
model, choose CPU or NVIDIA GPUs, and set how far it may scale.

Charmed llm-d is for platform and ML engineers who want to run LLM inference on
infrastructure they control, with the reproducibility of Juju and Terraform.

.. toctree::
   :hidden:
   :maxdepth: 2

   How to <how-to/index>
   explanation/index
   reference/index
   contribute/index

In this documentation
---------------------

.. grid:: 1 1 2 2

   .. grid-item-card:: How-to guides
      :link: /how-to/index
      :link-type: doc

      Deploy the stack with Terraform, connect it to COS and serve your first model.

   .. grid-item-card:: Explanation
      :link: /explanation/index
      :link-type: doc

      Understand the architecture, how autoscaling works and how GPUs are managed.

.. grid:: 1 1 2 2

   .. grid-item-card:: Reference
      :link: /reference/index
      :link-type: doc

      Versions of the underlying projects, the charms, and the observability signals
      the stack publishes.

   .. grid-item-card:: Contribute
      :link: /contribute/index
      :link-type: doc

      Report bugs, improve the charms or this documentation.

Project and community
---------------------

Charmed llm-d is an open-source project that values its community. Contributions,
suggestions, fixes and constructive feedback are welcome from everyone.

* Read our `Code of conduct`_.
* `Contribute <https://github.com/canonical/kserve-operators>`_ and
  `report bugs <https://github.com/canonical/kserve-operators/issues>`_.
* Talk to us on `Matrix`_.
