export const APP_ENTRY = "src/App.tsx";
export const HTML_ENTRY = "index.html";

const featureEntry = (feature) => `src/features/${feature}/index.ts`;

const entries = {
  home: featureEntry("home"),
  tenants: featureEntry("tenants"),
  projects: featureEntry("projects"),
  canvas: featureEntry("canvas"),
  data: featureEntry("data"),
  voiceprint: featureEntry("voiceprint"),
  knowledge: "src/modules/knowledge/index.ts",
  listening: featureEntry("listening"),
  labels: featureEntry("labels"),
  insights: featureEntry("insights"),
  evaluation: featureEntry("evaluation"),
  calibration: "src/modules/calibration/index.ts",
  assets: featureEntry("assets"),
  settings: featureEntry("settings")
};

const canvasTabs = ["flow", "definition", "canvases", "io", "schedule", "experiments", "runs", "versions"];
const canvasDrawers = ["overview", "mapping", "plan", "logs"];

export function dynamicEdge(from, to) {
  return { from, to };
}

export function dynamicEdgeKey({ from, to }) {
  return `${from}\u0000${to}`;
}

function topLevelScenario(route, entry = entries[route]) {
  return {
    id: "base",
    entryKeys: [entry],
    dynamicEdges: [dynamicEdge(APP_ENTRY, entry)]
  };
}

function buildCanvasScenarios() {
  const scenarios = [];
  for (const tab of canvasTabs) {
    for (const drawer of canvasDrawers) {
      for (const audio of [false, true]) {
        for (const nodes of [false, true]) {
          scenarios.push({
            id: `tab=${tab};drawer=${drawer};audio=${audio ? "on" : "off"};nodes=${nodes ? "open" : "closed"}`,
            entryKeys: [entries.canvas],
            dynamicEdges: [dynamicEdge(APP_ENTRY, entries.canvas)]
          });
        }
      }
    }
  }
  return scenarios;
}

function buildListeningScenarios() {
  const scenarios = [topLevelScenario("listening")];
  for (const mode of ["simple", "matrix"]) {
    for (const config of [false, true]) {
      scenarios.push({
        id: `mode=${mode};config=${config ? "open" : "closed"}`,
        entryKeys: [entries.listening],
        dynamicEdges: [dynamicEdge(APP_ENTRY, entries.listening)]
      });
    }
  }
  for (const config of [false, true]) {
    for (const boundary of [false, true]) {
      for (const diff of [false, true]) {
        for (const track of [false, true]) {
          scenarios.push({
            id: `mode=evidence;config=${config ? "open" : "closed"};boundary=${boundary ? "open" : "closed"};diff=${diff ? "open" : "closed"};track=${track ? "open" : "closed"}`,
            entryKeys: [entries.listening],
            dynamicEdges: [dynamicEdge(APP_ENTRY, entries.listening)]
          });
        }
      }
    }
  }
  return scenarios;
}

export function buildRouteScenarioDefinitions() {
  const simpleRoutes = ["home", "tenants", "projects", "knowledge", "labels", "insights", "assets", "settings"];
  const definitions = Object.fromEntries(simpleRoutes.map((route) => [route, {
    entry: entries[route],
    scenarios: [topLevelScenario(route)]
  }]));
  definitions.canvas = { entry: entries.canvas, scenarios: buildCanvasScenarios() };
  definitions.data = {
    entry: entries.data,
    scenarios: [
      topLevelScenario("data"),
      {
        id: "tab=voiceprint",
        entryKeys: [entries.data, entries.voiceprint],
        dynamicEdges: [dynamicEdge(APP_ENTRY, entries.data), dynamicEdge(APP_ENTRY, entries.voiceprint)]
      }
    ]
  };
  definitions.listening = { entry: entries.listening, scenarios: buildListeningScenarios() };
  definitions.evaluation = {
    entry: entries.evaluation,
    scenarios: [
      topLevelScenario("evaluation"),
      {
        id: "workspace=calibration",
        entryKeys: [entries.evaluation, entries.calibration],
        dynamicEdges: [dynamicEdge(APP_ENTRY, entries.evaluation), dynamicEdge(entries.evaluation, entries.calibration)]
      }
    ]
  };
  return Object.fromEntries(Object.keys(entries)
    .filter((route) => !["voiceprint", "calibration"].includes(route))
    .map((route) => [route, definitions[route]]));
}

export function collectManifestDynamicEdges(manifest) {
  return Object.entries(manifest).flatMap(([from, chunk]) =>
    (chunk.dynamicImports ?? []).map((to) => dynamicEdge(from, to))
  );
}

export function collectRegisteredDynamicEdges(definitions, initialEdges) {
  const edges = [...initialEdges];
  for (const definition of Object.values(definitions)) {
    for (const scenario of definition.scenarios) edges.push(...scenario.dynamicEdges);
  }
  return [...new Map(edges.map((edge) => [dynamicEdgeKey(edge), edge])).values()];
}

export function compareDynamicEdgeCoverage(manifest, definitions, initialEdges) {
  const actual = collectManifestDynamicEdges(manifest);
  const registered = collectRegisteredDynamicEdges(definitions, initialEdges);
  const actualKeys = new Set(actual.map(dynamicEdgeKey));
  const registeredKeys = new Set(registered.map(dynamicEdgeKey));
  return {
    actual,
    registered,
    unregistered: actual.filter((edge) => !registeredKeys.has(dynamicEdgeKey(edge))),
    stale: registered.filter((edge) => !actualKeys.has(dynamicEdgeKey(edge)))
  };
}

export const initialDynamicEdges = [
  dynamicEdge(HTML_ENTRY, APP_ENTRY),
  dynamicEdge(APP_ENTRY, entries.home)
];

export const routeManifestEntries = entries;
