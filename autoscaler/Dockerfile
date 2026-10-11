# Autoscaler image = the Chronos backend base (same Python, same
# requirements, same code) PLUS a Docker CLI and the Compose plugin, so the
# autoscaler process can drive `docker compose` against the host daemon
# through the mounted /var/run/docker.sock. This is the ONE service that
# needs the Docker client; every other service runs the plain root
# Dockerfile, so the extra ~50MB of CLI lives only here.
#
# Static binaries (not the apt repo) are used deliberately: they don't
# depend on the base image's Debian codename matching a Docker apt suite,
# so this build stays deterministic across python:*-slim bumps.
FROM python:3.13-slim

ARG DOCKER_CLI_VERSION=27.3.1
ARG COMPOSE_VERSION=2.29.7

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Same convention as the base image: the compose service sets the command
# (python -m autoscaler). No ENTRYPOINT/CMD baked in.
