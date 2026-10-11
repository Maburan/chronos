# Chronos backend image -- shared by every Python process in the stack
# (migrate, api, relay, reaper, worker-*). These remain separate
# containers/services in docker-compose.yml; this Dockerfile only builds
# the one image they all run from, differing solely in the `command:`
# each service overrides it with. See HANDOVER.md's Phase 5 section for
# why one image is correct here: every one of these processes is the
# same codebase, same dependencies, same "python -m <package>" shape --
# splitting them into separate images would duplicate the same
# requirements.txt install for no benefit.
FROM python:3.13-slim

# curl: used by docker-compose healthchecks (e.g. the API's /healthz).
# Not needed by the application itself.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No ENTRYPOINT/CMD here -- every service in docker-compose.yml sets its
# own `command:` (python -m db / uvicorn ... / python -m relay / python
# -m reaper / python -m worker). Keeps this image a plain, reusable base
# rather than baking in one process's assumptions.
