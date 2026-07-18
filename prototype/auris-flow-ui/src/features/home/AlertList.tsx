import type { ModuleKey } from "../../shared/contracts/navigation";
import { homeAlerts } from "./fixtures";

export function AlertList({ setActiveModule }: { setActiveModule: (module: ModuleKey) => void }) {
  return (
    <div className="alert-list">
      {homeAlerts.map((alert) => (
        <button key={alert.title} className={`alert-row ${alert.tone}`} onClick={() => setActiveModule(alert.tone === "violet" ? "assets" : "listening")}>
          <strong>{alert.title}</strong>
          <span>{alert.meta}</span>
        </button>
      ))}
    </div>
  );
}
