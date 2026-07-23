# LeaderWorkerSet (LWS) Controller charm

A Juju charm that deploys the upstream
[LeaderWorkerSet](https://github.com/kubernetes-sigs/lws) controller and its
companion `LeaderWorkerSet` CRD as part of the Charmed KServe distribution.

The charm publishes a simple readiness contract on the `lws-controller`
relation so dependent charms (for example `kserve-llmisvc`) can wait for the
LWS controller to be reconciled before deploying workloads that depend on the
`leaderworkersets.leaderworkerset.x-k8s.io` API.

## Quick start

```bash
juju deploy ./lws-controller_*.charm \
  --resource lws-controller-image=registry.k8s.io/lws/lws:v0.7.0 \
  --trust -m kubeflow
```
