# KServe Controller

## Deployment steps

sudo snap install microk8s --classic --channel=1.24/stable                                                                                                                                    
microk8s enable dns storage ingress metallb:10.64.140.43-10.64.140.49 rbac                                                                                                                    
microk8s status --wait-ready                                              
juju bootstrap microk8s                                                                                                                                                                       
juju add-model kubeflow   

cat <<EOF > "./istio.yaml"
bundle: kubernetes
name: istio
applications:
  istio-ingressgateway:
    charm: istio-gateway
    channel: 1.11/stable
    scale: 1
    trust: true
    _github_repo_name: istio-operators
    options:
      kind: ingress
  istio-pilot:
    charm: istio-pilot
    channel: 1.11/stable
    scale: 1
    trust: true
    _github_repo_name: istio-operators
    options:
      default-gateway: kubeflow-gateway
relations:
  - [istio-pilot:istio-pilot, istio-ingressgateway:istio-pilot]
EOF

juju deploy ./istio.yaml

charmcraft pack
juju deploy ./kserve-controller_ubuntu@24.04-amd64.charm --resource kserve-controller-image=kserve/kserve-controller:v0.8.0 --trust


