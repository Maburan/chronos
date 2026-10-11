"""Chronos autoscaling.

Policy-based (not ML-based) horizontal autoscaling of worker replicas,
built ON TOP of the existing horizontal-scaling architecture (one durable
RabbitMQ queue per task type, N worker replicas per queue, PostgreSQL as
the source of truth). Nothing here changes task transition semantics, task
claiming, leases, heartbeats, the reaper, or the transactional outbox: the
autoscaler is a pure control loop that observes backlog and asks a
``WorkerRuntime`` to change a replica count.

    task type -> RabbitMQ queue -> N worker replicas
                                        ^
                                        | desired replica count
                                   Autoscaler (this package)

Layers (each independently testable):

  * policy.py     -- ScalingPolicy: backlog -> desired replicas (+ clamp).
  * runtime.py    -- WorkerRuntime protocol + DockerComposeRuntime.
  * autoscaler.py -- Autoscaler: the reconcile loop with scale-up
                     stabilization and scale-down cooldown.
  * config.py     -- AutoscalerConfig.from_env (project conventions).
  * __main__.py   -- the `python -m autoscaler` process entrypoint.
"""
