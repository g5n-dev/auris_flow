import type { ReactNode } from "react";

export function PanelHeader({
  title,
  subtitle,
  icon,
  sticky = false,
  className = ""
}: {
  title: string;
  subtitle: string;
  icon: ReactNode;
  sticky?: boolean;
  className?: string;
}) {
  return (
    <div className={["module-panel-head", sticky ? "sticky-panel-head" : "", className].filter(Boolean).join(" ")}>
      <div>
        <span>{title}</span>
        <strong>{subtitle}</strong>
      </div>
      <i>{icon}</i>
    </div>
  );
}
