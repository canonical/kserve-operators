.. _run-a-model:

Run a model
===========

This guide serves models with the ``llm-integrator`` charm. Each ``llm-integrator``
application serves one model: it renders KServe's ``LLMInferenceService`` resource from
its configuration and keeps it in sync. You do not write any YAML.

.. toctree::
   :hidden:

   custom-llminferenceservice

Requirements
------------

* The stack deployed as in :doc:`/how-to/deploy/index`. The commands assume the
  serving stack is in the ``kserve-llm`` model.
* For GPU models, the NVIDIA GPU Operator installed and GPUs advertised by the node.

Store the gateway address for the requests below:

.. code-block:: bash

   GW=$(kubectl get gateway -n kserve-llm envoy-ingress-k8s -o jsonpath='{.status.addresses[0].value}')

Serve a model on CPU
--------------------

Start with a small public model from Hugging Face. CPU is the default accelerator:

.. code-block:: bash

   juju deploy -m kserve-llm llm-integrator pythia --channel latest/edge --trust \
     --config model-uri=hf://EleutherAI/pythia-70m
   juju integrate -m kserve-llm pythia:kserve-llmisvc kserve-llmisvc:kserve-llmisvc

The application name, ``pythia``, becomes the name of the ``LLMInferenceService``. The
charm downloads the model, starts vLLM and reports its progress, for example
``LLMInferenceService pythia is starting: downloading the model``. Wait until it is
``active``:

.. terminal::

   juju status -m kserve-llm pythia

   App     Version  Status  Scale  Charm           Channel      Rev  Address         Exposed  Message
   pythia           active      1  llm-integrator  latest/edge  103  10.152.183.180  no

   Unit       Workload  Agent  Address    Ports  Message
   pythia/0*  active    idle   10.1.0.12

Send a request
--------------

vLLM serves the OpenAI API under ``/<namespace>/<application>/``. The served model name
defaults to the part of the URI after ``hf://``:

.. code-block:: bash

   curl -s "http://$GW/kserve-llm/pythia/v1/completions" \
     -H 'Content-Type: application/json' \
     -d '{"model": "EleutherAI/pythia-70m", "prompt": "Kubernetes is", "max_tokens": 30}'

Chat models also answer on ``/v1/chat/completions``. Set the ``model-name``
configuration option to expose a model under a different name.

Serve a gated model on GPUs
---------------------------

This example serves Mixtral-8x7B-Instruct, which is gated on Hugging Face and too large
for a single 80 GB GPU. It needs a Hugging Face access token from an account that has
accepted the model's terms.

Store the token in a Juju secret. ``read -s`` keeps it out of your shell history:

.. code-block:: bash

   read -s HF_TOKEN
   SECRET=$(juju add-secret -m kserve-llm hf-token token="$HF_TOKEN"); unset HF_TOKEN

Deploy the model on two GPUs, with the secret already configured:

.. code-block:: bash

   juju deploy -m kserve-llm llm-integrator mixtral --channel latest/edge --trust \
     --config model-uri=hf://mistralai/Mixtral-8x7B-Instruct-v0.1 \
     --config accelerator=nvidia-gpu \
     --config gpu-count=2 \
     --config cpu-request=4 --config cpu-limit=8 \
     --config memory-request=32Gi --config memory-limit=96Gi \
     --config hf-token-secret="$SECRET"

Grant the secret to the application, then relate it:

.. code-block:: bash

   juju grant-secret -m kserve-llm hf-token mixtral
   juju integrate -m kserve-llm mixtral:kserve-llmisvc kserve-llmisvc:kserve-llmisvc

Until the secret is granted the charm is ``blocked`` and deploys nothing, so the model
is never started without its token.

With ``gpu-count=2`` each worker gets two GPUs and vLLM splits the model across them with
tensor parallelism. The download is about 90 GB, so the first start takes a while. The
charm reports the progress:

.. terminal::

   juju status -m kserve-llm mixtral

   Unit        Workload  Agent  Address     Ports  Message
   mixtral/0*  waiting   idle   10.1.0.239         LLMInferenceService mixtral is starting: downloading the model

When it is ``active``, the worker and its router-scheduler run in the model's
namespace:

.. terminal::

   kubectl get pods -n kserve-llm | grep mixtral

   mixtral-0                                        1/1     Running   0          89m
   mixtral-kserve-589bb9d6f9-7pmdp                  1/1     Running   0          89m
   mixtral-kserve-router-scheduler-6cd78c9f7d-b7894 1/1     Running   0          89m

``mixtral-0`` is the charm itself. Ask the model a question:

.. terminal::
   :scroll:

   curl -s "http://$GW/kserve-llm/mixtral/v1/completions" \
     -H 'Content-Type: application/json' \
     -d '{"model": "mistralai/Mixtral-8x7B-Instruct-v0.1",
          "prompt": "Explain tensor parallelism to a developer in exactly two sentences.",
          "max_tokens": 80, "temperature": 0.3}' | jq -r '.choices[0].text'

   Tensor parallelism is a technique for distributing the computation of large tensors across multiple GPUs, allowing for efficient training of large models that would not fit on a single GPU. It involves splitting the tensor along one or more dimensions and distributing the resulting chunks across the GPUs, with each GPU computing its assigned chunk in parallel.

Serve a model from S3
---------------------

To serve a model you have stored in an S3 bucket, give the charm the bucket's
credentials through the `s3-integrator <https://charmhub.io/s3-integrator>`_ charm:

