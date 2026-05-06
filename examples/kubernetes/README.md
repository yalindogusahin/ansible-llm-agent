# Kubernetes playbooks

Investigation playbooks for nodes in a Kubernetes cluster. Run **on the
node**, not from a control plane / kubectl-less workstation. Assumptions:

- Node has `kubectl` on PATH and a kubeconfig that the agent's user can read
  (typically `/etc/kubernetes/kubelet.conf` or `~/.kube/config`).
- Container runtime is containerd (so `crictl` works); CRI-O / docker users
  should adjust prompts.
- `kubelet` runs as a systemd unit named `kubelet`.

`rules.yml` allows `kubectl`, `crictl`, and reads under `/var/log/pods/`,
`/var/log/containers/`, `/etc/kubernetes/`. Mutation verbs (`kubectl delete`,
`kubectl edit`, `kubectl drain`) are not blocked at the rule layer — the
read-only nature comes from the prompts. If you want hard guarantees,
narrow `kubectl` invocations to subcommands via the prompt and rely on RBAC.

## Playbooks

- **pod-crashloop.yml** — a named pod is in CrashLoopBackOff; pull `describe`,
  events, and the previous-container logs.
- **node-not-ready.yml** — node shows `NotReady`; check kubelet, runtime,
  network plugin, and disk pressure.
