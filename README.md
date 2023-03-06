## KServe Operators - a component of the Charmed Kubeflow distribution from Canonical

This repository hosts the Kubernetes Python Operators for KServe
(see [CharmHub](https://charmhub.io/?q=dex-auth)).

Upstream documentation can be found at https://kserve.github.io/website/0.8/

## Usage

### Pre-requisites

* Microk8s <supported-version>/stable, supported-version=1.22, 1.23, 1.24
>NOTE: These instructions assume you have run `microk8s enable dns storage rbac metallb:"10.64.140.43-10.64.140.49,192.168.0.105-192.168.0.111"`

* `istio-pilot` and `istio-ingressgateway` charms deployed. See "Deploy dependencies" for deploy instructions.

### Add a model and set variables

```
MODEL_NAME="kserve"
DEFAULT_GATEWAY="kserve-gateway"
juju add-model ${MODEL_NAME}
```

### Deploy dependencies

KServe requires istio to be deployed in the cluster. To correctly configure them, you can:

```
ISTIO_CHANNEL=1.11/stable
juju deploy istio-pilot --config default-gateway=${DEFAULT_GATEWAY} --channel ${ISTIO_CHANNEL} --trust
juju deploy istio-gateway istio-ingressgateway --config kind="ingress" --channel ${ISTIO_CHANNEL} --trust
juju relate istio-pilot istio-ingressgateway
```

### Deploy in `RawDeployment` mode

KServe supports `RawDeployment` mode to enable `InferenceService`, which removes the KNative dependency and unlocks some of its limitations, like mounting multiple volumes. Please note this mode is not loaded with serverless capabilities, for that you'd need to deploy in `Serverless` mode.

1. Deploy `kserver-controller`

```
juju deploy kserve-controller --channel <channel> --trust
```

> `channel` is the available channels of the Charmed KServe:
* latest/stable
* 0.8/stable

### Deploy in `Serverless` mode

TODO

## Deploy a simple `InferenceService`

To deploy a simple example of an `InferenceServer`, you can use the one provided in `examples/`

> NOTE: this example is based on [First InferenceService](https://kserve.github.io/website/0.9/get_started/first_isvc/#2-create-an-inferenceservice)

1. Create an `InferenceService`

```
kubectl apply -f sklearn-iris.yaml
```

2. Check the `InferenceService` status

```
kubectl get inferenceservices sklearn-iris
```

3. Determine the URL for performing inference

* Using the `ClusterIP`

> NOTE: this method can only be used for performing inference within the cluster.

```
SERVICE_IP=$(kubectl get svc sklearn-iris-predictor-default -ojsonpath='{.spec.clusterIP}')
INFERENCE_URL="${SERVICE_IP}/v1/models/sklearn-iris:predict"
```

* Using Istio Ingress and LoadBalancer

> NOTE: This step assumes you enabled MetalLB addon in Microk8s at the beginning of this tutorial.

```
LOADBALANCER_IP=$(kubectl get svc istio-ingressgateway-workload -n${MODEL_NAME} -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
INFERENCE_URL="{LOADBALANCER_IP}/v1/models/sklearn-iris:predict"
```

4. Perform inference

Create a file with the input request:

```
cat <<EOF > "./iris-input.json"
{
  "instances": [
    [6.8,  2.8,  4.8,  1.4],
    [6.0,  3.4,  4.5,  1.6]
  ]
}
EOF

Now call the `InferenceService`:

```
curl -v $INFERENCE_URL -d @iris-input.json
```

Expected output:

```
{"predictions": [1, 1]}
```

## Looking for a fully supported platform for MLOps?

Canonical [Charmed Kubeflow](https://charmed-kubeflow.io) is a state of the art, fully supported MLOps platform that helps data scientists collaborate on AI innovation on any cloud from concept to production, offered by Canonical - the publishers of [Ubuntu](https://ubuntu.com).

[![Kubeflow diagram](https://res.cloudinary.com/canonical/image/fetch/f_auto,q_auto,fl_sanitize,w_350,h_304/https://assets.ubuntu.com/v1/10400c98-Charmed-kubeflow-Topology-header.svg)](https://charmed-kubeflow.io)

Charmed Kubeflow is free to use: the solution can be deployed in any environment without constraints, paywall or restricted features. Data labs and MLOps teams only need to train their data scientists and engineers once to work consistently and efficiently on any cloud – or on-premise.

Charmed Kubeflow offers a centralised, browser-based MLOps platform that runs on any conformant Kubernetes – offering enhanced productivity, improved governance and reducing the risks associated with shadow IT.

Learn more about deploying and using Charmed Kubeflow at [https://charmed-kubeflow.io](https://charmed-kubeflow.io).

### Key features
* Centralised, browser-based data science workspaces: **familiar experience**
* Multi user: **one environment for your whole data science team**
* NVIDIA GPU support: **accelerate deep learning model training**
* Apache Spark integration: **empower big data driven model training**
* Ideation to production: **automate model training & deployment**
* AutoML: **hyperparameter tuning, architecture search**
* Composable: **edge deployment configurations available**

### What’s included in Charmed Kubeflow 1.4
* LDAP Authentication
* Jupyter Notebooks
* Work with Python and R
* Support for TensorFlow, Pytorch, MXNet, XGBoost
* TFServing, Seldon-Core
* Katib (autoML)
* Apache Spark
* Argo Workflows
* Kubeflow Pipelines

### Why engineers and data scientists choose Charmed Kubeflow
* Maintenance: Charmed Kubeflow offers up to two years of maintenance on select releases
* Optional 24/7 support available, [contact us here](https://charmed-kubeflow.io/contact-us) for more information
* Optional dedicated fully managed service available, [contact us here](https://charmed-kubeflow.io/contact-us) for more information or [learn more about Canonical’s Managed Apps service](https://ubuntu.com/managed/apps).
* Portability: Charmed Kubeflow can be deployed on any conformant Kubernetes, on any cloud or on-premise

### Documentation
Please see the [official docs site](https://charmed-kubeflow.io/docs) for complete documentation of the Charmed Kubeflow distribution.

### Bugs and feature requests
If you find a bug in our operator or want to request a specific feature, please file a bug here:
[https://github.com/canonical/dex-auth-operator/issues](https://github.com/canonical/dex-auth-operator/issues)

### License
Charmed Kubeflow is free software, distributed under the [Apache Software License, version 2.0](https://github.com/canonical/dex-auth-operator/blob/master/LICENSE).

### Contributing
Canonical welcomes contributions to Charmed Kubeflow. Please check out our [contributor agreement](https://ubuntu.com/legal/contributors) if you're interested in contributing to the distribution.

### Security
Security issues in Charmed Kubeflow can be reported through [LaunchPad](https://wiki.ubuntu.com/DebuggingSecurity#How%20to%20File). Please do not file GitHub issues about security issues.


