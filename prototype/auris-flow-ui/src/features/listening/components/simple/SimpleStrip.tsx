import { segmentBars } from "../../fixtures/evidenceFixtures";

export function SimpleStrip({ setModeLabel }: { setModeLabel: () => void }) {
  return (
    <section className="simple-strip">
      <div>
        <strong>简单调听</strong>
        <span>快速切换，仅保留波形、ASR 与高亮标签</span>
      </div>
      <div className="strip-wave">
        {segmentBars.slice(0, 38).map((height, i) => (
          <span key={i} style={{ height: `${height}%` }} />
        ))}
      </div>
      <div className="strip-tags">
        <span>报价</span>
        <span>试驾</span>
        <span>异议</span>
        <span>低置信</span>
      </div>
      <button onClick={setModeLabel}>收起</button>
    </section>
  );
}
