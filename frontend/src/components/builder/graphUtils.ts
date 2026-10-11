/**
 * Pure graph helpers for the builder canvas. Mirrors the backend's own
 * rules exactly (see db/persistence.py::_validate_no_cycles and
 * save_workflow_definition's self-dependency check) so a connection the
 * canvas allows is always one the backend will also accept -- this is a
 * client-side UX nicety (reject instantly, no round trip), never the
 * authority; the backend still validates independently on Save.
 */

export interface SimpleEdge {
  source: string;
  target: string;
}

/** True if adding `candidate` (source depends-on target, i.e. an edge
 * source -> target meaning "target must run after source" in canvas
 * terms -- see the builder page for the exact direction convention) would
 * create a cycle among `edges`. */
export function wouldCreateCycle(edges: SimpleEdge[], candidate: SimpleEdge): boolean {
  if (candidate.source === candidate.target) return true;

  const adjacency = new Map<string, string[]>();
  for (const e of edges) {
    if (!adjacency.has(e.source)) adjacency.set(e.source, []);
    adjacency.get(e.source)!.push(e.target);
  }
  if (!adjacency.has(candidate.source)) adjacency.set(candidate.source, []);
  adjacency.get(candidate.source)!.push(candidate.target);

  // Is there already a path from candidate.target back to candidate.source?
  const visited = new Set<string>();
  const stack = [candidate.target];
  while (stack.length > 0) {
    const node = stack.pop()!;
    if (node === candidate.source) return true;
    if (visited.has(node)) continue;
    visited.add(node);
    for (const next of adjacency.get(node) ?? []) stack.push(next);
  }
  return false;
}

export interface LayoutInput {
  id: string;
  dependsOn: string[];
}

const NODE_W = 220;
const NODE_H = 90;
const COL_GAP = 100;
const ROW_GAP = 32;

/** Layered left-to-right auto-layout, identical in spirit to the old
 * static DependencyGraph's depth-based layering -- used only for nodes
 * that have never been manually positioned (position_x/position_y both
 * null). Manually dragged nodes keep their saved position forever. */
export function autoLayout(nodes: LayoutInput[]): Map<string, { x: number; y: number }> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depthCache = new Map<string, number>();

  function depthOf(id: string, visiting: Set<string>): number {
    if (depthCache.has(id)) return depthCache.get(id)!;
    if (visiting.has(id)) return 0;
    const deps = byId.get(id)?.dependsOn.filter((d) => byId.has(d)) ?? [];
    if (deps.length === 0) {
      depthCache.set(id, 0);
      return 0;
    }
    visiting.add(id);
    const depth = 1 + Math.max(...deps.map((d) => depthOf(d, visiting)));
    visiting.delete(id);
    depthCache.set(id, depth);
    return depth;
  }

  const layers = new Map<number, string[]>();
  for (const n of nodes) {
    const d = depthOf(n.id, new Set());
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d)!.push(n.id);
  }

  const positions = new Map<string, { x: number; y: number }>();
  for (const [depth, ids] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    ids.forEach((id, row) => {
      positions.set(id, {
        x: depth * (NODE_W + COL_GAP),
        y: row * (NODE_H + ROW_GAP),
      });
    });
  }
  return positions;
}
