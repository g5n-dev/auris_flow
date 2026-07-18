export const INSIGHT_METRIC_POLL_LIMIT = 15;
export const INSIGHT_REPORT_POLL_LIMIT = 15;

const INSIGHT_POLL_INTERVAL_MS = 1000;

export const waitForInsightPoll = () => new Promise<void>((resolve) => {
  window.setTimeout(resolve, INSIGHT_POLL_INTERVAL_MS);
});
