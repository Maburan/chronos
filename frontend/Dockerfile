# Chronos frontend image -- Vite dev server only (see Phase 5 constraint:
# no production deployment complexity yet). docker-compose.yml bind-mounts
# ./frontend over /app so edits on the host hot-reload through Vite's own
# HMR; this image just needs node_modules installed once, matching the
# Linux container's platform (a named volume over /app/node_modules in
# compose keeps this install from being overwritten by a host bind mount
# built for a different OS/arch).
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173

# --host 0.0.0.0: Vite's dev server binds localhost-only by default,
# which is unreachable from outside the container.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
