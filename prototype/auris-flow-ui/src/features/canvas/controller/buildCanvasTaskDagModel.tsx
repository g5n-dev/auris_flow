import type { CanvasModuleProps } from "../types";
import type { CanvasState } from "./useCanvasState";
import type { CanvasPrimitiveActions } from "./buildCanvasPrimitiveActions";
import type { CanvasRecoveryModel } from "./useCanvasRecovery";
import type { CanvasSectionModel } from "./buildCanvasSectionModel";
import type { CanvasScheduleModel } from "./useCanvasScheduleModel";
import type { CanvasExecutionPlan } from "./buildCanvasExecutionPlan";
import type { CanvasRuntimeModel } from "./useCanvasRuntimeModel";
import type { CanvasNodeCollections } from "./buildCanvasNodeCollections";
import type { CanvasNodeContextModel } from "./buildCanvasNodeContextModel";
import type { CanvasNodeInteractions } from "./buildCanvasNodeInteractions";
import { canvasTaskDagDescriptors } from "../fixtures/viewDescriptors";
import type { CanvasIntentKey } from "../types";

type CanvasTaskDagNode = {
  id: string;
  column: string;
  label: string;
  asset: string;
  detail: string;
  kind: string;
  nodeId: string;
  intentKeys: CanvasIntentKey[];
  action?: () => void;
};

