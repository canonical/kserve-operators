Run a custom LLMInferenceService
================================

``llm-integrator`` covers the common cases. For anything it does not expose, such as
custom routing, scaling on your own query, or other KServe features, write the
``LLMInferenceService`` yourself. The serving stack runs it just the same; you then own
its lifecycle and its scaling.

This guide serves Mixtral-8x7B-Instruct on two GPUs from a manifest, and autoscales it
with a hand-written KEDA ``ScaledObject``.

.. note::

   Do not use the name of an ``llm-integrator`` application for your own resources in
   the same namespace. The charm owns the ``LLMInferenceService`` and ``ScaledObjects``
   named after its application.

Requirements
------------

* The stack deployed as in :doc:`/how-to/deploy/index`, with the NVIDIA GPU Operator.
* A Hugging Face access token from an account that has accepted the model's terms.

Create the model
----------------

Store the token in a Kubernetes Secret, so it never appears in the manifest:

.. code-block:: bash

   read -s HF_TOKEN
   kubectl create secret generic hf-token -n default --from-literal=HF_TOKEN="$HF_TOKEN"
   unset HF_TOKEN

Save the ``LLMInferenceService`` as ``mixtral.yaml``. The images are the ones
``llm-integrator`` uses by default:

.. parsed-literal::

   apiVersion: serving.kserve.io/v1alpha2
   kind: LLMInferenceService
   metadata:
     name: mixtral
     namespace: default
   spec:
     model:
       uri: hf://mistralai/Mixtral-8x7B-Instruct-v0.1
       name: mistralai/Mixtral-8x7B-Instruct-v0.1
     storageInitializer:
       enabled: false
     router:
       scheduler: {}
       route: {}
     template:
       volumes:
         - name: kserve-pvc-source
           emptyDir: {}
       initContainers:
         - name: storage-initializer
           image: |storage_initializer_image|
           args: ["hf://mistralai/Mixtral-8x7B-Instruct-v0.1", "/mnt/models"]
           env:
             - name: HF_TOKEN
               valueFrom:
                 secretKeyRef: {name: hf-token, key: HF_TOKEN}
           volumeMounts:
             - {name: kserve-pvc-source, mountPath: /mnt/models}
       containers:
         - name: main
           image: |vllm_gpu_image|
           args: ["--tensor-parallel-size", "2"]
           volumeMounts:
             - {name: kserve-pvc-source, mountPath: /mnt/models, readOnly: true}
           resources:
             requests: {cpu: "4", memory: 32Gi, nvidia.com/gpu: "2"}
             limits: {cpu: "8", memory: 96Gi, nvidia.com/gpu: "2"}

A few choices in this manifest are deliberate:

* KServe's own storage initializer is disabled and replaced by an init container
  without its 1 GiB memory limit, which is too small for multi-gigabyte models.
* ``--tensor-parallel-size 2`` and ``nvidia.com/gpu: "2"`` split the model across two
  GPUs.
* ``replicas`` is not set, so that KEDA can own the replica count. Set it to run a fixed
  number of replicas instead.

Apply it and wait until it is ready:

.. code-block:: bash

   kubectl apply -f mixtral.yaml
   kubectl get llminferenceservice -n default mixtral -w

After the download and model load, KServe reports it ready, with the vLLM worker and the
router-scheduler running:

.. terminal::

   kubectl get llminferenceservice,pods -n default

   NAME                                              URL   READY   REASON   AGE
   llminferenceservice.serving.kserve.io/mixtral           True             89m

   NAME                                                   READY   STATUS    RESTARTS   AGE
   pod/mixtral-kserve-589bb9d6f9-7pmdp                    1/1     Running   0          89m
   pod/mixtral-kserve-router-scheduler-6cd78c9f7d-b7894   1/1     Running   0          89m

Then send it a request through the gateway:

.. terminal::

   GW=$(kubectl get gateway -n kserve-llm envoy-ingress-k8s -o jsonpath='{.status.addresses[0].value}')
   curl -s "http://$GW/default/mixtral/v1/chat/completions" \
     -H 'Content-Type: application/json' \
     -d '{"model": "mistralai/Mixtral-8x7B-Instruct-v0.1",
          "messages": [{"role": "user", "content": "Say hi in 5 words."}], "max_tokens": 20}' \
     | jq -r '.choices[0].message.content'

   Hello, greetings, warmest salutations!

Autoscale it with KEDA
----------------------

KServe names the worker Deployment ``<name>-kserve``. Save this ``ScaledObject`` as
``mixtral-scaling.yaml``. It reads the running and waiting requests of the model's pods
from Prometheus in the ``cos`` model, and keeps them at about four per replica:

.. code-block:: yaml

   apiVersion: keda.sh/v1alpha1
   kind: ScaledObject
   metadata:
     name: mixtral-kserve
     namespace: default
   spec:
     scaleTargetRef:
       name: mixtral-kserve
     minReplicaCount: 1
     maxReplicaCount: 2
     pollingInterval: 15
     advanced:
       restoreToOriginalReplicaCount: true
       horizontalPodAutoscalerConfig:
         behavior:
           scaleDown:
             stabilizationWindowSeconds: 300
     triggers:
       - type: prometheus
         metadata:
           serverAddress: http://prometheus.cos.svc.cluster.local:9090
           query: sum({__name__=~"vllm:num_requests_running|vllm:num_requests_waiting",k8s_namespace="default",k8s_pod_name=~"mixtral-kserve-[^-]+-[^-]+"})
           threshold: "4"

Apply it and watch the HorizontalPodAutoscaler KEDA creates:

.. terminal::
   :scroll:

   kubectl apply -f mixtral-scaling.yaml
   kubectl get scaledobject,hpa -n default

   NAME                                  SCALETARGETKIND      SCALETARGETNAME   MIN   MAX   READY   ACTIVE    FALLBACK   PAUSED    TRIGGERS     AUTHENTICATIONS   AGE
   scaledobject.keda.sh/mixtral-kserve   apps/v1.Deployment   mixtral-kserve    1     2     True    Unknown   False      Unknown   prometheus                     7s

   NAME                                                          REFERENCE                   TARGETS             MINPODS   MAXPODS   REPLICAS   AGE
   horizontalpodautoscaler.autoscaling/keda-hpa-mixtral-kserve   Deployment/mixtral-kserve   <unknown>/4 (avg)   1         2         0          6s

The target reads ``<unknown>`` for the first polling interval, then shows the current
requests per replica.

KEDA supports many other triggers, such as queue length or a schedule. See the `KEDA`_
documentation for their options.

Remove the model
----------------

.. code-block:: bash

   kubectl delete -f mixtral-scaling.yaml -f mixtral.yaml
   kubectl delete secret -n default hf-token
