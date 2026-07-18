import { Metric } from "../../../../shared/ui/Metric";
import { matrixTimes } from "../../fixtures/evidenceFixtures";
import { EvidenceList } from "../evidence/trackDisplays";
import type { MatrixModeController } from "./matrixActions";
import type { CSSProperties } from "react";

export function MatrixModeView({ controller }: { controller: MatrixModeController }) {
  const { activeCell, activeDimension, activeDimensionKey, activeFinding, activeRow, activeTime, commitMatrixAction, currentFilterLabel, findingByKey, getCell, matrixDimensions, matrixGridStyle, onlyAnomalies, pickCell, pickRow, previewCurrentSegment, previewState, resetMatrixFilters, reviewReceipt, selectMatrixDimension, selectedWindow, setMode, setShowFullMatrix, showFullMatrix, statusCounts, statusFilter, statusFilters, toggleOnlyAnomalies, toggleStatusFilter, visibleCols, visibleRows } = controller;
  return (
    (
        <div className={`matrix-page ${showFullMatrix ? "full" : "compact"}`}>
          <section className="matrix-main">
            <div className="section-head">
              <div>
                <span>审音矩阵</span>
                <strong>{activeDimension.route}</strong>
              </div>
              <div className="matrix-head-actions">
                <button className={onlyAnomalies ? "active" : ""} aria-pressed={onlyAnomalies} onClick={toggleOnlyAnomalies}>
                  只看异常
                </button>
                <button onClick={() => setShowFullMatrix((current) => !current)}>
                  {showFullMatrix ? "收起矩阵" : "展开完整矩阵"}
                </button>
                <button onClick={() => setMode("evidence")}>返回证据审查</button>
              </div>
            </div>
            <div className="matrix-context-bar">
              <div>
                <span>当前审查上下文</span>
                <strong>{selectedWindow} · {activeDimension.label}路径 · {currentFilterLabel}</strong>
                <em>
                  选中 {activeTime} / {activeRow.label} / {activeFinding.label}，{reviewReceipt}
                </em>
              </div>
              <button onClick={resetMatrixFilters}>重置筛选</button>
            </div>
            <div className="groupbar">
              {matrixDimensions.map((dimension) => (
                <button
                  key={dimension.key}
                  className={activeDimensionKey === dimension.key ? "active" : ""}
                  onClick={() => selectMatrixDimension(dimension)}
                >
                  {dimension.label}
                </button>
              ))}
            </div>
            <div className="matrix-route-hint">
              <strong>{activeDimension.label}审查路径</strong>
              <span>{activeDimension.route}</span>
              <em>{activeDimension.summary}</em>
            </div>
            <div className="matrix-summary">
              <button className={onlyAnomalies && !statusFilter ? "hot active" : "hot"} onClick={toggleOnlyAnomalies}>
                {showFullMatrix ? "完整矩阵" : "当前窗口"} · {onlyAnomalies ? "异常优先" : "含背景"}
              </button>
              {statusFilters.map((item) => (
                <button key={item.key} className={statusFilter === item.key ? "active" : ""} onClick={() => toggleStatusFilter(item.key)}>
                  {item.label} {statusCounts[item.key] ?? 0}
                </button>
              ))}
            </div>
            <div className="matrix-legend">
              {[
                ["main", "主录音"],
                ["danger", "串音候选"],
                ["candidate", "同窗候选"],
                ["low", "低置信"],
                ["missing", "缺失"]
              ].map(([tone, label]) => (
                <span key={tone} className={`mx-legend ${tone}`}>
                  <i />
                  {label}
                </span>
              ))}
            </div>
            <div className="matrix-scroll" style={matrixGridStyle}>
              <div className="matrix-time" style={matrixGridStyle}>
                <span />
                {visibleCols.map((col) => (
                  <b key={matrixTimes[col]}>{matrixTimes[col]}</b>
                ))}
              </div>
              <div className="matrix-grid">
                {visibleRows.map((row) => (
                  <div className="matrix-row" key={row.id} style={matrixGridStyle}>
                    <button className={activeFinding.rowId === row.id ? "matrix-label active" : "matrix-label"} onClick={() => pickRow(row.id)} title="选择该设备下最高优先级异常">
                      <strong>{row.label}</strong>
                      <span>{row.meta}</span>
                    </button>
                    {visibleCols.map((col) => {
                      const cell = getCell(row.id, col);
                      const hasFinding = Boolean(findingByKey[cell.key]);
                      const selected = activeCell === cell.key;
                      const activeCol = activeFinding.col === col;
                      const filteredOut = Boolean(statusFilter && cell.status !== statusFilter) || (onlyAnomalies && !hasFinding);
                      const level = `${Math.round(cell.score * 100)}%`;
                      return (
                        <button
                          key={col}
                          className={[
                            "matrix-cell",
                            cell.status,
                            hasFinding ? "actionable" : "background",
                            filteredOut ? "filtered-out" : "",
                            selected ? "selected" : "",
                            activeCol ? "active-col" : ""
                          ].join(" ")}
                          onClick={() => pickCell(row.id, col)}
                          title={`${row.label} · ${matrixTimes[col]} · ${cell.label} · ${level} · ${cell.reason}`}
                        >
                          <span className="mc-bars" style={{ "--score": cell.score } as CSSProperties}>
                            {Array.from({ length: Math.max(1, Math.min(4, cell.records)) }, (_, index) => (
                              <i key={index} />
                            ))}
                          </span>
                          <b>{cell.label}</b>
                          <em>{cell.overlap ? `${cell.overlap}m` : "空窗"}</em>
                          <small>{level}</small>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
            <div className={`matrix-inline-detail ${activeFinding.status}`}>
              <div className="matrix-inline-copy">
                <span>选中异常</span>
                <strong>
                  {activeRow.label} · {activeTime} · {activeFinding.label}
                </strong>
                <p>{activeFinding.reason}</p>
              </div>
              <div className="matrix-inline-facts">
                <span>
                  <b>{Math.round(activeFinding.score * 100)}%</b>
                  <em>置信度</em>
                </span>
                <span>
                  <b>{activeFinding.overlap ? `${activeFinding.overlap}m` : "空窗"}</b>
                  <em>重叠</em>
                </span>
                <span>
                  <b>{activeFinding.records}</b>
                  <em>证据数</em>
                </span>
              </div>
              <div className="matrix-inline-actions">
                <button onClick={previewCurrentSegment}>{previewState === "playing" ? "试听中" : "试听当前段"}</button>
                <button onClick={() => commitMatrixAction("main", "标为主录音")}>标为主录音</button>
                <button className="danger" onClick={() => commitMatrixAction("crosstalk", "标为串音")}>标为串音</button>
                <button onClick={() => commitMatrixAction("none", "排除")}>排除</button>
              </div>
            </div>
          </section>
          <aside className="matrix-side">
            <div className="metric-grid">
              <Metric label="串音候选" value={`${statusCounts.danger ?? 0}`} tone="danger" />
              <Metric label="缺失录音" value={`${statusCounts.missing ?? 0}`} tone="amber" />
              <Metric label="同窗候选" value={`${statusCounts.candidate ?? 0}`} tone="violet" />
              <Metric label="低置信" value={`${statusCounts.low ?? 0}`} tone="teal" />
            </div>
            <div className={`matrix-focus ${activeFinding.status}`}>
              <div className="mf-head">
                <span>{activeTime} · {activeRow.label}</span>
                <b>{Math.round(activeFinding.score * 100)}%</b>
              </div>
              <strong>{activeFinding.label}</strong>
              <p>{activeFinding.reason}</p>
              <div className="mf-score">
                <span>串音置信度</span>
                <i style={{ width: `${Math.round(activeFinding.score * 100)}%` }} />
              </div>
            </div>
            <div className="route-card matrix-chain">
              <strong>证据链</strong>
              {activeFinding.evidence.map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
            <div className="matrix-actions">
              <button onClick={() => commitMatrixAction("main", "标为主录音")}>
                标为主录音
              </button>
              <button className="danger" onClick={() => commitMatrixAction("crosstalk", "标为串音")}>
                标为串音
              </button>
              <button onClick={() => commitMatrixAction("none", "排除")}>
                排除
              </button>
            </div>
            <EvidenceList />
          </aside>
        </div>
      )
  );
}
