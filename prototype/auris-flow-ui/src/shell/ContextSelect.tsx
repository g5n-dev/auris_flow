import { ChevronDown, ShieldCheck } from "lucide-react";

export function ContextSelect({
  label,
  value,
  active,
  locked,
  onClick
}: {
  label: string;
  value: string;
  active?: boolean;
  locked?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      className={[active ? "context-select active" : "context-select", locked ? "locked" : ""].filter(Boolean).join(" ")}
      aria-expanded={locked ? undefined : active}
      title={locked ? "租户是全局隔离边界，点击进入租户管理" : undefined}
      onClick={onClick}
    >
      <span>{label}</span>
      <strong>{value}</strong>
      {locked ? <ShieldCheck size={14} /> : <ChevronDown size={14} />}
    </button>
  );
}
