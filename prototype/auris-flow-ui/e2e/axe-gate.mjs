import axe from "axe-core";

const BLOCKING_IMPACTS = new Set(["critical", "serious"]);

const compactViolation = (violation) => ({
  id: violation.id,
  impact: violation.impact,
  help: violation.help,
  helpUrl: violation.helpUrl,
  nodes: violation.nodes.slice(0, 5).map((node) => ({
    target: node.target,
    html: node.html,
    failureSummary: node.failureSummary
  }))
});

export async function assertNoBlockingAxeViolations(
  page,
  {
    context = "body",
    label = context
  } = {}
) {
  if (!(await page.evaluate(() => Boolean(window.axe)))) {
    await page.evaluate(axe.source);
  }
  const result = await page.evaluate(
    async ({ selector }) => {
      const root = document.querySelector(selector);
      if (!root) {
        throw new Error(`axe context not found: ${selector}`);
      }
      return window.axe.run(root, {
        resultTypes: ["violations"],
        runOnly: {
          type: "tag",
          values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
        }
      });
    },
    { selector: context }
  );
  const blocking = result.violations
    .filter((violation) => BLOCKING_IMPACTS.has(violation.impact))
    .map(compactViolation);
  if (blocking.length > 0) {
    const error = new Error(`${label} 存在 critical/serious axe 违规`);
    error.detail = {
      context,
      blocking
    };
    throw error;
  }
  return {
    context,
    label,
    violationCount: 0
  };
}
