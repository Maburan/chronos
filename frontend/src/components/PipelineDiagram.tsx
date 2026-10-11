import type { ComponentType, CSSProperties, SVGProps } from "react";

/**
 * The presentational orchestration pipeline — the rail, the six stages, the
 * flowing particles, the worker core-cluster. It holds no data logic of its
 * own: callers hand it a fully-resolved list of stages and whether work is
 * flowing. The live dashboard hero (PipelineHero) feeds it real aggregate
 * task state; the logged-out landing page feeds it an illustrative showcase.
 */

export type PipelineTone = "running" | "ready" | "succeeded" | "failed";

export interface PipelineStage {
  key: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  label: string;
  caption: string;
  /** a live number to surface on the stage, if meaningful */
  value?: number;
  tone?: PipelineTone;
  /** whether this stage is actively carrying work right now */
  hot: boolean;
  /** worker-style core cluster (lit of total) rendered under the stage */
  cores?: { total: number; lit: number };
}

export function PipelineDiagram({
  stages,
  flowing,
  flowDur = "3.4s",
}: {
  stages: PipelineStage[];
  flowing: boolean;
  flowDur?: string;
}) {
  return (
    <div
      className={`pipeline${flowing ? " flowing" : ""}`}
      aria-hidden="true"
      style={{ "--flow-dur": flowDur } as CSSProperties}
    >
      <div className="pipeline-rail">
        <span className="rail-line" />
        <span className="rail-particle" />
        <span className="rail-particle" />
        <span className="rail-particle" />
        <span className="rail-particle" />
      </div>

      {stages.map((s) => {
        const Icon = s.icon;
        return (
          <div key={s.key} className={`pstage${s.hot ? " hot" : ""}`} data-tone={s.tone ?? ""}>
            <div className="pstage-tile">
              <Icon width={20} height={20} />
              {s.value !== undefined && s.value > 0 && <span className="pstage-badge tnum">{s.value}</span>}
            </div>
            <div className="pstage-label">{s.label}</div>
            <div className="pstage-caption">{s.caption}</div>
            {s.cores && (
              <div className="core-cluster" title={`${s.cores.lit} of ${s.cores.total} lit`}>
                {Array.from({ length: s.cores.total }).map((_, i) => (
                  <span key={i} className={`core${i < s.cores!.lit ? " lit" : ""}`} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
