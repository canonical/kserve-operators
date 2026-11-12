# gpu-ib-cluster actions

Composite actions that bring up, and tear down, a real GPU cluster connected
via InfiniBand on [Testflinger](https://testflinger.canonical.com), used by
[`gpu_ib_integration.yaml`](../../workflows/gpu_ib_integration.yaml) to run the
GPU bundle integration tests (`tox -e bundle-integration-gpu`).

## Topology

Cluster spec [`tor-A100_x2_IB`](testflinger/cluster-specs/tor-A100_x2_IB.yaml):

| Role | Testflinger queue | Used for |
|---|---|---|
| `controller` | akis | Juju manual-cloud controller (`controller-manual-cloud`) only |
| `worker-gpu` | cortez, blanka | Canonical K8s nodes (8x A100 each, InfiniBand `ibp18s0`) |
| GH runner | — | Orchestrator: juju/kubectl/helm client, sshuttle tunnels (Juju API, K8s API, LB CIDR), tox |

The K8s cluster runs the NVIDIA GPU Operator and the NVIDIA Network Operator,
which provides the RDMA shared device plugin (`rdma/rdma_shared_device_ib`). A
second Juju controller (`vllm`) is bootstrapped inside K8s; the tests run
against it.

[`tor-k8s-smoke`](testflinger/cluster-specs/tor-k8s-smoke.yaml) (controller
hidon, workers cortez and buzzsaw) validates the setup with
`up-to: k8s`. The `up-to` input (`k8s`, `gpu-operator`, `network-operator`,
`all`) stops the setup after that stage; the tests need `all`.

## Actions

### `setup`

Runs [`scripts/00` to `06`](scripts/) in order:

| Script | What it does |
|---|---|
| `00-install-prereqs.sh` | Installs the juju/kubectl/helm/testflinger-cli snaps, jq/sshuttle, yq, the Testflinger wrapper venv, and generates the **ephemeral SSH keypair** |
| `01-reserve-machines.sh` | Reserves the cluster with [`deploy_testflinger_cluster.py`](testflinger/), then prepares the hosts (inotify limits, RDMA modules) |
| `02-bootstrap-juju.sh` | Starts the sshuttle tunnels, adds a manual cloud on the controller host, then runs `juju bootstrap` |
| `03-deploy-k8s.sh` | Enlists the workers as manual machines, deploys the `k8s` charm, exports the kubeconfig |
| `04-deploy-gpu-operator.sh` | Installs the GPU Operator with Helm and waits for the GPU node labels |
| `05-deploy-network-operator.sh` | Installs the Network Operator, prepares the IB modules and PCI ACS, applies the NicClusterPolicy, runs an `ib_send_bw` check between the workers |
| `06-bootstrap-vllm-controller.sh` | `juju add-k8s vllm --storage csi-rawfile-default`, `juju bootstrap vllm vllm`, `juju switch vllm` |

Outputs: `state-dir`, `kubeconfig`, `controller`, `workers`, `vllm-controller`.
`STATE_DIR` and `KUBECONFIG` are also exported to `$GITHUB_ENV` for later
steps.

### `release`

Runs `scripts/99-release.sh`. It cancels the Testflinger jobs, stops sshuttle,
removes the `~/.ssh/config` block, the Juju client entries and the state dir.
It never fails. With `release-machines: "false"`, the reservations are kept
(until `reservation-timeout-hours`) and only the runner state is cleaned.

### Diagnostics

`scripts/collect-diagnostics.sh <dir>` dumps the following:

- Juju status and debug-log for both controllers;
- Kubernetes objects and events, the ClusterPolicy and NicClusterPolicy;
- `nvidia-smi` and InfiniBand port counters for every worker.

## SSH access

- **CI:** each run generates a keypair in the state dir. Testflinger's `test`
  phase runs before `reserve` on the agent host and appends the public key to
  `ubuntu@$DEVICE_IP:~/.ssh/authorized_keys` (`test_data.test_cmds`). A
  `~/.ssh/config` block scopes the key to the reserved IPs, because
  `juju bootstrap` has no `--private-key` option. No SSH secret is needed.
- **Humans:** the `ssh-import-ids` (`lp:`/`gh:`) are added to
  `reserve_data.ssh_keys`. The workflow also adds `gh:<user>` for the GitHub
  user who triggered or re-ran it (except bots). With `keep-reservation`, log in with
  `ssh ubuntu@<ip>` (the IPs are in the job summary). On a worker,
  `sudo k8s config` prints the kubeconfig.
- **Runner:** not reachable via inbound SSH (private endpoint), and the tmate
  relay is blocked too. To debug, set `keep-reservation` and work from the
  reserved machines (e.g. via the bastion); diagnostics are uploaded as an
  artifact.

## Secrets

| Secret | Used for |
|---|---|
| `TESTFLINGER_CLIENT_ID`, `TESTFLINGER_SECRET_KEY` | Authenticating to Testflinger; `job_priority` overrides (setup input `job-priority-overrides`) only if the client is allowed to use them |
| `HF_TOKEN` | The gated Hugging Face model in the GPU test |

## Requirements on the runner

- Ubuntu with passwordless `sudo`, for snaps, apt and sshuttle.
- Network reach to the Testflinger API and SSH (port 22) to the reserved machines
  (tor lab). On the private-endpoint runners all lab traffic goes through aproxy,
  which lets SSH through but resets other ports. So the Juju API (17070), the
  K8s API (6443) and the LB CIDR go through sshuttle tunnels, and port-scoped
  rules in the `ip aproxy` nft table make aproxy skip only that traffic
  (`start_lab_tunnels` and `aproxy_bypass` in `env.sh`). The
  terragrunt-deployment-pipelines runner (`solqa-ps7`) reaches 17070/6443
  directly and only tunnels the LB CIDR.
- The workers pull images through the lab mirrors (`K8S_REGISTRIES` in
  [`scripts/env.sh`](scripts/env.sh)).

## Running locally

Every setting is an env var with a default in [`scripts/env.sh`](scripts/env.sh):

```bash
export SSH_IMPORT_IDS=lp:<you>
export TESTFLINGER_CLIENT_ID=... TESTFLINGER_SECRET_KEY=...   # optional
s=.github/actions/gpu-ib-cluster/scripts
for i in 00 01 02 03 04 05 06; do "$s"/$i-*.sh || break; done
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
