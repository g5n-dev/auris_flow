import type { PointerEvent as ReactPointerEvent } from "react";

import type { CanvasIntentKey, CanvasNode, CanvasNodePosition } from "../types";

export function ConnectorNode({
  node,
  selected,
  activeIntentKey,
  position,
  dragging,
  dragHandlers,
  onSelect
}: {
  node: CanvasNode;
  selected: boolean;
  activeIntentKey: CanvasIntentKey;
  position: CanvasNodePosition;
  dragging: boolean;
  dragHandlers: {
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
    onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
    onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void;
    onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void;
  };
  onSelect: () => void;
}) {
  const Icon = node.icon;
  const inIntent = node.intentKeys.includes(activeIntentKey);
  return (
    <button
      className={["connector-node", "canvas-draggable", selected ? "selected" : "", inIntent ? "in-intent" : "muted", dragging ? "dragging" : ""].join(" ")}
      style={{ left: position.x, top: position.y }}
      {...dragHandlers}
      onClick={onSelect}
    >
      <div className="connector-node-head">
        <span>
          <Icon size={19} />
          {node.name}
        </span>
        <i className={node.active ? "live" : ""} />
      </div>
      <div className="connector-node-body">
        <span>{node.metaA}</span>
        <strong>{node.metaB}</strong>
        <span>状态</span>
        <strong>{node.status}</strong>
        <span>职责</span>
        <strong>{node.role}</strong>
        <span>置信度</span>
        <strong>{node.confidence}%</strong>
      </div>
      <div className="connector-node-foot">
        <span>{inIntent ? "当前任务引用" : "可被其他任务复用"}</span>
        <b>{inIntent ? "本版本内配置" : "未启用"}</b>
      </div>
    </button>
  );
}
