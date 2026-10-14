# Jenkins for the payment-api sandbox pipeline (ticket #5). Runs in Docker on
# the host, outside the kind cluster, per decision D1 - the cluster is the
# thing being deliberately broken, so CI must not share its failure domain.
#
# This image adds the three things the pipeline needs beyond stock Jenkins:
#   - docker CLI: to build the payment-api image and to `docker exec` into
#     the kind node container, mirroring scripts/up.sh's image-load step.
#     Reaches the host's own Docker daemon via the socket bind-mounted in
#     infra/jenkins-compose.yml (this container is a *sibling* of the kind
#     node containers, not a nested Docker-in-Docker daemon).
#   - kubectl and kind: kind generates a kubeconfig on demand
#     (`kind get kubeconfig --internal`) whose server address is the kind
#     control-plane container's own name and port, reachable because this
#     container is attached to the "kind" Docker network in
#     infra/jenkins-compose.yml. See that file's comment for why this is
#     used instead of the host-published API server port.
#   - uv: to install and test sandbox/payment-api exactly as a developer
#     would on the host.
FROM jenkins/jenkins:lts-jdk17

USER root

# libgomp1: the OpenMP runtime LightGBM loads when PyCaret imports it in the
# helix-mlops job (OPT-1); without it training fails at import.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only, as a static binary - this container never runs a Docker
# daemon of its own, it only ever talks to the host's daemon over the
# bind-mounted socket. Debian's `docker.io` package on this base image does
# not ship the client binary at all (daemon and tooling only), which is why
# this is fetched directly from Docker's static release archive instead.
#
# Version pinned to match this host's Docker Engine (29.7.2): the
# Jenkinsfile's deploy stage uses `docker save --platform`, a client flag
# that an older CLI (e.g. 27.4.0) does not have even though this host's
# daemon supports it.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-29.7.2.tgz \
      | tar xz --strip-components=1 -C /usr/local/bin docker/docker

# Buildx CLI plugin - the Jenkinsfile's build stage passes
# `--provenance=false` (same as scripts/up.sh) to avoid an attestation
# manifest list that `ctr images import` cannot load, and that flag only
# exists on the BuildKit-backed builder, not the legacy one the plain
# `docker` client falls back to without this plugin.
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSLo /usr/local/lib/docker/cli-plugins/docker-buildx \
      https://github.com/docker/buildx/releases/download/v0.19.2/buildx-v0.19.2.linux-amd64 \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx

# Pinned to the cluster's server version rather than tracking stable.txt.
# kind v0.24.0 brings up a v1.32.x API server, while stable.txt currently
# resolves to v1.37.0 - a five minor version skew, well outside kubectl's
# supported +/-1. It happens to work for the handful of commands the deploy
# stage runs, which is precisely how a skew like this survives unnoticed
# until a command it does break lands in the pipeline.
RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/v1.32.2/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

RUN curl -fsSLo /usr/local/bin/kind \
      "https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64" \
    && chmod +x /usr/local/bin/kind

RUN curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

# Kept outside /var/jenkins_home (a volume) so a rebuild of this image always
# ships the current jenkins.yaml, instead of the file only landing on the
# volume's first, empty run.
COPY jenkins.yaml /opt/jenkins-config/jenkins.yaml

USER jenkins

ENV CASC_JENKINS_CONFIG=/opt/jenkins-config/jenkins.yaml
