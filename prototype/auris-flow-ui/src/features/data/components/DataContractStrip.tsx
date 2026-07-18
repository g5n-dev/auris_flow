import { dataDagsterContracts } from "../fixtures";
import type { DataWorkspace } from "../useDataWorkspace";

export function DataContractStrip({ workspace }: { workspace: DataWorkspace }) {
  const { isContractCollapsed } = workspace;
  return (
    <>
      {!isContractCollapsed && (
        <section className="data-dagster-strip" aria-label="数据资产底层兼容契约">
          {dataDagsterContracts.map((contract) => (
            <button key={contract.endpoint} type="button" className={contract.state === "需人工确认" ? "data-dagster-card attention" : "data-dagster-card"}>
              <span>
                <b>{contract.method}</b>
                {contract.label}
              </span>
              <strong>{contract.endpoint}</strong>
              <em>{contract.mapsTo}</em>
              <i>{contract.state}</i>
            </button>
          ))}
        </section>
      )}
    </>
  );
}
