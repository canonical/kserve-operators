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
        capabilities:
          add: ["IPC_LOCK"]
