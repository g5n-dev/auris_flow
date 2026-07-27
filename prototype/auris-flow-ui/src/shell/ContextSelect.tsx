import { ChevronDown, ShieldCheck } from "lucide-react";

export function ContextSelect({
  label,
  value,
  active,
  locked,
  disabled,
  onClick
}: {
  label: string;
  value: string;
  active?: boolean;
  locked?: boolean;
  disabled?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      className={[active ? "context-select active" : "context-select", locked ? "locked" : ""].filter(Boolean).join(" ")}
      aria-expanded={locked ? undefined : active}
      title={locked ? "租户为身份隔离边界，当前会话只读" : undefined}
      disabled={disabled}
      onClick={onClick}
    >
      <span>{label}</span>
      <strong>{value}</strong>
      {locked ? <ShieldCheck size={14} /> : <ChevronDown size={14} />}
    </button>
  );
}