export function buildCanvasTaskDagModel(scope: CanvasModuleProps & CanvasState & CanvasPrimitiveActions & CanvasRecoveryModel & CanvasSectionModel & CanvasScheduleModel & CanvasExecutionPlan & CanvasRuntimeModel & CanvasNodeCollections & CanvasNodeContextModel & CanvasNodeInteractions) {
  const { activeIntent, appliedMappingCount, draftState, mappingTotal, openOutputSinkTemplate, scheduleMode, selectedCanvasVariant, selectedNodeId, selectedTaskType, setActiveTab, setCanvasLevel, setDrawerTab, setSelectedNodeId } = scope;
  const taskDagIntentKey = selectedTaskType.intentKey;
  const taskDagNodes: CanvasTaskDagNode[] = canvasTaskDagDescriptors.nodes.map((descriptor) => {
    const { actionKey, ...staticFields } = descriptor;
    const asset = descriptor.id === "dagster" ? `${activeIntent.taskId}_plan` : descriptor.asset ?? "";
    const detail = descriptor.id === "ai"
      ? `${appliedMappingCount}/${mappingTotal} 已应用`
      : descriptor.id === "dagster"
        ? `${selectedCanvasVariant.name} / ${scheduleMode}`
        : descriptor.detail ?? "";
    const node: CanvasTaskDagNode = {
      ...staticFields,
      asset,
      detail,
      intentKeys: [...descriptor.intentKeys]
    };
    if (actionKey) node.action = () => openOutputSinkTemplate(actionKey);
    return node;
  });
  const taskDagColumns = canvasTaskDagDescriptors.columns.map((column) => ({ ...column }));
  const taskDagVisibleNodes = taskDagNodes.filter((node) => node.intentKeys.includes(taskDagIntentKey));
  const taskDagVisibleNodeIds = new Set(taskDagVisibleNodes.map((node) => node.id));
  const taskDagNodeById = Object.fromEntries(taskDagNodes.map((node) => [node.id, node]));
  const taskDagEdges = canvasTaskDagDescriptors.edges
    .map(([from, to]): [string, string] => [from, to])
    .filter(([from, to]) => taskDagVisibleNodeIds.has(from) && taskDagVisibleNodeIds.has(to));

  const renderTaskDagSnapshot = (variant: "release" | "embedded") => (
    <section className={`task-tab-card wide task-dag-snapshot-card ${variant}`}>
      <div className="task-tab-card-head">
        <span>{variant === "release" ? "发布编排快照" : "当前编排图"}</span>
        <button
          type="button"
          onClick={() => {
            setActiveTab("flow");
            setCanvasLevel("nodes");
          }}
        >
          查看节点画布
        </button>
      </div>
      <div className="task-dag-summary">
        <strong>{selectedTaskType.name}</strong>
        <p>
          {selectedCanvasVariant.version} · {taskDagVisibleNodes.length} 个可执行节点 · {taskDagEdges.length} 条依赖边 · {selectedCanvasVariant.traffic}
        </p>
        <b>{draftState}</b>
      </div>
      <div className="task-dag-board" aria-label="任务编排依赖图">
        {taskDagColumns.map((column, columnIndex) => (
          <div key={column.id} className="task-dag-column">
            <div className="task-dag-column-head">
              <span>{column.label}</span>
              <em>{column.hint}</em>
            </div>
            {taskDagVisibleNodes
              .filter((node) => node.column === column.id)
              .map((node) => (
                <button
                  key={node.id}
                  type="button"
                  className={selectedNodeId === node.nodeId ? "task-dag-node selected" : "task-dag-node"}
                  data-kind={node.kind}
                  onClick={() => {
                    setSelectedNodeId(node.nodeId);
                    setDrawerTab(node.kind === "output" || node.kind === "human" ? "plan" : "overview");
                    node.action?.();
                  }}
                >
                  <strong>{node.label}</strong>
                  <span>{node.asset}</span>
                  <em>{node.detail}</em>
                </button>
              ))}
            {columnIndex < taskDagColumns.length - 1 && <i aria-hidden="true" />}
          </div>
        ))}
      </div>
      <div className="task-dag-edge-strip" aria-label="当前依赖边">
        {taskDagEdges.map(([from, to]) => (
          <button key={`${from}-${to}`} type="button">
            <span>{taskDagNodeById[from]?.label}</span>
            <b>→</b>
            <span>{taskDagNodeById[to]?.label}</span>
          </button>
        ))}
      </div>
    </section>
  );

  const renderTaskAssetCanvas = () => {
    const assetGroups = [
      { id: "input", title: "数据抽取", subtitle: "外部数据源", nodes: taskDagVisibleNodes.filter((node) => node.column === "input") },
      { id: "map", title: "聚合关联", subtitle: "Entity / Event join", nodes: taskDagVisibleNodes.filter((node) => node.column === "map" || node.column === "plan") },
      { id: "model", title: "智能处理", subtitle: "VAD / Diar / ASR / Tagger", nodes: taskDagVisibleNodes.filter((node) => node.column === "model") },
      { id: "output", title: "输出资产", subtitle: "资产生成记录 / 回写目标", nodes: taskDagVisibleNodes.filter((node) => node.column === "output") }
    ];
    return (
      <section className="task-tab-card wide task-asset-canvas-card">
        <div className="task-tab-card-head">
          <span>数据处理 / 数据血缘 / 聚合关联画布</span>
          <button
            type="button"
            onClick={() => {
              setActiveTab("flow");
              setCanvasLevel("nodes");
            }}
          >
            打开节点画布
          </button>
        </div>
        <p>
          输入不是扁平字段，输出也不是 CSV 字符串；每一步都落到外部数据源或处理资产，按租户、门店、日期和业务对象聚合，运行时保留 partition、run_id、materialization_id 和 trace_id。
        </p>
        <div className="task-asset-canvas-board" aria-label="任务资产血缘画布">
          {assetGroups.map((group, index) => (
            <div key={group.id} className="task-asset-lane">
              <div className="task-asset-lane-head">
                <strong>{group.title}</strong>
                <span>{group.subtitle}</span>
              </div>
              {group.nodes.map((node) => (
                <button
                  key={node.id}
                  type="button"
                  className="task-asset-node"
                  data-kind={node.kind}
                  onClick={() => {
                    setSelectedNodeId(node.nodeId);
                    setDrawerTab(node.kind === "source" ? "overview" : "plan");
                    node.action?.();
                  }}
                >
                  <b>{node.label}</b>
                  <strong>{node.asset}</strong>
                  <em>{node.detail}</em>
                </button>
              ))}
              {index < assetGroups.length - 1 && <i aria-hidden="true" />}
            </div>
          ))}
        </div>
        <div className="task-aggregate-strip" aria-label="聚合键">
          {["tenant_id", "store_id", "business_date", "recording_id", "employee_id", "event_id", "task_run_id"].map((key) => (
            <span key={key}>{key}</span>
          ))}
        </div>
      </section>
    );
  };

  return {
    taskDagIntentKey,
    taskDagNodes,
    taskDagColumns,
    taskDagVisibleNodes,
    taskDagVisibleNodeIds,
    taskDagNodeById,
    taskDagEdges,
    renderTaskDagSnapshot,
    renderTaskAssetCanvas
  };
}

export type CanvasTaskDagModel = ReturnType<typeof buildCanvasTaskDagModel>;
