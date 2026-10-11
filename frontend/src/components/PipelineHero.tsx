import { Link } from "react-router-dom";
import {
  ServerIcon,
  DatabaseIcon,
  SendIcon,
  InboxIcon,
  CpuIcon,
  CheckCircleIcon,
  PlusIcon,
} from "./icons";
import { PipelineDiagram, type PipelineStage, type PipelineTone } from "./PipelineDiagram";

/**
 * The live orchestration hero — Chronos's signature centerpiece.
 *
 * It renders the *real* execution architecture (API → Postgres/Outbox →
 * Relay → RabbitMQ → Workers → Settled) as a pipeline, and drives its
 * motion from genuine aggregate task state polled from the API: when tasks
 * are actually running or queued, work visibly flows across the rail and
 * the live stages light up; when the system is idle, the pipeline settles
 * and nothing pretends to be busy. No number here is fabricated — each is a
 * sum of real task states across every workflow's latest run.
 */

export interface SystemPulse {
  running: number;
  ready: number;
  pending: number;
  succeeded: number;
  failed: number;
  skipped: number;
  total: number;
  workflowCount: number;
}

function headline(p: SystemPulse): { title: string; sub: string } {
  if (p.total === 0) {
    return {
      title: "Ready to orchestrate",
      sub: "Chronos runs, retries, and recovers your workflows automatically. Build one to watch it flow.",
    };
  }
  if (p.running > 0) {
    return {
      title: `${p.running} task${p.running === 1 ? "" : "s"} executing`,
      sub:
        p.ready > 0
          ? `${p.ready} more queued and moving through the pipeline.`
          : "Work is flowing through the pipeline right now.",
    };
  }
  if (p.ready > 0 || p.pending > 0) {
    const waiting = p.ready + p.pending;
    return {
      title: `${waiting} task${waiting === 1 ? "" : "s"} queued`,
      sub: "Waiting on a worker to claim them — dispatch is in flight.",
    };
  }
  if (p.failed > 0) {
    return {
      title: "Idle — with failures to review",
      sub: `${p.succeeded} succeeded · ${p.failed} failed terminally across your runs.`,
    };
  }
  return {
    title: "All caught up",
    sub: `${p.succeeded} task${p.succeeded === 1 ? "" : "s"} completed. The pipeline is quiet and ready.`,
  };
}

export function PipelineHero({ pulse }: { pulse: SystemPulse }) {
  const active = pulse.running > 0 || pulse.ready > 0 || pulse.pending > 0;
  const inFlight = pulse.ready + pulse.pending;
  const { title, sub } = headline(pulse);

  // Flow speed scales (gently) with how much work is moving — more load,
  // faster current. Clamped so it never becomes frantic or imperceptible.
  const load = pulse.running + pulse.ready + pulse.pending;
  const flowDur = `${Math.max(1.6, 4.2 - Math.min(load, 24) * 0.1).toFixed(2)}s`;

  // How many worker "cores" to light — a nod to the real replica model.
  const CORES = 5;
  const litCores = Math.min(CORES, pulse.running);

  const stages: PipelineStage[] = [
    { key: "api", icon: ServerIcon, label: "API", caption: "Ingest", hot: active },
    { key: "db", icon: DatabaseIcon, label: "Postgres", caption: "Commit + outbox", hot: active },
    { key: "relay", icon: SendIcon, label: "Relay", caption: "Publish", hot: inFlight > 0 },
    { key: "queue", icon: InboxIcon, label: "RabbitMQ", caption: "Queue", value: inFlight, tone: "ready", hot: inFlight > 0 },
    { key: "workers", icon: CpuIcon, label: "Workers", caption: "Execute", value: pulse.running, tone: "running", hot: pulse.running > 0, cores: { total: CORES, lit: litCores } },
    { key: "done", icon: CheckCircleIcon, label: "Settled", caption: "Terminal", value: pulse.succeeded, tone: "succeeded", hot: false },
  ];

  return (
    <section className={`hero glass${active ? " is-active" : ""}`}>
      <div className="hero-glow" aria-hidden="true" />

      <div className="hero-top">
        <div className="hero-lede">
          <div className="hero-eyebrow">
            <span className={`live-dot${active ? " on" : ""}`} />
            {active ? "Live orchestration" : "System idle"}
          </div>
          <h1 className="hero-title">{title}</h1>
          <p className="hero-sub">{sub}</p>
          <div className="hero-cta">
            <Link to="/workflows/new">
              <button type="button" className="btn primary">
                <PlusIcon /> New workflow
              </button>
            </Link>
            <Link to="/workflows" className="hero-link">
              View all workflows →
            </Link>
          </div>
        </div>

        <div className="hero-stats" role="group" aria-label="Live system pulse">
          <PulseStat label="Running" value={pulse.running} tone="running" live={pulse.running > 0} />
          <PulseStat label="Queued" value={inFlight} tone="ready" live={inFlight > 0} />
          <PulseStat label="Completed" value={pulse.succeeded} tone="succeeded" />
          {pulse.failed > 0 && <PulseStat label="Failed" value={pulse.failed} tone="failed" />}
        </div>
      </div>

      <PipelineDiagram stages={stages} flowing={active} flowDur={flowDur} />
    </section>
  );
}

function PulseStat({ label, value, tone, live }: { label: string; value: number; tone: PipelineTone; live?: boolean }) {
  return (
    <div className={`pulse-stat${live ? " live" : ""}`} data-tone={tone}>
      <span className="pulse-value tnum">{value}</span>
      <span className="pulse-label">
        {live && <span className="pulse-ring" />}
        {label}
      </span>
    </div>
  );
}
