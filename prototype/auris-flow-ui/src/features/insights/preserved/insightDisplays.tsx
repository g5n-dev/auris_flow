import type { ModuleKey } from "../../../shared/contracts/navigation";
import { BrainCircuit } from "lucide-react";
import { useState } from "react";
import type { CSSProperties } from "react";

function InsightBand({
  setActiveModule,
  compact = false,
  initialView = "tags"
}: {
  setActiveModule: (module: ModuleKey) => void;
  compact?: boolean;
  initialView?: "tags" | "model" | "assets";
}) {
  const [activeView, setActiveView] = useState<"tags" | "model" | "assets">(initialView);
  const days = ["6/20", "6/21", "6/22", "6/23", "6/24", "6/25", "6/26"];
  const views = {
    tags: {
      eyebrow: "业务标签趋势",
      title: "标签命中占比",
      subtitle: "同一口径：音频片段中命中该标签的占比",
      unit: "%",
      range: [0, 40],
      action: "证据样本",
      route: "listening" as ModuleKey,
      series: [
        { key: "price", label: "价格异议", value: "18.8%", delta: "+2.6", values: [11, 12, 14, 15, 16, 18, 18.8] },
        { key: "intent", label: "成交意向", value: "31.2%", delta: "+5.4", values: [22, 24, 25, 26, 28, 29, 31.2] },
        { key: "drive", label: "试驾意向", value: "24.6%", delta: "+1.8", values: [20, 21, 21.8, 22.2, 23, 24, 24.6] }
      ],
      cards: [
        ["价格异议", "18.8%", "高于同城 +6.2pp", "amber", "listening"],
        ["成交意向", "31.2%", "集中在试驾后 20 分钟", "green", "listening"],
        ["试驾意向", "24.6%", "A 店提升明显", "teal", "listening"],
        ["报价承诺", "16.4%", "需校验单据", "violet", "listening"]
      ]
    },
    model: {
      eyebrow: "模型质量趋势",
      title: "评测集 F1",
      subtitle: "同一口径：固定评测集上的模型能力分数",
      unit: "F1",
      range: [70, 100],
      action: "评测详情",
      route: "evaluation" as ModuleKey,
      series: [
        { key: "price", label: "ASR 转写", value: "93.1", delta: "+0.7", values: [91.8, 92.1, 92.4, 92.3, 92.8, 93, 93.1] },
        { key: "intent", label: "标签识别", value: "93.4", delta: "+0.9", values: [91.9, 92.4, 92.6, 92.8, 93, 93.2, 93.4] },
        { key: "quality", label: "边界切分", value: "88.6", delta: "-2.1", values: [91.1, 90.8, 90.7, 90.2, 89.7, 89.1, 88.6] }
      ],
      cards: [
        ["ASR 转写", "93.1", "稳定提升", "green", "evaluation"],
        ["标签识别", "93.4", "可发布", "teal", "evaluation"],
        ["边界切分", "88.6", "需观察", "amber", "evaluation"],
        ["串音识别", "84.9", "样本不足", "violet", "evaluation"]
      ]
    },
    assets: {
      eyebrow: "资产生成趋势",
      title: "每日生成资产数",
      subtitle: "同一口径：每天成功写入资产目录的资产条目数",
      unit: "条/日",
      range: [0, 5200],
      action: "资产目录",
      route: "assets" as ModuleKey,
      series: [
        { key: "price", label: "原始音频资产", value: "4,820", delta: "+11%", values: [3820, 3960, 4100, 4260, 4380, 4610, 4820] },
        { key: "intent", label: "ASR 转写资产", value: "4,360", delta: "+9%", values: [3310, 3480, 3620, 3790, 4010, 4190, 4360] },
        { key: "quality", label: "证据包资产", value: "1,284", delta: "+6%", values: [940, 980, 1040, 1100, 1160, 1210, 1284] }
      ],
      cards: [
        ["原始音频资产", "4,820", "成功写入", "green", "assets"],
        ["ASR 转写资产", "4,360", "失败 60", "teal", "assets"],
        ["证据包资产", "1,284", "待回填 42", "amber", "assets"],
        ["评测报告资产", "96", "今日新增", "violet", "assets"]
      ]
    }
  };
  const view = views[activeView];
  const [minValue, maxValue] = view.range;
  const lineChart = { width: 680, height: 230, left: 58, right: 34, top: 32, bottom: 34 };
  const lineBottom = lineChart.height - lineChart.bottom;
  const lineRight = lineChart.width - lineChart.right;
  const linePlotHeight = lineBottom - lineChart.top;
  const chartX = (index: number) => lineChart.left + index * ((lineRight - lineChart.left) / (days.length - 1));
  const chartY = (value: number) => lineBottom - ((value - minValue) / (maxValue - minValue)) * linePlotHeight;
  const lineGrid = [0, 1, 2, 3].map((index) => lineChart.top + index * (linePlotHeight / 3));
  const toPath = (values: number[]) => values.map((value, index) => `${index === 0 ? "M" : "L"}${chartX(index)} ${chartY(value).toFixed(1)}`).join(" ");
  const storeHeatmapHours = ["09", "10", "11", "12", "13", "14", "15", "18"];
  const storeHeatmapRows = [
    { store: "极光中心", issue: "同峰串音", values: [18, 22, 28, 82, 48, 36, 24, 18], route: "listening" as ModuleKey },
    { store: "北京 SKP", issue: "金额冲突", values: [12, 18, 24, 58, 76, 42, 31, 24], route: "listening" as ModuleKey },
    { store: "静安体验", issue: "低信噪", values: [21, 26, 33, 44, 53, 69, 38, 25], route: "evaluation" as ModuleKey },
    { store: "华南门店", issue: "缺录音", values: [8, 12, 18, 24, 31, 36, 64, 46], route: "assets" as ModuleKey }
  ];
  const storeRiskRows = [
    { label: "12:23 同峰串音", value: "82%", note: "B 工牌与 Hall-Mic 重叠，需确认主录音", tone: "red", route: "listening" as ModuleKey },
    { label: "报价金额冲突", value: "76%", note: "ASR 优惠金额与报价单字段不一致", tone: "amber", route: "listening" as ModuleKey },
    { label: "低信噪 ASR", value: "69%", note: "展厅麦克风热词召回下降，进入评测", tone: "violet", route: "evaluation" as ModuleKey },
    { label: "音频 URL 空窗", value: "64%", note: "试驾车设备 15:00 后缺录音资产", tone: "blue", route: "canvas" as ModuleKey }
  ];
  type InsightBandFact = { id: string; route: ModuleKey };
  const selectedFact: InsightBandFact = { id: "insight-band-current", route: view.route };
  const riskFacts: InsightBandFact[] = storeRiskRows.map((row, index) => ({ id: `insight-band-risk-${index}`, route: row.route }));
  const openInsightTarget = (fact: InsightBandFact | undefined, targetModule?: ModuleKey) => {
    setActiveModule(targetModule ?? fact?.route ?? view.route);
  };

  return (
    <div className={compact ? "insight-board compact" : "insight-board"}>
      <section className="insight-chart-card trend-card">
        <div className="insight-chart-head">
          <div>
            <span>{view.eyebrow}</span>
            <strong>{view.title}</strong>
          </div>
          <button onClick={() => setActiveModule(view.route)}>{view.action}</button>
        </div>
        <div className="insight-view-switch" aria-label="趋势视图">
          {[
            ["tags", "标签热度"],
            ["model", "模型质量"],
            ["assets", "资产生成"]
          ].map(([key, label]) => (
            <button key={key} className={activeView === key ? "active" : ""} onClick={() => setActiveView(key as typeof activeView)}>
              {label}
            </button>
          ))}
        </div>
        <p className="insight-chart-note">{view.subtitle}，单位：{view.unit}</p>
        <svg className="insight-line-chart" viewBox={`0 0 ${lineChart.width} ${lineChart.height}`} role="img" aria-label={`${view.eyebrow}近七天趋势`}>
          {lineGrid.map((y) => (
            <line key={y} x1={lineChart.left - 20} x2={lineRight} y1={y} y2={y} />
          ))}
          {days.map((label, index) => (
            <text key={label} x={chartX(index)} y={lineChart.height - 12}>
              {label}
            </text>
          ))}
          {view.series.map((series) => (
            <path key={series.key} className={series.key} d={toPath(series.values)} />
          ))}
          {view.series.map((series) => (
            <circle
              key={`${series.key}-dot`}
              className={series.key}
              cx={chartX(series.values.length - 1)}
              cy={chartY(series.values[series.values.length - 1])}
              r="4"
            />
          ))}
        </svg>
        <div className="insight-legend">
          {view.series.map((series) => (
            <span key={series.label} className={series.key}>{series.label} {series.value} <b>{series.delta}</b></span>
          ))}
        </div>
      </section>

      {activeView === "assets" ? (
        <section className="insight-chart-card store-heatmap-card">
          <div className="insight-chart-head">
            <div>
              <span>门店时空热区</span>
              <strong>门店 × 时间窗 × 异常强度</strong>
            </div>
            <button onClick={() => setActiveModule("listening")}>串音矩阵</button>
          </div>
          <div className="store-heatmap" aria-label="门店时空异常热区">
            <div className="store-heatmap-head">
              <span>门店 / 异常</span>
              {storeHeatmapHours.map((hour) => <b key={hour}>{hour}:00</b>)}
            </div>
            {storeHeatmapRows.map((row) => (
              <div key={row.store} className="store-heatmap-row">
                <button type="button" className="store-heatmap-label" onClick={() => setActiveModule(row.route)}>
                  <strong>{row.store}</strong>
                  <span>{row.issue}</span>
                </button>
                {row.values.map((value, index) => (
                  <button
                    key={`${row.store}-${storeHeatmapHours[index]}`}
                    type="button"
                    className={value > 70 ? "hot" : value > 50 ? "warm" : ""}
                    style={{ "--heat": `${value}%` } as CSSProperties}
                    onClick={() => setActiveModule(row.route)}
                  >
                    <span>{value}%</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </section>
      ) : (
        <section className="insight-chart-card sankey-card">
          <div className="insight-chart-head">
            <div>
              <span>问题流向桑基图</span>
              <strong>门店 → 标签 → 处置动作</strong>
            </div>
            <button onClick={() => setActiveModule("listening")}>下钻</button>
          </div>
          <svg className="insight-sankey" viewBox="0 0 640 240" role="img" aria-label="业务问题流向桑基图">
            <path className="flow amber f1" d="M118 56 C218 56 246 62 292 62" />
            <path className="flow teal f2" d="M118 112 C218 112 246 96 292 96" />
            <path className="flow violet f3" d="M118 168 C218 168 246 142 292 142" />
            <path className="flow red f4" d="M382 62 C454 62 482 74 540 74" />
            <path className="flow teal f5" d="M382 96 C454 96 482 126 540 126" />
            <path className="flow violet f6" d="M382 142 C454 142 482 174 540 174" />
            {[
              [24, 38, 92, "北京 SKP", "31"],
              [24, 94, 92, "极光中心", "24"],
              [24, 150, 92, "静安体验", "18"],
              [292, 44, 90, "价格异议", "18.8%"],
              [292, 84, 90, "串音", "40%"],
              [292, 130, 90, "模型下降", "54%"],
              [540, 56, 82, "人工复核", "42"],
              [540, 108, 82, "培训", "16"],
              [540, 156, 82, "重训", "9"]
            ].map(([x, y, width, label, value]) => (
              <g key={`${label}-${value}`}>
                <rect x={Number(x)} y={Number(y)} width={Number(width)} height="34" rx="6" />
                <text x={Number(x) + 6} y={Number(y) + 11}>{label}</text>
                <text className="value" x={Number(x) + 6} y={Number(y) + 26}>{value}</text>
              </g>
            ))}
          </svg>
        </section>
      )}

      {activeView === "assets" ? (
        <section className="insight-chart-card store-risk-card">
          <div className="insight-chart-head">
            <div>
              <span>异常排序与动作</span>
              <strong>从聚合指标直接落到处理入口</strong>
            </div>
            <button onClick={() => setActiveModule("canvas")}>补数据源</button>
          </div>
          <div className="store-risk-stack" aria-label="门店异常排序">
            {storeRiskRows.map((risk, index) => (
              <button key={risk.label} type="button" className={risk.tone} onClick={() => setActiveModule(risk.route)}>
                <b>{index + 1}</b>
                <div>
                  <strong>{risk.label}</strong>
                  <span>{risk.note}</span>
                  <i style={{ width: risk.value }} />
                </div>
                <em>{risk.value}</em>
              </button>
            ))}
          </div>
        </section>
      ) : (
        <section className="insight-chart-card radar-card">
          <div className="insight-chart-head">
            <div>
              <span>质量雷达图</span>
              <strong>业务风险与模型能力画像</strong>
            </div>
            <button onClick={() => setActiveModule("evaluation")}>评测</button>
          </div>
          <svg className="insight-radar" viewBox="0 0 300 240" role="img" aria-label="质量雷达图">
            {[
              "150,42 238,104 204,198 96,198 62,104",
              "150,68 214,114 190,178 110,178 86,114",
              "150,94 190,124 176,158 124,158 110,124"
            ].map((points) => (
              <polygon
                key={points}
                className="grid"
                points={points}
              />
            ))}
            {[
              ["转写", 150, 20],
              ["边界", 256, 96],
              ["标签", 210, 222],
              ["串音", 88, 222],
              ["单据", 40, 96]
            ].map(([label, x, y]) => (
              <text key={label} x={Number(x)} y={Number(y)}>{label}</text>
            ))}
            <polygon className="radar-area" points="150,54 224,108 198,176 104,186 78,108" />
            <polygon className="radar-compare" points="150,72 204,122 182,164 116,166 92,122" />
            <circle cx="150" cy="54" r="4" />
            <circle cx="224" cy="108" r="4" />
            <circle cx="198" cy="176" r="4" />
            <circle cx="104" cy="186" r="4" />
            <circle cx="78" cy="108" r="4" />
          </svg>
          <div className="radar-summary">
            <span>当前门店</span>
            <b>82%</b>
            <span>同城均值</span>
            <b>74%</b>
          </div>
        </section>
      )}

      <div className="insight-band">
        {view.cards.map(([item, value, note, tone, route]) => (
          <button
            key={item}
            className={tone}
	            onClick={() => setActiveModule(route as ModuleKey)}
          >
            <span>{item}</span>
            <strong>{value}</strong>
            <em>{note}</em>
            <i />
          </button>
        ))}
      </div>
    </div>
  );
}

function InsightOperationalFunnel({ setActiveModule }: { setActiveModule: (module: ModuleKey) => void }) {
  const stages: Array<{
    label: string;
    value: string;
    rate: number;
    drop: string;
    note: string;
    route: ModuleKey;
    tone: "blue" | "green" | "amber" | "red" | "violet";
  }> = [
    { label: "音频接入", value: "8,426", rate: 100, drop: "0", note: "PBX / 工牌 / 门店麦克风", route: "data", tone: "blue" },
    { label: "有效转写", value: "7,912", rate: 94, drop: "514", note: "ASR + VAD 通过基础质量", route: "assets", tone: "green" },
    { label: "标签命中", value: "3,184", rate: 38, drop: "4,728", note: "报价、异议、试驾、成交意向", route: "labels", tone: "violet" },
    { label: "风险复核", value: "317", rate: 15, drop: "2,867", note: "金额冲突、串音、低置信", route: "listening", tone: "amber" },
    { label: "报告回写", value: "46", rate: 6, drop: "271", note: "日报、门店归因、训练包", route: "assets", tone: "red" }
  ];
  const blockers = [
    { label: "标签命中断层", value: "4,728", rate: 56, detail: "ASR 有效但未命中业务标签", route: "labels" as ModuleKey },
    { label: "复核沉积", value: "317", rate: 15, detail: "金额冲突、串音和低置信待处理", route: "listening" as ModuleKey },
    { label: "报告未回写", value: "271", rate: 86, detail: "复核后未进入日报/归因/训练包", route: "assets" as ModuleKey }
  ];
  const summary = [
    { label: "接入", value: "8,426" },
    { label: "有效率", value: "94%" },
    { label: "命中率", value: "38%" },
    { label: "回写", value: "46" }
  ];

  return (
    <section className="insight-funnel-card">
      <div className="insight-chart-head">
        <div>
          <span>运营转化漏斗</span>
          <strong>接入 → 转写 → 标签 → 复核 → 报告回写</strong>
        </div>
        <div className="insight-funnel-summary" aria-label="运营漏斗关键指标">
          {summary.map((item) => (
            <span key={item.label}>
              <em>{item.label}</em>
              <b>{item.value}</b>
            </span>
          ))}
        </div>
        <button type="button" onClick={() => setActiveModule("listening")}>处理阻断</button>
      </div>
      <div className="insight-funnel-layout">
        <div className="insight-funnel-visual" aria-label="运营转化漏斗">
          {stages.map((stage, index) => {
            const visualWidth = Math.max(38, Math.min(100, 36 + stage.rate * 0.64));
            const nextStage = stages[index + 1];
            return (
              <div key={stage.label} className="insight-funnel-step">
                <button
                  type="button"
                  className={`insight-funnel-stage ${stage.tone}`}
                  style={{ "--funnel-width": `${stage.rate}%`, "--funnel-display-width": `${visualWidth}%` } as CSSProperties}
                  onClick={() => setActiveModule(stage.route)}
                >
                  <span>
                    <i>{index + 1}</i>
                    <em>{stage.rate}%</em>
                  </span>
                  <strong>{stage.label}</strong>
                  <b>{stage.value}</b>
                  <small>{stage.note}</small>
                  <div className="insight-funnel-meter" aria-hidden="true">
                    <i />
                  </div>
                  <em>{index === 0 ? "入口样本" : `阻断 ${stage.drop}`}</em>
                </button>
                {nextStage && (
                  <div className="insight-funnel-drop">
                    <i />
                    <span>流失 {nextStage.drop}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <aside className="insight-funnel-diagnostics" aria-label="漏斗阻断诊断">
          <div>
            <span>阻断诊断</span>
            <strong>优先处理高损耗节点</strong>
          </div>
          {blockers.map((item) => (
            <button key={item.label} type="button" onClick={() => setActiveModule(item.route)}>
              <span>
                <b>{item.label}</b>
                <em>{item.detail}</em>
              </span>
              <strong>{item.value}</strong>
              <i style={{ width: `${item.rate}%` }} />
            </button>
          ))}
        </aside>
      </div>
      <div className="insight-funnel-footer">
        <span>当前最大阻断：标签命中到风险复核之间需要按门店、设备和时间窗归因。</span>
        <button type="button" onClick={() => setActiveModule("canvas")}>补齐采集链路</button>
        <button type="button" onClick={() => setActiveModule("evaluation")}>重跑质量评测</button>
      </div>
    </section>
  );
}

function InsightRecallDashboard({ setActiveModule, compact = false }: { setActiveModule: (module: ModuleKey) => void; compact?: boolean }) {
  const recallRows = [
    { label: "报价金额", recall: 91, precision: 88, missed: 7, samples: 128, owner: "标签运营", route: "listening" as ModuleKey, tone: "blue" },
    { label: "客户价格异议", recall: 84, precision: 91, missed: 12, samples: 96, owner: "销售质检", route: "labels" as ModuleKey, tone: "green" },
    { label: "串音疑似", recall: 68, precision: 76, missed: 18, samples: 42, owner: "音频算法", route: "listening" as ModuleKey, tone: "amber" },
    { label: "成交意向", recall: 79, precision: 86, missed: 9, samples: 74, owner: "业务运营", route: "data" as ModuleKey, tone: "violet" }
  ];
  const evidenceChannels = ["ASR", "单据", "声纹", "人工"];
  const recallMatrix = [
    [92, 88, 71, 96],
    [86, 64, 58, 91],
    [72, 42, 84, 76],
    [81, 69, 48, 89]
  ];
  const scatter = recallRows.map((row, index) => ({
    ...row,
    x: 42 + row.recall * 2.05,
    y: 222 - row.precision * 1.78,
    r: 5 + index * 1.4
  }));

  return (
    <section className={compact ? "insight-recall-dashboard compact" : "insight-recall-dashboard"}>
      <div className="insight-chart-head">
        <div>
          <span>标签召回大盘</span>
          <strong>基于证据样本评估 Recall / Precision / 漏检</strong>
        </div>
        <button type="button" onClick={() => setActiveModule("labels")}>配置标签口径</button>
      </div>
      <div className="insight-recall-grid">
        <div className="recall-score-table">
          <div className="recall-score-row head">
            <span>标签</span>
            <span>召回</span>
            <span>精度</span>
            <span>漏检</span>
          </div>
          {recallRows.map((row) => (
            <button key={row.label} type="button" className={`recall-score-row ${row.tone}`} onClick={() => setActiveModule(row.route)}>
              <strong>{row.label}</strong>
              <span>
                <b>{row.recall}%</b>
                <i style={{ width: `${row.recall}%` }} />
              </span>
              <span>
                <b>{row.precision}%</b>
                <i style={{ width: `${row.precision}%` }} />
              </span>
              <em>{row.missed} 条</em>
            </button>
          ))}
        </div>
        <div className="recall-matrix">
          <div className="recall-matrix-head">
            <span>证据通道覆盖</span>
            {evidenceChannels.map((channel) => <b key={channel}>{channel}</b>)}
          </div>
          {recallRows.map((row, rowIndex) => (
            <div key={row.label} className="recall-matrix-row">
              <button type="button" onClick={() => setActiveModule(row.route)}>{row.label}</button>
              {recallMatrix[rowIndex].map((value, cellIndex) => (
                <button
                  key={`${row.label}-${evidenceChannels[cellIndex]}`}
                  type="button"
                  className={value < 60 ? "weak" : value > 85 ? "strong" : ""}
                  style={{ "--recall": `${value}%` } as CSSProperties}
                  onClick={() => setActiveModule(cellIndex === 1 ? "data" : cellIndex === 3 ? "labels" : "listening")}
                >
                  {value}
                </button>
              ))}
            </div>
          ))}
        </div>
        {!compact && (
          <div className="recall-scatter-card">
            <span>召回 / 精度分布</span>
            <svg viewBox="0 0 280 230" role="img" aria-label="标签召回精度散点图">
              {[60, 75, 90].map((tick) => (
                <g key={tick}>
                  <line x1={40 + tick * 2.05} x2={40 + tick * 2.05} y1="30" y2="205" />
                  <line x1="38" x2="252" y1={222 - tick * 1.78} y2={222 - tick * 1.78} />
                  <text x={40 + tick * 2.05} y="220">{tick}</text>
                  <text x="8" y={226 - tick * 1.78}>{tick}</text>
                </g>
              ))}
              <text x="104" y="20">Recall</text>
              <text x="4" y="24">Precision</text>
              {scatter.map((item) => (
                <g key={item.label} className={item.tone}>
                  <circle cx={item.x} cy={item.y} r={item.r} />
                  <text x={item.x + 8} y={item.y + 4}>{item.label}</text>
                </g>
              ))}
            </svg>
          </div>
        )}
      </div>
      <div className="recall-miss-queue">
        <span>漏检样本队列</span>
        {[
          ["串音疑似", "18 条", "Hall-Mic 与工牌同峰但标签未召回", "listening"],
          ["客户价格异议", "12 条", "ASR 命中压价语义但意图标签缺失", "labels"],
          ["成交意向", "9 条", "试驾后 20 分钟出现确认语句未入库", "data"]
        ].map(([label, count, detail, route]) => (
          <button key={label} type="button" onClick={() => setActiveModule(route as ModuleKey)}>
            <strong>{label}</strong>
            <b>{count}</b>
            <em>{detail}</em>
          </button>
        ))}
      </div>
    </section>
  );
}

function DataTable({
  columns,
  rows,
  onRowClick
}: {
  columns: string[];
  rows: string[][];
  onRowClick?: () => void;
}) {
  return (
    <div className="module-table" style={{ "--cols": columns.length } as CSSProperties}>
      <div className="module-table-head">
        {columns.map((column) => (
          <span key={column}>{column}</span>
        ))}
      </div>
      {rows.map((row) => (
        <button key={row.join("-")} className="module-table-row" onClick={onRowClick}>
          {row.map((cell, index) => (
            <span key={`${cell}-${index}`}>{cell}</span>
          ))}
        </button>
      ))}
    </div>
  );
}

function QualityBars() {
  return (
    <div className="quality-bars">
      {[
        ["自动通过", 88],
        ["人工复核 SLA", 76],
        ["证据链完整", 96],
        ["资产质量", 92]
      ].map(([label, value]) => (
        <div key={String(label)}>
          <span>{label}</span>
          <strong>{value}%</strong>
          <i>
            <b style={{ width: `${value}%` }} />
          </i>
        </div>
      ))}
    </div>
  );
}

function ModuleTimeline() {
  return (
    <div className="module-timeline">
      <div className="module-axis">
        {["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"].map((time) => (
          <span key={time}>{time}</span>
        ))}
      </div>
      {["音频", "VAD/ASR", "事件标签", "业务单据", "人工复核"].map((lane, laneIndex) => (
        <div key={lane} className="module-time-row">
          <strong>{lane}</strong>
          <div>
            {Array.from({ length: 16 }, (_, index) => (
              <button
                key={index}
                className={[
                  index === 7 && laneIndex < 4 ? "active" : "",
                  index === 8 && laneIndex === 2 ? "warn" : "",
                  index === 11 && laneIndex === 3 ? "doc" : ""
                ].join(" ")}
                style={{ opacity: 0.22 + (((index * 17 + laneIndex * 9) % 70) / 100) }}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function AgentRecommendation({
  title,
  reason,
  action,
  onAction
}: {
  title: string;
  reason: string;
  action: string;
  onAction: () => void;
}) {
  return (
    <div className="agent-reco">
      <BrainCircuit size={28} />
      <strong>{title}</strong>
      <p>{reason}</p>
      <div>
        <span>置信度</span>
        <b>82%</b>
      </div>
      <button onClick={onAction}>{action}</button>
    </div>
  );
}
