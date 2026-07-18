import type { ModuleMetric, ProjectionMetricSource } from "../contracts/modules";

export function MetricCards({
  metrics,
  source,
  onMetricClick
}: {
  metrics: ModuleMetric[];
  source: ProjectionMetricSource;
  onMetricClick?: (metric: ModuleMetric) => void;
}) {
  return (
    <section className="module-metrics" data-source={source}>
      {metrics.map((metric) => {
        const content = (
          <>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
            <em>{metric.delta}</em>
          </>
        );
        return onMetricClick ? (
          <button key={metric.label} type="button" className={`module-metric ${metric.tone}`} data-source={source} onClick={() => onMetricClick(metric)}>
            {content}
          </button>
        ) : (
          <div key={metric.label} className={`module-metric ${metric.tone}`} data-source={source}>
            {content}
          </div>
        );
      })}
    </section>
  );
}
