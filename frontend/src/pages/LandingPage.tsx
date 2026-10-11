import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ServerIcon,
  DatabaseIcon,
  SendIcon,
  InboxIcon,
  CpuIcon,
  CheckCircleIcon,
  RefreshIcon,
  LayersIcon,
} from "../components/icons";
import { PipelineDiagram, type PipelineStage } from "../components/PipelineDiagram";
import { prefersReducedMotion } from "../execution";

/**
 * The logged-out front door. It leads with the same orchestration pipeline
 * the live dashboard uses, cycling through an *illustrative* run so a first
 * visitor sees what Chronos does — work flowing from the API through the
 * outbox, broker, and workers to a settled state. This is clearly a
 * showcase (there is no signed-in system behind it), not live data.
 */

interface Frame {
  queued: number;
  running: number;
  done: number;
}

// One illustrative run: a burst arrives, workers pick it up and scale, then
// it drains and settles. Loops.
const FRAMES: Frame[] = [
  { queued: 6, running: 0, done: 12 },
  { queued: 4, running: 3, done: 14 },
  { queued: 1, running: 5, done: 19 },
  { queued: 0, running: 2, done: 26 },
  { queued: 0, running: 0, done: 30 },
];

const CORES = 5;

function framesToStages(f: Frame): PipelineStage[] {
  const active = f.running > 0 || f.queued > 0;
  return [
    { key: "api", icon: ServerIcon, label: "API", caption: "Ingest", hot: active },
    { key: "db", icon: DatabaseIcon, label: "Postgres", caption: "Commit + outbox", hot: active },
    { key: "relay", icon: SendIcon, label: "Relay", caption: "Publish", hot: f.queued > 0 },
    { key: "queue", icon: InboxIcon, label: "RabbitMQ", caption: "Queue", value: f.queued, tone: "ready", hot: f.queued > 0 },
    { key: "workers", icon: CpuIcon, label: "Workers", caption: "Execute", value: f.running, tone: "running", hot: f.running > 0, cores: { total: CORES, lit: Math.min(CORES, f.running) } },
    { key: "done", icon: CheckCircleIcon, label: "Settled", caption: "Terminal", value: f.done, tone: "succeeded", hot: false },
  ];
}

const FEATURES = [
  {
    icon: RefreshIcon,
    title: "Survives worker crashes",
    body: "Kill a worker mid-task and the reaper reclaims its lease and re-dispatches the work — proven against a real killed process, not a diagram.",
  },
  {
    icon: CheckCircleIcon,
    title: "Exactly-once effects",
    body: "PostgreSQL is the single source of truth. Atomic claiming and a transactional outbox mean no task is ever lost or run twice, even if the broker hiccups.",
  },
  {
    icon: CpuIcon,
    title: "Scales with the backlog",
    body: "Workers scale horizontally across one queue, and the autoscaler adds and removes replicas per task type as the queue rises and drains.",
  },
];

export function LandingPage() {
  const reduced = prefersReducedMotion();
  const [frame, setFrame] = useState(reduced ? 2 : 0);

  useEffect(() => {
    if (reduced) return;
    const id = setInterval(() => setFrame((i) => (i + 1) % FRAMES.length), 2200);
    return () => clearInterval(id);
  }, [reduced]);

  const f = FRAMES[frame];
  const stages = framesToStages(f);
  const flowing = f.running > 0 || f.queued > 0;

  return (
    <div className="container landing">
      <section className="hero glass is-active landing-hero">
        <div className="hero-glow" aria-hidden="true" />
        <div className="hero-lede landing-lede">
          <div className="hero-eyebrow">
            <span className="live-dot on" />
            Distributed task orchestration
          </div>
          <h1 className="hero-title landing-title">
            Workflows that run, retry, and recover — on their own.
          </h1>
          <p className="hero-sub">
            Chronos executes graphs of dependent tasks with an at-least-once guarantee, atomic
            claiming, and automatic crash recovery. Submit a workflow over HTTP and watch it flow
            through the pipeline.
          </p>
          <div className="hero-cta">
            <Link to="/register">
              <button type="button" className="btn primary lg">
                Get started
              </button>
            </Link>
            <Link to="/login" className="hero-link">
              Sign in →
            </Link>
          </div>
        </div>

        <div className="landing-stage">
          <PipelineDiagram stages={stages} flowing={flowing} flowDur="2.4s" />
        </div>
      </section>

      <div className="landing-features">
        {FEATURES.map((ft) => {
          const Icon = ft.icon;
          return (
            <div key={ft.title} className="feature-card">
              <div className="feature-icon">
                <Icon width={20} height={20} />
              </div>
              <div className="feature-title">{ft.title}</div>
              <p className="feature-body">{ft.body}</p>
            </div>
          );
        })}
      </div>

      <div className="landing-foot">
        <LayersIcon width={16} height={16} />
        <span>
          PostgreSQL · RabbitMQ · Redis · FastAPI — the real distributed stack, running locally in
          one <span className="mono">docker compose up</span>.
        </span>
      </div>
    </div>
  );
}
