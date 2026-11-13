# gpu-ib-cluster actions

Composite actions that bring up, and tear down, a real GPU cluster connected
via InfiniBand on [Testflinger](https://testflinger.canonical.com), used by
[`gpu_ib_integration.yaml`](../../workflows/gpu_ib_integration.yaml) to run the
GPU bundle integration tests (`tox -e bundle-integration-gpu`).

## Topology

Cluster spec [`tor-A100_x2_IB`](testflinger/cluster-specs/tor-A100_x2_IB.yaml):

| Role | Testflinger queue | Used for |
|---|---|---|
| `worker-gpu` | cortez, blanka | Canonical K8s (snap) nodes (8x A100 each, InfiniBand `ibp18s0`); the first one bootstraps the cluster, the others join it |
| GH runner | — | Orchestrator: juju/kubectl/helm client, sshuttle tunnels (K8s API, LB CIDR), tox |

The K8s cluster runs the NVIDIA GPU Operator and the NVIDIA Network Operator,
which provides the RDMA shared device plugin (`rdma/rdma_shared_device_ib`). A
Juju controller (`vllm`) is bootstrapped inside K8s; the tests run
against it.

[`tor-k8s-smoke`](testflinger/cluster-specs/tor-k8s-smoke.yaml) (workers
cortez and buzzsaw, no InfiniBand link) validates the setup with
`up-to: gpu-operator`. The workflow bootstraps the `vllm` controller on every
cluster (unless `up-to: k8s`) and runs the tests only on `tor-A100_x2_IB`. The `up-to` input (`k8s`,
`gpu-operator`, `network-operator`) stops the operator stages after that one;
the tests need at least `gpu-operator`. With `network-operator` (the default)
the workflow also passes `--run-multi-gpu-tests`, which runs the prefill/decode
test with the pools on two nodes, moving the KV cache through NIXL over
InfiniBand. The `vllm-controller` input then
bootstraps the Juju controller in K8s.

## Actions

### `setup`

Runs [`scripts/00` to `05`](scripts/) in order:

| Script | What it does |
|---|---|
| `00-install-prereqs.sh` | Installs the juju/kubectl/helm/testflinger-cli snaps, jq/sshuttle, yq, the Testflinger wrapper venv, and generates the **ephemeral SSH keypair** |
| `01-reserve-machines.sh` | Reserves the cluster with [`deploy_testflinger_cluster.py`](testflinger/). The job's test phase prepares every machine (see below); the script then checks SSH access and the preparation marker |
| `02-deploy-k8s.sh` | `k8s bootstrap` on the first worker (network, DNS, load balancer on `LB_CIDR`, gateway, local storage), `k8s join-cluster --worker` for the others, starts the sshuttle tunnels, exports the kubeconfig (`k8s config`) |
| `03-deploy-gpu-operator.sh` | Installs the GPU Operator with Helm and waits for the GPU node labels |
| `04-deploy-network-operator.sh` | Installs the Network Operator, prepares the IB modules and PCI ACS, applies the NicClusterPolicy, runs an `ib_send_bw` check between the workers |
| `05-bootstrap-vllm-controller.sh` | `juju add-k8s vllm --storage csi-rawfile-default`, `juju bootstrap vllm vllm`, `juju switch vllm` |

The node-local preparation runs in the Testflinger job, so the machines reach
the runner ready: `01` embeds
[`scripts/node/prepare-node.sh`](scripts/node/prepare-node.sh) into
`test_data.test_cmds`. The test phase runs before `reserve`, on the agent host,
and runs the script on the device over SSH. The script raises the inotify limits, loads the RDMA modules,
writes the containerd registry mirrors (`/etc/containerd/hosts.d`), installs
the `k8s` snap (`k8s-channel`) and writes `/var/lib/gpu-ib-cluster/prepared`.
Bootstrap and join stay on the runner, because the join token comes from the
bootstrapped node and the Testflinger jobs cannot wait for each other.

Outputs: `state-dir`, `kubeconfig`, `workers`, `vllm-controller`.
`STATE_DIR` and `KUBECONFIG` are also exported to `$GITHUB_ENV` for later
steps.

### `release`

Runs `scripts/99-release.sh`. It cancels the Testflinger jobs, stops sshuttle,
removes the aproxy bypass rules, the `vllm` Juju client entries and the state dir.
It never fails. With `release-machines: "false"`, the reservations are kept
(until `reservation-timeout-hours`) and the runner state is left untouched
(state dir with the kubeconfig, Juju client entries, sshuttle tunnels), so a
detached tmate session can still use it while the job waits at the end.

### Diagnostics

`scripts/collect-diagnostics.sh <dir>` dumps the following:

- Juju status and debug-log for the `vllm` controller;
- Kubernetes objects and events, the ClusterPolicy and NicClusterPolicy;
- `k8s status`, `nvidia-smi` and InfiniBand port counters for every worker.

## SSH access

- **CI:** each run generates a keypair in the state dir. Testflinger's `test`
  phase runs before `reserve` on the agent host and appends the public key to
  `ubuntu@$DEVICE_IP:~/.ssh/authorized_keys` (`test_data.test_cmds`). No SSH
  secret is needed.
- **Humans:** the `ssh-import-ids` (`lp:`/`gh:`) are added to
  `reserve_data.ssh_keys`. The workflow also adds `gh:<user>` for the GitHub
  user who triggered or re-ran it (except bots). With `keep-reservation`, log in with
  `ssh ubuntu@<ip>` (the IPs are in the job summary). On a worker,
  `sudo k8s config` prints the kubeconfig.
- **Runner:** with the `tmate` input, a detached
  [`canonical/action-tmate`](https://github.com/canonical/action-tmate) session
  (reachable from the Canonical VPN with a GitHub-registered SSH key) opens
  before the K8s deployment; the connection command is in the step log and the
  job waits at the end for the session to be closed (10 minutes if nobody
  connects), after the machines are released. Without it, a blocking session
  opens if the cluster setup fails, before releasing the machines. Otherwise set `keep-reservation` and work from the
  reserved machines (e.g. via the bastion); diagnostics are uploaded as an
  artifact.

## Secrets

| Secret | Used for |
|---|---|
| `TESTFLINGER_CLIENT_ID`, `TESTFLINGER_SECRET_KEY` | Authenticating to Testflinger; `job_priority` overrides (setup input `job-priority-overrides`) only if the client is allowed to use them |
| `HF_TOKEN` | The gated Hugging Face model in the GPU test |
| `LLM_STORAGE_AWS_ACCESS_KEY_ID`, `LLM_STORAGE_AWS_SECRET_ACCESS_KEY` | The S3 bucket with the test model (`s3://` tests), as in `ci.yaml` |

## Requirements on the runner

- Ubuntu with passwordless `sudo`, for snaps, apt and sshuttle.
- Network reach to the Testflinger API and SSH (port 22) to the reserved machines
  (tor lab). On the private-endpoint runners all lab traffic goes through aproxy,
  which lets SSH through but resets other ports. So the K8s API (6443, on the
  first worker, tunnelled via the second) and the LB CIDR (via the first
  worker) go through sshuttle tunnels, and port-scoped rules in the
  `ip aproxy` nft table make aproxy skip only that traffic
  (`start_lab_tunnels` and `aproxy_bypass` in `env.sh`).
- The workers reach the Snap Store and pull images through the lab mirrors
  (`K8S_REGISTRIES` in [`scripts/env.sh`](scripts/env.sh)).

## Running locally

Every setting is an env var with a default in [`scripts/env.sh`](scripts/env.sh):

```bash
export SSH_IMPORT_IDS=lp:<you>
export TESTFLINGER_CLIENT_ID=... TESTFLINGER_SECRET_KEY=...   # optional
s=.github/actions/gpu-ib-cluster/scripts
for i in 00 01 02 03 04 05; do "$s"/$i-*.sh || break; done
export KUBECONFIG=~/gpu-ib-cluster-state-local/kubeconfig
tox -e bundle-integration-gpu -- --charms-path=<dir with packed charms>
"$s"/99-release.sh
```

## Provenance

The scripts were adapted from `scripts/local-gpu-ib-cluster/` of the internal
`canonical/terragrunt-deployment-pipelines` repository. The Testflinger wrapper
(`testflinger/*.py`) and `scripts/ib/` are copies of
`sqa_tools/sqa_common/sqa_common/` and
`catalogue/units/nic_cluster_policy/scripts/` at commit `3dc548a4`, including
the not-yet-merged transient-retry fixes. Keep them in sync when updating.
