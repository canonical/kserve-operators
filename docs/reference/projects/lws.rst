LeaderWorkerSet
===============

`LeaderWorkerSet`_ (LWS) is a Kubernetes API for running a group of pods as a single
unit, with one leader and several workers. It is used to serve models that are too large
for one node, by spreading each replica over several nodes.

Version: |lws_version|

Role in the stack
-----------------

``lws-controller`` runs the LeaderWorkerSet controller and installs its custom
resources. It is maintained alongside the other Charmed KServe charms, in the
`lws-controller-operator`_ repository.

``kserve-llmisvc`` requires ``lws-controller`` and waits for it to be ready, because
multi-node ``LLMInferenceService`` workloads are built on LeaderWorkerSets.

The guides in this documentation use a single node, so they do not create any
LeaderWorkerSets.

Further reading
---------------

* `lws-controller on Charmhub <https://charmhub.io/lws-controller>`_
* `lws-controller-operator on GitHub <https://github.com/canonical/lws-controller-operator>`_
* `LeaderWorkerSet documentation <https://lws.sigs.k8s.io/docs/>`_
