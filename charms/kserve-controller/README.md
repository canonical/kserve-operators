# KServe Controller

## Deployment steps

sudo snap install microk8s --classic --channel=1.24/stable                                                                                                                                    
microk8s enable dns storage ingress metallb:10.64.140.43-10.64.140.49 rbac                                                                                                                    
microk8s status --wait-ready                                              
juju bootstrap microk8s                                                                                                                                                                       
juju add-model kubeflow   

charmcraft pack
juju deploy ./kserve-controller_ubuntu-20.04-amd64.charm --resource kserve-controller-image=kserve/kserve-controller:v0.8.0 --resource kube-rbac-proxy-image=gcr.io/kubebuilder/kube-rbac-proxy:v0.8.0 --trust
