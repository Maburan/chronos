import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  MarkerType,
  applyEdgeChanges,
  applyNodeChanges,
  addEdge,
  type Node,
  type Edge,
  type Connection,
  type EdgeChange,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  ApiError,
  getWorkflow,
  getWorkflowDefinition,
  saveWorkflowDefinition,
  type TaskDefinition,
  type TaskDefinitionInput,
  type TaskDefinitionDependencyInput,
  type TaskTypeInfo,
  type Workflow,
} from "../api";
import { describeError, type DisplayError } from "../errors";
import { ErrorBanner } from "../components/ErrorBanner";
import { StepNode, type StepNodeData } from "../components/builder/StepNode";
import { StartNode } from "../components/builder/StartNode";
import { StepPicker } from "../components/builder/StepPicker";
import { NodeInspector } from "../components/builder/NodeInspector";
import { autoLayout, wouldCreateCycle } from "../components/builder/graphUtils";
import { useTaskTypes, taskTypeInfo } from "../useTaskTypes";
import { defaultFieldValues, fieldValuesFromPayload, buildPayload } from "../taskFieldForm";
import { useToast } from "../components/toast";
import { ArrowLeftIcon, PlusIcon, SaveIcon, CheckIcon, SparkIcon } from "../components/icons";

const START_ID = "__start__";

interface BuilderNodeData extends StepNodeData {
  fieldValues: Record<string, unknown>;
  maxAttempts: number;
}

type BuilderNode = Node<BuilderNodeData>;

const nodeTypes = { step: StepNode, start: StartNode };

function computeIncomplete(info: TaskTypeInfo | undefined, fieldValues: Record<string, unknown>): boolean {
  if (!info) return false;
  return info.fields.some((f) => f.required && !fieldValues[f.name] && fieldValues[f.name] !== 0);
}

function makeStartNode(y: number): BuilderNode {
  return {
    id: START_ID,
    type: "start",
    position: { x: -260, y },
    draggable: false,
    selectable: false,
    connectable: false,
    data: {
      name: "Start",
      icon: "",
      typeLabel: "",
      taskType: "",
      fieldValues: {},
      maxAttempts: 1,
      incomplete: false,
    },
  };
}

function buildGraphFromDefinition(
  tasks: TaskDefinition[],
  taskTypes: TaskTypeInfo[]
): { nodes: BuilderNode[]; edges: Edge[] } {
  const missingPosition = tasks.filter((t) => t.position_x == null || t.position_y == null);
  const layout = autoLayout(missingPosition.map((t) => ({ id: String(t.id), dependsOn: t.depends_on_ids.map(String) })));

  const nodes: BuilderNode[] = tasks.map((t) => {
    const info = taskTypeInfo(taskTypes, t.task_type);
    const fieldValues = info ? fieldValuesFromPayload(info, t.payload) : {};
    const pos =
      t.position_x != null && t.position_y != null
        ? { x: t.position_x, y: t.position_y }
        : layout.get(String(t.id)) ?? { x: 0, y: 0 };
    return {
      id: String(t.id),
      type: "step",
      position: pos,
      data: {
        name: t.name,
        icon: info?.icon ?? "▢",
        typeLabel: info?.label ?? t.task_type,
        taskType: t.task_type,
        fieldValues,
        maxAttempts: t.max_attempts,
        incomplete: computeIncomplete(info, fieldValues),
      },
    };
  });

  const edges: Edge[] = tasks.flatMap((t) =>
    t.depends_on_ids.map((depId) => ({
      id: `e-${depId}-${t.id}`,
      source: String(depId),
      target: String(t.id),
    }))
  );

  const startY = nodes.length > 0 ? (Math.min(...nodes.map((n) => n.position.y)) + Math.max(...nodes.map((n) => n.position.y))) / 2 : 0;
  return { nodes: [makeStartNode(startY), ...nodes], edges };
}

