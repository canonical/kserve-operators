.. _deploy-the-stack:

Deploy the stack with Terraform
===============================

This guide prepares a machine and deploys the complete Charmed KServe stack, together
with the `Canonical Observability Stack`_ (COS), using the `autoscaling-model-serving`_
Terraform solution.

When you finish, you have two Juju models:

``kserve-llm``
   The serving stack: KServe, the Envoy gateway, LeaderWorkerSet, KEDA and an
   OpenTelemetry collector.

``cos``
   COS Lite: Prometheus, Grafana, Loki, Alertmanager and Traefik.

.. toctree::
   :hidden:

   integrate-observability

Requirements
------------

* A machine running Ubuntu |ubuntu_version| with ``sudo`` access, at least 8 CPU cores,
  32 GB of memory and 100 GB of free disk. Models need more: plan for the size of the
  models you want to serve.
* For GPU models, one or more NVIDIA GPUs on that machine.
* A free IP address on the machine's network for the gateway's load balancer.

The guide uses a single node. If you already have a Kubernetes cluster with a Juju
controller, skip to `Deploy the stack`_.

Install the tools
-----------------

Install `concierge`_, `Juju`_ and `Terraform`_ as snaps:

.. code-block:: bash

   sudo snap install concierge --classic
   sudo snap install juju --channel 3.6/stable --classic
   sudo snap install terraform --classic

For GPU machines, also install Helm from its APT repository, which the NVIDIA GPU
Operator needs:

.. code-block:: bash

   sudo apt-get install -y curl gpg apt-transport-https
   curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey \
     | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
   echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" \
     | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
   sudo apt-get update
   sudo apt-get install -y helm

Create the cluster and the Juju controller
------------------------------------------

Concierge installs `Canonical Kubernetes`_ and bootstraps a Juju controller on it in one
step. Create a ``concierge.yaml`` file, and replace ``10.64.140.43/32`` with the free IP
address for the load balancer:

.. code-block:: yaml

   juju:
     channel: 3.6/stable

   providers:
     k8s:
       enable: true
       bootstrap: true
       channel: 1.33-classic/stable
       features:
         local-storage:
         load-balancer:
           enabled: true
           l2-mode: true
           cidrs: 10.64.140.43/32
       bootstrap-constraints:
         root-disk: 50G

Then run concierge from the directory that contains the file:

.. code-block:: bash

   sudo concierge prepare

When it finishes, ``juju controllers`` lists the new controller and ``kubectl get
nodes`` shows a ``Ready`` node.

Install the NVIDIA GPU Operator
-------------------------------

Skip this step on machines without GPUs.

The `NVIDIA GPU Operator`_ installs the NVIDIA driver and makes the GPUs available to
Kubernetes. See :doc:`/explanation/gpu-drivers` for what it does.

.. code-block:: bash

   helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
   helm repo update
   helm install --generate-name -n gpu-operator-resources --create-namespace nvidia/gpu-operator

The operator builds the driver on first install, which takes several minutes. Wait
until its validator reports success:

.. code-block:: bash

   kubectl logs -n gpu-operator-resources -l app=nvidia-operator-validator | grep "all validations are successful"

All the operator's pods are then running, and the CUDA validator has completed:

.. terminal::

   kubectl get pods -n gpu-operator-resources | grep nvidia

   nvidia-container-toolkit-daemonset-rffn5    1/1     Running     0          133m
   nvidia-cuda-validator-qxj8s                 0/1     Completed   0          130m
   nvidia-dcgm-exporter-hhjmv                  1/1     Running     0          133m
   nvidia-device-plugin-daemonset-bwgmm        1/1     Running     0          133m
   nvidia-driver-daemonset-ztz8n               1/1     Running     0          133m
   nvidia-mig-manager-x4dlj                    1/1     Running     0          130m
   nvidia-operator-validator-6cpf5             1/1     Running     0          133m

Check that the node advertises its GPUs:

