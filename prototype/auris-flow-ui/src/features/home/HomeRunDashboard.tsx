import { Activity, RotateCcw } from "lucide-react";
import { Fragment, type CSSProperties } from "react";

import type { ModuleKey } from "../../shared/contracts/navigation";
import { PanelHeader } from "../../shared/ui/PanelHeader";
import {
  homeRunFailureHeatmap,
  homeRunOverview,
  homeRunQueues,
  homeRunTimeline,
  homeRunTrendLabels,
  homeRunTrendSeries,
  type HomeRunTrendKey
} from "./fixtures";
import { HomeRunSparkline, HomeRunTrendChart } from "./HomeRunTrendChart";

export function HomeRunDashboard({
  activeRunTrendKey,
  activeRunTrendPoint,
  setActiveRunTrendKey,
  setActiveRunTrendPoint,
  setActiveModule
}: {
  activeRunTrendKey: HomeRunTrendKey;
  activeRunTrendPoint: number;
  setActiveRunTrendKey: (key: HomeRunTrendKey) => void;
  setActiveRunTrendPoint: (index: number) => void;
  setActiveModule: (module: ModuleKey) => void;
}) {
  return (
    <section className="module-panel home-run-panel home-focus-panel">
      <PanelHeader title="运行大盘" subtitle="任务运行、队列、失败分区和 SLA 一屏可下钻" icon={<Activity size={16} />} />
      <div className="home-run-board">
        <div className="home-run-left">
          <div className="home-run-hero">
            <span>当前运行健康度</span>
            <strong>91</strong>
            <p>TaskVersion v3.2.1 · 15 个节点 · 21 条依赖边 · 最近 24 小时</p>
            <div>
              <b>run_key</b>
              <em>sales_qa_daily:aurora:20250526</em>
            </div>
          </div>
          <div className="home-run-gate-card">
            <div className="home-run-subhead">
              <span>运行门禁</span>
              <em>3 项可下钻</em>
            </div>
            {[
              ["输入资产", "96%", "录音、事件、单据已对齐", "green", "data"],
              ["队列延迟", "3m", "人工网关 317 条待复核", "amber", "listening"],
              ["审计链路", "已写入", "trace / run_key / outbox 已绑定", "blue", "canvas"]
            ].map(([label, value, detail, tone, route]) => (
              <button key={label} type="button" className={`home-run-gate-row ${tone}`} onClick={() => setActiveModule(route as ModuleKey)}>
                <span>{label}</span>
                <strong>{value}</strong>
                <em>{detail}</em>
              </button>
            ))}
          </div>
          <div className="home-run-quick-card">
            <button type="button" onClick={() => setActiveModule("assets")}>失败分区处理</button>
            <button type="button" onClick={() => setActiveModule("evaluation")}>回流评测集</button>
            <button type="button" onClick={() => setActiveModule("canvas")}>查看运行画布</button>
          </div>
        </div>
        <div className="home-run-main">
          <div className="home-run-metrics">
            {homeRunOverview.map((item) => (
              <button key={item.label} type="button" className={`home-run-metric ${item.tone}`} onClick={() => setActiveModule(item.route)}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                <em>{item.detail}</em>
              </button>
            ))}
          </div>
          <div className="home-run-analytics">
            <HomeRunTrendChart
              activeKey={activeRunTrendKey}
              activePointIndex={activeRunTrendPoint}
              onSelectSeries={setActiveRunTrendKey}
              onSelectPoint={setActiveRunTrendPoint}
              onNavigate={setActiveModule}
            />
            <div className="home-run-ops-grid">
              <div className="home-run-stage-sparks">
                <div className="home-run-subhead">
                  <span>阶段吞吐曲线</span>
                  <em>最近 7 个窗口</em>
                </div>
                {homeRunQueues.map((queue) => {
                  const series = homeRunTrendSeries.find((item) => item.route === queue.route);
                  return (
                    <button
                      key={queue.label}
                      type="button"
                      className={`home-run-queue-row ${queue.tone}`}
                      style={{ "--run-progress": `${queue.percent}%` } as CSSProperties}
                      onClick={() => setActiveModule(queue.route)}
                    >
                      <span>{queue.label}</span>
                      <strong>{queue.value}</strong>
                      <em>{queue.detail}</em>
                      <HomeRunSparkline values={queue.trend} color={series?.color ?? "#165dff"} />
                      <i />
                    </button>
                  );
                })}
              </div>
              <div className="home-run-failure-heatmap">
                <div className="home-run-subhead">
                  <span>失败分区热力</span>
                  <em>点击下钻处理</em>
                </div>
                <div className="home-run-heatmap-grid">
                  <span />
                  {homeRunTrendLabels.slice(1).map((label) => (
                    <b key={label}>{label.replace(":00", "")}</b>
                  ))}
                  {homeRunFailureHeatmap.map((row) => (
                    <Fragment key={row.label}>
                      <button type="button" className="row-label" onClick={() => setActiveModule(row.route)}>{row.label}</button>
                      {row.values.slice(1).map((value, index) => (
                        <button
                          key={`${row.label}-${index}`}
                          type="button"
                          className={value >= 3 ? "hot" : value >= 1 ? "warm" : ""}
                          style={{ "--heat": `${Math.min(1, value / 5)}` } as CSSProperties}
                          onClick={() => setActiveModule(row.route)}
                          aria-label={`${row.label} ${homeRunTrendLabels[index + 1]} 失败 ${value} 个分区`}
                        >
                          {value}
                        </button>
                      ))}
                    </Fragment>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
        <div className="home-run-side">
          <div className="home-run-side-head">
            <span>最近运行实例</span>
            <button type="button" onClick={() => setActiveModule("canvas")}>
              <RotateCcw size={13} />
              运行记录
            </button>
          </div>
          <div className="home-run-timeline">
            {homeRunTimeline.map((run) => (
              <button key={run.runId} type="button" className={`home-run-item ${run.tone}`} onClick={() => setActiveModule(run.route)}>
                <span>{run.time}</span>
                <strong>{run.title}</strong>
                <em>{run.state}</em>
                <small>{run.runId}</small>
              </button>
            ))}
          </div>
          <div className="home-run-actions">
            <button type="button" onClick={() => setActiveModule("assets")}>处理失败分区</button>
            <button type="button" onClick={() => setActiveModule("evaluation")}>进入评测回流</button>
          </div>
          <div className="home-run-side-summary">
            <div className="home-run-subhead">
              <span>运行影响面</span>
              <em>按当前异常聚合</em>
            </div>
            {[
              ["影响资产", "12", "标签、ASR、单据资产需同步", "assets"],
              ["待人审样本", "24", "金额冲突和申音候选优先", "listening"],
              ["待回归 badcase", "6", "边界过碎、低置信、串音", "evaluation"]
            ].map(([label, value, detail, route]) => (
              <button key={label} type="button" onClick={() => setActiveModule(route as ModuleKey)}>
                <span>{label}</span>
                <strong>{value}</strong>
                <em>{detail}</em>
              </button>
            ))}
          </div>
          <div className="home-run-next-steps">
            <span>建议下一步</span>
            <button type="button" onClick={() => setActiveModule("listening")}>先处理 AF-128 金额冲突证据</button>
            <button type="button" onClick={() => setActiveModule("assets")}>再回填 auris/label/event_tags</button>
            <button type="button" onClick={() => setActiveModule("insights")}>最后生成今日经营摘要</button>
          </div>
        </div>
      </div>
    </section>
  );
}