function BuilderCanvas({ workflowId }: { workflowId: number }) {
  const taskTypes = useTaskTypes();
  const toast = useToast();
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<DisplayError | null>(null);

  const [nodes, setNodes] = useState<BuilderNode[]>([makeStartNode(0)]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<DisplayError | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [shakeIncomplete, setShakeIncomplete] = useState(false);

  useEffect(() => {
    getWorkflow(workflowId)
      .then(setWorkflow)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
        else setLoadError(describeError(err));
      });
  }, [workflowId]);

  const reload = useCallback(() => {
    if (taskTypes.length === 0) return Promise.resolve();
    return getWorkflowDefinition(workflowId).then((def) => {
      const built = buildGraphFromDefinition(def.tasks, taskTypes);
      setNodes(built.nodes);
      setEdges(built.edges);
      setLoading(false);
    });
  }, [workflowId, taskTypes]);

  useEffect(() => {
    if (!workflow || taskTypes.length === 0) return;
    reload().catch((err) => {
      setLoadError(describeError(err));
      setLoading(false);
    });
    // Only re-run when the workflow or the task-type catalog first become
    // available -- `reload` is also called explicitly after a save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow, taskTypes.length]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => !(c.type === "remove" && c.id === START_ID));
    setNodes((nds) => applyNodeChanges(filtered, nds) as BuilderNode[]);
    const removedIds = filtered.filter((c) => c.type === "remove").map((c) => c.id);
    if (removedIds.length > 0) {
      setEdges((eds) => eds.filter((e) => !removedIds.includes(e.source) && !removedIds.includes(e.target)));
      setSelectedNodeId((sel) => (sel && removedIds.includes(sel) ? null : sel));
    }
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    const realChanges = changes.filter((c) => !("id" in c) || !c.id.startsWith("start-"));
    if (realChanges.length === 0) return;
    setEdges((eds) => applyEdgeChanges(realChanges, eds));
  }, []);

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return;
      if (connection.source === START_ID) return;
      if (connection.source === connection.target) {
        setSaveError({ title: "That's not a valid connection", detail: "A step can't run after itself." });
        return;
      }
      const already = edges.some((e) => e.source === connection.source && e.target === connection.target);
      if (already) return;
      const simple = edges.map((e) => ({ source: e.source, target: e.target }));
      if (wouldCreateCycle(simple, { source: connection.source, target: connection.target })) {
        setSaveError({
          title: "That would create a loop",
          detail: "A step can't depend on something that (directly or indirectly) depends on it.",
        });
        return;
      }
      setSaveError(null);
      setEdges((eds) =>
        addEdge(
          { ...connection, markerEnd: { type: MarkerType.ArrowClosed, color: "var(--accent)" }, style: { stroke: "var(--accent)" } },
          eds
        )
      );
    },
    [edges]
  );

  const renderedNodes = useMemo(
    () =>
      nodes.map((n) => ({
        ...n,
        selected: n.id === selectedNodeId,
        data: shakeIncomplete && n.data.incomplete ? { ...n.data, shake: true } : n.data,
      })),
    [nodes, selectedNodeId, shakeIncomplete]
  );

  const displayEdges = useMemo(() => {
    const realNodeIds = nodes.filter((n) => n.id !== START_ID).map((n) => n.id);
    const hasIncoming = new Set(edges.map((e) => e.target));
    const synthetic: Edge[] = realNodeIds
      .filter((id) => !hasIncoming.has(id))
      .map((id) => ({
        id: `start-${id}`,
        source: START_ID,
        target: id,
        selectable: false,
        deletable: false,
        style: { stroke: "var(--text-faint)", strokeDasharray: "4 4" },
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--text-faint)" },
      }));
    return [...synthetic, ...edges];
  }, [nodes, edges]);

  function openPicker() {
    setPickerOpen(true);
  }

  function handlePickType(info: TaskTypeInfo) {
    const realNodes = nodes.filter((n) => n.id !== START_ID);
    const maxX = realNodes.length > 0 ? Math.max(...realNodes.map((n) => n.position.x)) : 0;
    const id = crypto.randomUUID();
    const fieldValues = defaultFieldValues(info);
    const newNode: BuilderNode = {
      id,
      type: "step",
      position: { x: maxX + 320, y: 0 },
      data: {
        name: "",
        icon: info.icon ?? "▢",
        typeLabel: info.label,
        taskType: info.type,
        fieldValues,
        maxAttempts: 1,
        incomplete: computeIncomplete(info, fieldValues),
      },
    };
    setNodes((nds) => [...nds, newNode]);
    setPickerOpen(false);
    setSelectedNodeId(id);
  }

  function updateSelectedNode(updater: (data: BuilderNodeData) => BuilderNodeData) {
    if (!selectedNodeId) return;
    setNodes((nds) => nds.map((n) => (n.id === selectedNodeId ? { ...n, data: updater(n.data) } : n)));
  }

  function handleDeleteSelected() {
    if (!selectedNodeId) return;
    setNodes((nds) => nds.filter((n) => n.id !== selectedNodeId));
    setEdges((eds) => eds.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId));
    setSelectedNodeId(null);
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);

    const realNodes = nodes.filter((n) => n.id !== START_ID);
    const incompleteNode = realNodes.find((n) => n.data.incomplete);
    if (incompleteNode) {
      setSaveError({
        title: "Some steps need more information",
        detail: `Finish configuring "${incompleteNode.data.name || "Untitled step"}" before saving.`,
      });
      setSelectedNodeId(incompleteNode.id);
      setShakeIncomplete(true);
      window.setTimeout(() => setShakeIncomplete(false), 500);
      setSaving(false);
      return;
    }

    let tasks: TaskDefinitionInput[];
    try {
      tasks = realNodes.map((n) => {
        const info = taskTypeInfo(taskTypes, n.data.taskType);
        const { payload } = info ? buildPayload(info, n.data.fieldValues) : { payload: {} };
        return {
          client_id: n.id,
          name: n.data.name.trim() || "Untitled step",
          task_type: n.data.taskType,
          max_attempts: n.data.maxAttempts,
          payload: Object.keys(payload).length > 0 ? payload : null,
          position_x: n.position.x,
          position_y: n.position.y,
        };
      });
    } catch {
      setSaveError({ title: "Invalid value", detail: "One of the advanced fields isn't valid JSON." });
      setSaving(false);
      return;
    }

    const dependencies: TaskDefinitionDependencyInput[] = edges.map((e) => ({
      task_client_id: e.target,
      depends_on_client_id: e.source,
    }));

    try {
      await saveWorkflowDefinition(workflowId, tasks, dependencies);
      await reload();
      setSelectedNodeId(null);
      setSaveMessage("Saved");
      toast.success("Workflow saved");
      window.setTimeout(() => setSaveMessage(null), 2400);
    } catch (err) {
      setSaveError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  if (notFound) {
    return (
      <div className="canvas-page">
        <div className="canvas-prompt">
          <div className="float canvas-prompt-card">
            <div className="empty-icon">
              <ArrowLeftIcon />
            </div>
            <div className="empty-title">Workflow not found</div>
            <p className="empty-detail">It may belong to another account, or no longer exists.</p>
            <Link to="/workflows" className="empty-action">
              <button type="button" className="btn">
                <ArrowLeftIcon /> Back to workflows
              </button>
            </Link>
          </div>
        </div>
      </div>
    );
  }

  const selectedNode = nodes.find((n) => n.id === selectedNodeId);
  const selectedInfo = selectedNode ? taskTypeInfo(taskTypes, selectedNode.data.taskType) : undefined;
  const dependencyNames = selectedNode
    ? edges
        .filter((e) => e.target === selectedNode.id)
        .map((e) => nodes.find((n) => n.id === e.source)?.data.name || "Untitled step")
    : [];

  const realNodeCount = nodes.filter((n) => n.id !== START_ID).length;
  const incompleteCount = nodes.filter((n) => n.id !== START_ID && n.data.incomplete).length;

  return (
    <div className="canvas-page">
      {!workflow || loading ? (
        <div className="canvas-prompt">
          <div className="float canvas-prompt-card">
            <div className="empty-icon">
              <span className="health-dot checking" style={{ width: 10, height: 10 }} />
            </div>
            <div className="empty-title">Loading builder…</div>
          </div>
        </div>
      ) : (
        <>
          <div className="builder-canvas">
            {realNodeCount === 0 && (
              <div className="canvas-prompt">
                <div className="float canvas-prompt-card">
                  <div className="empty-icon">
                    <SparkIcon />
                  </div>
                  <div className="empty-title">Start building</div>
                  <p className="empty-detail">Click the Start node or “Add step” to drop your first step onto the canvas.</p>
                </div>
              </div>
            )}
            <ReactFlow
              nodes={renderedNodes}
              edges={displayEdges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => {
                if (node.id === START_ID) openPicker();
                else setSelectedNodeId(node.id);
              }}
              onPaneClick={() => setSelectedNodeId(null)}
              fitView
              fitViewOptions={{ padding: 0.4, maxZoom: 1 }}
              deleteKeyCode={["Backspace", "Delete"]}
              defaultEdgeOptions={{
                markerEnd: { type: MarkerType.ArrowClosed, color: "var(--accent)", width: 18, height: 18 },
                style: { stroke: "var(--accent)", strokeWidth: 1.75 },
              }}
              connectionLineStyle={{ stroke: "var(--accent)", strokeWidth: 2 }}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={26} size={1.4} color="color-mix(in srgb, var(--line-strong) 55%, transparent)" />
              <Controls showInteractive={false} position="bottom-right" />
              <MiniMap
                pannable
                zoomable
                position="bottom-left"
                style={{ background: "color-mix(in srgb, var(--surface-1) 80%, transparent)" }}
                maskColor="color-mix(in srgb, var(--bg) 62%, transparent)"
                nodeColor="var(--surface-3)"
                nodeStrokeColor="var(--line-strong)"
              />
            </ReactFlow>
          </div>

          {/* floating header */}
          <div className="canvas-header">
            <div className="float canvas-crumb">
              <div className="crumb-path">
                <Link to="/workflows">Workflows</Link>
                <span className="sep">/</span>
                <Link to={`/workflows/${workflowId}`}>{workflow.name}</Link>
              </div>
              <div className="crumb-title">Builder</div>
              <div className="canvas-status">
                <span className="tnum">
                  {realNodeCount} step{realNodeCount === 1 ? "" : "s"}
                </span>
                <span className="sep">·</span>
                <span>drag between steps to connect</span>
              </div>
            </div>

            <div className="canvas-actions">
              {incompleteCount > 0 && (
                <span className={`badge badge-failed${shakeIncomplete ? " shake" : ""}`} title="Some steps need required fields before saving">
                  {incompleteCount} incomplete
                </span>
              )}
              {saveMessage && (
                <span className="save-flash">
                  <CheckIcon /> {saveMessage}
                </span>
              )}
              <button type="button" className="btn sm" onClick={openPicker}>
                <PlusIcon /> Add step
              </button>
              <button type="button" className="btn primary sm" onClick={handleSave} disabled={saving}>
                <SaveIcon /> {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>

          {(loadError || saveError) && (
            <div style={{ position: "absolute", bottom: 16, left: "50%", transform: "translateX(-50%)", zIndex: 20, width: "min(520px, calc(100% - 32px))" }}>
              <div className="float" style={{ padding: 2 }}>
                <ErrorBanner
                  error={(saveError || loadError)!}
                  onDismiss={() => {
                    setSaveError(null);
                    setLoadError(null);
                  }}
                />
              </div>
            </div>
          )}

          {selectedNode && (
            <div className="float canvas-inspector">
              <NodeInspector
                node={{
                  id: selectedNode.id,
                  name: selectedNode.data.name,
                  taskType: selectedNode.data.taskType,
                  fieldValues: selectedNode.data.fieldValues,
                  maxAttempts: selectedNode.data.maxAttempts,
                }}
                info={selectedInfo}
                dependencyNames={dependencyNames}
                onNameChange={(name) => updateSelectedNode((d) => ({ ...d, name }))}
                onFieldChange={(fieldName, value) =>
                  updateSelectedNode((d) => {
                    const fieldValues = { ...d.fieldValues, [fieldName]: value };
                    return { ...d, fieldValues, incomplete: computeIncomplete(selectedInfo, fieldValues) };
                  })
                }
                onMaxAttemptsChange={(value) => updateSelectedNode((d) => ({ ...d, maxAttempts: value }))}
                onDelete={handleDeleteSelected}
                onClose={() => setSelectedNodeId(null)}
              />
            </div>
          )}
        </>
      )}

      {pickerOpen && <StepPicker taskTypes={taskTypes} onPick={handlePickType} onClose={() => setPickerOpen(false)} />}
    </div>
  );
}

export function WorkflowBuilderPage() {
  const { id } = useParams<{ id: string }>();
  const workflowId = Number(id);
  if (!Number.isFinite(workflowId)) return null;

  return (
    <ReactFlowProvider>
      <BuilderCanvas workflowId={workflowId} />
    </ReactFlowProvider>
  );
}