.. terminal::

   kubectl get nodes -o custom-columns='NAME:.metadata.name,GPUS:.status.allocatable.nvidia\.com/gpu'

   NAME    GPUS
   hidon   8

Deploy the stack
----------------

Clone the Terraform solution and switch to its |solution_track| branch:

.. code-block:: bash

   git clone https://github.com/canonical/autoscaling-model-serving.git
   cd autoscaling-model-serving
   git checkout track/0.3

The ``llm-cos`` deployment deploys COS Lite in a ``cos`` model and the serving stack in
a ``kserve-llm`` model, and connects them. Apply it:

.. code-block:: bash

   cd terraform/deployments/llm-cos
   terraform init
   terraform apply

To deploy the serving stack into a model you already have, pass its UUID:

.. code-block:: bash

   terraform apply -var model_uuid="$(juju show-model my-model --format json | jq -r '.[]."model-uuid"')"

The deployment's README lists all its inputs, for example to reuse an existing COS
model.

Check the deployment
--------------------

The charms take several minutes to settle. Watch them until every application is
``active``:

.. terminal::
   :scroll:

   juju status -m kserve-llm

   Model       Controller     Cloud/Region  Version  SLA          Timestamp
   kserve-llm  concierge-k8s  k8s           3.6.28   unsupported  08:33:38Z

   SAAS                             Status  Store  URL
   grafana-dashboards               active  local  admin/cos.grafana-dashboards
   loki-logging                     active  local  admin/cos.loki-logging
   prometheus-receive-remote-write  active  local  admin/cos.prometheus-receive-remote-write

   App                          Version  Status  Scale  Charm                        Channel        Rev  Address         Exposed  Message
   envoy-ai-controller-k8s      v0.6.0   active      1  envoy-ai-controller-k8s      latest/edge      8  10.152.183.179  no
   envoy-controller-k8s         v1.7.0   active      1  envoy-controller-k8s         latest/edge      9  10.152.183.65   no
   envoy-ingress-k8s                     active      1  envoy-ingress-k8s            latest/edge      1  10.152.183.70   no       Serving at 10.64.140.40
   keda-controller                       active      1  keda-controller              latest/edge     12  10.152.183.213  no
   kserve-controller                     active      1  kserve-controller            latest/edge   1538  10.152.183.141  no
   kserve-llmisvc                        active      1  kserve-llmisvc               latest/edge    121  10.152.183.99   no
   lws-controller                        active      1  lws-controller               latest/edge    100  10.152.183.35   no
   opentelemetry-collector-k8s  0.130.1  active      1  opentelemetry-collector-k8s  2/stable       210  10.152.183.17   no
   self-signed-certificates              active      1  self-signed-certificates     latest/stable  264  10.152.183.140  no

   Unit                            Workload  Agent  Address     Ports  Message
   envoy-ai-controller-k8s/0*      active    idle   10.1.0.120
   envoy-controller-k8s/0*         active    idle   10.1.0.119
   envoy-ingress-k8s/0*            active    idle   10.1.0.200         Serving at 10.64.140.40
   keda-controller/0*              active    idle   10.1.0.161
   kserve-controller/0*            active    idle   10.1.0.236
   kserve-llmisvc/0*               active    idle   10.1.0.27
   lws-controller/0*               active    idle   10.1.0.78
   opentelemetry-collector-k8s/0*  active    idle   10.1.0.206
   self-signed-certificates/0*     active    idle   10.1.0.163

The three ``SAAS`` entries are the cross-model connections to COS. Check the ``cos``
model the same way:

.. code-block:: bash

   juju status -m cos

In the ``kserve-llm`` model, ``envoy-ingress-k8s`` reports the gateway address, here
``10.64.140.40``. Models are served on that address. To read it from a script:

.. terminal::

   kubectl get gateway -n kserve-llm envoy-ingress-k8s -o jsonpath='{.status.addresses[0].value}'

   10.64.140.40

Next steps
----------

* :doc:`integrate-observability` explains what the deployment connected to COS and how
  to open Grafana.
* :doc:`/how-to/run-a-model/index` serves your first model.
