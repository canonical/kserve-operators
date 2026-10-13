GPU driver installation and management
======================================

Charmed KServe runs GPU models on any Kubernetes node that advertises NVIDIA GPUs as the
``nvidia.com/gpu`` resource. This page explains how that works on the reference
platform, a single Ubuntu |ubuntu_version| machine with several NVIDIA GPUs, running
`Canonical Kubernetes`_ and the `NVIDIA GPU Operator`_.

The reference platform
----------------------

The stack is tested on a single node with eight NVIDIA H100 GPUs, prepared in three
steps:

#. `concierge`_ installs `Canonical Kubernetes`_ (channel |k8s_channel|) and `Juju`_
   (channel |juju_channel|), and bootstraps a Juju controller on the cluster.
#. Helm installs the NVIDIA GPU Operator into the ``gpu-operator-resources``
   namespace.
#. Terraform deploys the serving stack, see :doc:`/how-to/deploy/index`.

Nothing GPU-specific is installed on the host by hand: there is no NVIDIA driver
package, no CUDA toolkit and no container runtime configuration. The GPU Operator
manages all of them from inside the cluster.

What the GPU Operator does
--------------------------

The GPU Operator runs a set of components as Kubernetes DaemonSets on every GPU node:

Driver
   Builds and loads the NVIDIA kernel driver from a container
   (``nvidia-driver-daemonset``), so the driver version follows the operator, not the
   host's packages.

Container toolkit
   Configures the node's container runtime so containers can access GPUs
   (``nvidia-container-toolkit-daemonset``).

Device plugin
   Advertises the GPUs to Kubernetes as the ``nvidia.com/gpu`` resource
   (``nvidia-device-plugin-daemonset``). The scheduler only places a pod that requests
   ``nvidia.com/gpu`` on a node with enough free GPUs.

GPU feature discovery
   Labels each node with the GPU model, memory and driver version, for example
   ``nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3``.

DCGM exporter
   Exports GPU utilisation, memory and temperature as Prometheus metrics
   (``nvidia-dcgm-exporter``).

Validator
   Runs a CUDA workload to check the whole chain, and reports
   ``all validations are successful`` when the node is ready
   (``nvidia-operator-validator``).

How models use GPUs
-------------------

A model gets GPUs by requesting them. With ``llm-integrator`` you set
``accelerator=nvidia-gpu`` and ``gpu-count``, and each worker pod requests that many
whole GPUs. With ``gpu-count`` greater than one, vLLM splits the model across the
worker's GPUs with tensor parallelism.

The GPUs a model needs are a product of three numbers: GPUs per worker, workers per
replica (two with prefill/decode, otherwise one), and replicas. ``llm-integrator``
checks that the cluster can fit ``min-replicas`` workers before it deploys a model.
Replicas added by autoscaling stay ``Pending`` if no node has enough free GPUs.

GPUs are not shared: each GPU belongs to one worker. Sharing a GPU between models, with
MIG, time-slicing or MPS, is out of scope for this documentation.

Checking the GPUs
-----------------

To see how many GPUs a node offers to Kubernetes, and which model they are:

.. terminal::

   kubectl get nodes -o custom-columns='NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu,PRODUCT:.metadata.labels.nvidia\.com/gpu\.product'

   NAME    GPUS   PRODUCT
   hidon   8      NVIDIA-H100-80GB-HBM3

To check that the operator finished its validation:

.. code-block:: bash

   kubectl logs -n gpu-operator-resources -l app=nvidia-operator-validator | grep "all validations are successful"

To see which GPUs a running model uses, run ``nvidia-smi`` in its worker. A model on two
GPUs under load uses both:

.. terminal::
   :scroll:

   kubectl exec -n kserve-llm mixtral-kserve-589bb9d6f9-7pmdp -c main -- nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv

   index, name, memory.used [MiB], memory.total [MiB], utilization.gpu [%]
   0, NVIDIA H100 80GB HBM3, 75697 MiB, 81559 MiB, 100 %
   1, NVIDIA H100 80GB HBM3, 75697 MiB, 81559 MiB, 100 %

Managing the GPU Operator
-------------------------

The GPU Operator is a Helm release, so it is upgraded, configured and removed with Helm:

.. code-block:: bash

   # Find the release name
   helm list -n gpu-operator-resources

   # Upgrade to the latest chart version
   helm repo update
   helm upgrade -n gpu-operator-resources <release-name> nvidia/gpu-operator

   # Remove it
   helm uninstall -n gpu-operator-resources <release-name>

Upgrading the operator can change the NVIDIA driver version, which restarts the driver
on each node and briefly removes its GPUs. Stop or drain GPU models first. The NVIDIA
documentation lists the supported drivers and Helm values, for example how to pin
``driver.version``.