.. code-block:: bash

   juju deploy -m kserve-llm s3-integrator --channel 2/edge \
     --config endpoint=https://s3.eu-central-1.amazonaws.com \
     --config region=eu-central-1 \
     --config bucket=my-models

   read -s AWS_SECRET_ACCESS_KEY
   S3_SECRET=$(juju add-secret -m kserve-llm s3-creds \
     access-key="$AWS_ACCESS_KEY_ID" secret-key="$AWS_SECRET_ACCESS_KEY"); unset AWS_SECRET_ACCESS_KEY
   juju grant-secret -m kserve-llm s3-creds s3-integrator
   juju config -m kserve-llm s3-integrator credentials="$S3_SECRET"

Then point ``llm-integrator`` at the model's folder in the bucket:

.. code-block:: bash

   juju deploy -m kserve-llm llm-integrator my-model --channel latest/edge --trust \
     --config model-uri=s3://my-models/pythia-70m \
     --config model-name=EleutherAI/pythia-70m
   juju integrate -m kserve-llm my-model:kserve-llmisvc kserve-llmisvc:kserve-llmisvc
   juju integrate -m kserve-llm my-model:s3-credentials s3-integrator:s3-credentials

Staging large models in S3 also speeds up new replicas, which no longer download from
Hugging Face.

Autoscale a model
-----------------

A model autoscales when ``max-replicas`` is greater than ``min-replicas``. Autoscaling
needs two more relations: to ``keda-controller``, and to the Prometheus that holds the
vLLM metrics. See :doc:`/explanation/architecture` for how it works.

If you have not done so yet, offer the Prometheus API from the ``cos`` model:

.. code-block:: bash

   juju offer cos.prometheus:prometheus-api prometheus-api

Let the Mixtral model scale between one and two replicas, adding a replica when there
are more than four running and waiting requests per replica:

.. code-block:: bash

   juju config -m kserve-llm mixtral max-replicas=2 autoscaling-target=4
   juju integrate -m kserve-llm mixtral:keda keda-controller:keda
   juju integrate -m kserve-llm mixtral:prometheus-api admin/cos.prometheus-api

When autoscaling works, the charm reports ``Autoscaling 1-2 replicas``. While a new
replica starts next to a serving one, the status shows its progress, for example
``Autoscaling 1-2 replicas; 1/2 workers ready, downloading the model``.

To watch KEDA's decisions:

.. terminal::
   :scroll:

   kubectl get scaledobject,hpa -n kserve-llm

   NAME                                  SCALETARGETKIND      SCALETARGETNAME   MIN   MAX   READY   ACTIVE   FALLBACK   PAUSED    TRIGGERS     AUTHENTICATIONS   AGE
   scaledobject.keda.sh/mixtral-kserve   apps/v1.Deployment   mixtral-kserve    1     2     True    False    False      Unknown   prometheus                     73m

   NAME                                                          REFERENCE                   TARGETS      MINPODS   MAXPODS   REPLICAS   AGE
   horizontalpodautoscaler.autoscaling/keda-hpa-mixtral-kserve   Deployment/mixtral-kserve   12/4 (avg)   1         2         2          74m

Here twelve requests per replica are running or waiting, three times the target, so the
HPA asks for the maximum of two replicas. The new worker downloads the model while the
first one keeps serving:

.. terminal::

   kubectl get pods -n kserve-llm -l app.kubernetes.io/name=mixtral,kserve.io/component=workload

   NAME                              READY   STATUS     RESTARTS   AGE
   mixtral-kserve-55578f79f9-4z6rr   1/1     Running    0          74m
   mixtral-kserve-55578f79f9-d9wdm   0/1     Init:0/1   0          54s

The ``autoscaling-metric`` option chooses what to scale on: ``num-requests`` (running
plus waiting requests, the default), ``num-requests-waiting`` or ``kv-cache-usage``.
Leave ``autoscaling-target`` at ``0`` to use the metric's default target.

To stop autoscaling, set ``max-replicas`` back to ``min-replicas``. The model then runs
that fixed number of replicas, and KEDA and Prometheus are no longer needed.

.. note::

   Autoscaling does not yet support Prometheus served over TLS, or application names
   longer than 39 characters (31 with prefill/decode enabled).

Split prefill and decode
------------------------

To run prompt processing and token generation on separate workers:

.. code-block:: bash

   juju config -m kserve-llm mixtral enable-prefill-decode=true

The model now has a decode worker and a prefill worker, and each runs on its own
``gpu-count`` GPUs. When autoscaling, each worker scales independently with the same
settings.

.. terminal::

   kubectl get pods -n kserve-llm | grep mixtral

   mixtral-0                                          1/1     Running   0          5m16s
   mixtral-kserve-7c5d9b855f-r4t5g                    2/2     Running   0          4m33s
   mixtral-kserve-prefill-7dc786d7b7-ljvzh            1/1     Running   0          4m33s
   mixtral-kserve-router-scheduler-d5d59b57f-2n4r5    1/1     Running   0          4m26s

The decode worker has a second container, the routing sidecar that hands each request
from the prefill worker to the decode worker.

Remove a model
--------------

Removing the application deletes the ``LLMInferenceService``, its ``ScaledObjects`` and
the secrets the charm created:

.. code-block:: bash

   juju remove-application -m kserve-llm mixtral

See also
--------

* The `llm-integrator configuration <https://charmhub.io/llm-integrator/configurations>`_
  lists every option, for example worker resources, ``max-model-len`` and extra vLLM
  arguments.
* :doc:`custom-llminferenceservice` serves a model from your own manifest, for setups
  the charm does not cover.
