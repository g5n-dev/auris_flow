export function QuotaPanel({ rows }: { rows?: Array<[string, number, string]> }) {
  const quotaRows = rows ?? [
    ["项目", 72, "72%"],
    ["成员", 58, "58%"],
    ["存储", 68, "68%"],
    ["月处理小时", 81, "81%"],
    ["并发任务", 46, "46%"]
  ];

  return (
    <div className="quota-panel">
      {quotaRows.map(([name, value, detail]) => (
        <div key={String(name)}>
          <span>{name}</span>
          <i>
            <b style={{ width: `${value}%` }} />
          </i>
          <strong>{detail}</strong>
        </div>
      ))}
    </div>
  );
}

export function TimelineList({ items }: { items: Array<[string, string, string]> }) {
  return (
    <div className="timeline-list">
      {items.map(([time, title, desc]) => (
        <button key={`${time}-${title}`}>
          <span>{time}</span>
          <strong>{title}</strong>
          <em>{desc}</em>
        </button>
      ))}
    </div>
  );
}

export function StackedFacts({ facts }: { facts: Array<[string, string]> }) {
  return (
    <div className="stacked-facts">
      {facts.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </div>
      ))}
    </div>
  );
}
