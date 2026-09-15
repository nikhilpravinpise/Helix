# infra/ansible - host toolchain provisioning (subphase 1.13)

A single localhost playbook that makes a machine able to run the Phase 1
environment: it checks (and on Linux installs) the five required binaries,
ensures `~/.kube` exists, and applies the `vm.max_map_count` sysctl that
Elasticsearch and SonarQube's embedded Elasticsearch need.

```bash
# Linux host, or inside WSL2 on the Windows dev machine:
ansible-playbook infra/ansible/playbook.yml
```

## What it does

| Step | Behavior |
|---|---|
| Tool checks | `docker`, `kind`, `kubectl`, `uv`, `terraform` - each probed with its version command; missing ones are collected and reported. |
| Installs (Linux only) | `uv` (user-local, no sudo), `kind` and `kubectl` (single binaries into `/usr/local/bin`, sudo). Docker and Terraform are *reported*, not scripted: a container engine and an apt-repo install are bigger contracts than a binary drop - see the playbook comments and docs/bring-up.md for pointers. |
| Directories | Creates `~/.kube` (mode 0700) - the tools expect it for contexts. |
| `vm.max_map_count` | Linux: sets `524288` live via `sysctl` and persists it in `/etc/sysctl.d/99-helix.conf`. Covers Elasticsearch 8 (`>=262144`) and SonarQube (`>=524288`). |

## Honest platform notes

- **Ansible control node cannot be native Windows.** On the Windows dev
  host, run the playbook inside WSL2 (`wsl` -> Ubuntu shell ->
  `ansible-playbook ...`). The check/install tasks then provision the WSL
  side; Docker Desktop still supplies the real `docker` on the Windows
  side via its WSL integration.
- **Windows `vm.max_map_count`** lives inside Docker Desktop's own
  `docker-desktop` WSL distro, which a guest-side Ansible run cannot
  reach. Set it once per Docker Desktop session:
  `wsl -d docker-desktop -u root sysctl -w vm.max_map_count=524288`
  (same command in `infra/sonarqube-compose.yml`'s header). To persist it
  across restarts use `.wslconfig` `kernelCommandLine =
  sysctl.vm.max_map_count=524288`.
- The playbook deliberately does **not** duplicate anything the Compose
  files or Terraform own (container env, ports, volumes) - it is a host
  provisioner, not a service manager.
