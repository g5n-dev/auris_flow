import { Link2 } from "lucide-react";

import type { ModuleDeepLink, ModuleKey } from "../contracts/navigation";

export function DeepLinkSourceBar({
  target,
  onBack,
  getModuleTitle
}: {
  target: ModuleDeepLink;
  onBack?: () => void;
  getModuleTitle: (module: ModuleKey) => string;
}) {
  return (
    <section className="deep-link-source-bar" aria-label="当前关联详情来源">
      <div>
        <Link2 size={14} />
        <span>来自：{target.origin?.label ?? "关联跳转"}</span>
        <strong>{target.title ?? target.objectId ?? getModuleTitle(target.module)}</strong>
        <em>{target.detail ?? `${target.objectKind ?? "module"}:${target.objectId ?? target.module}`}</em>
      </div>
      <div>
        {target.objectId && <code>{target.objectId}</code>}
        {onBack && (
          <button type="button" onClick={onBack}>
            返回{target.origin?.module ? getModuleTitle(target.origin.module) : "上个上下文"}
          </button>
        )}
      </div>
    </section>
  );
}
