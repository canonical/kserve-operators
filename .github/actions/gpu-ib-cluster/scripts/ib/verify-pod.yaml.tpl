apiVersion: v1
kind: Pod
metadata:
  name: ${pod_name}
  labels:
    app: ib-verify
    ib-verify-role: ${role}
spec:
  restartPolicy: Never
  nodeSelector:
    kubernetes.io/hostname: ${node_name}
  containers:
    - name: ib-verify
      image: ubuntu:24.04
      command: ["sleep", "3600"]
      resources:
        limits:
          ${resource_name_prefixed}: 1
        requests:
          ${resource_name_prefixed}: 1
      securityContext:
        # ib_send_bw pins its buffers beyond the default memlock limit; IPC_LOCK lifts it.
        # See capabilities(7), CAP_IPC_LOCK, and
        # https://github.com/Mellanox/k8s-rdma-shared-dev-plugin/blob/master/example/test-hca-pod.yaml
        capabilities:
          add: ["IPC_LOCK"]
