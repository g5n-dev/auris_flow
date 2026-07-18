export const defaultArchitecturePolicy = Object.freeze({
  limits: Object.freeze({
    appLines: 300,
    appUseStateCalls: 5,
    featureEntryLines: 400,
    hookOrServiceLines: 300,
    managedFileLines: 800,
    functionLines: 400
  }),
  frozenLegacy: Object.freeze({
    "src/api/client.ts": Object.freeze({ maxLines: 3108, skipFunctionLimit: true }),
    "src/modules/knowledge/KnowledgeModule.tsx": Object.freeze({ maxLines: 2352, skipFunctionLimit: true }),
    "src/modules/calibration/CalibrationWorkspace.tsx": Object.freeze({ maxLines: 720, skipFunctionLimit: true }),
    "src/modules/voiceprint.tsx": Object.freeze({ maxLines: 858, skipFunctionLimit: true })
  }),
  allowedLayers: Object.freeze({
    shared: Object.freeze(["shared"]),
    api: Object.freeze(["api", "shared"]),
    catalog: Object.freeze(["catalog", "shared"]),
    feature: Object.freeze(["feature", "api", "catalog", "shared"]),
    workspace: Object.freeze(["workspace", "feature", "catalog", "shared"]),
    shell: Object.freeze(["shell", "workspace", "feature", "catalog", "shared"]),
    "app-core": Object.freeze(["app-core", "api", "shared"]),
    "root-app": Object.freeze(["app-core", "shell", "workspace", "shared"]),
    bootstrap: Object.freeze(["root-app", "catalog", "shared"]),
    ambient: Object.freeze([])
  }),
  genericFileNames: Object.freeze([
    "utils",
    "helpers",
    "common",
    "misc",
    "miscellaneous",
    "general",
    "generic",
    "everything",
    "all",
    "stuff",
    "temp",
    "tmp",
    "legacy",
    "shared",
    "commonutils",
    "sharedutils",
    "sharedcomponents"
  ])
});

export function mergeArchitecturePolicy(override = {}) {
  return {
    ...defaultArchitecturePolicy,
    ...override,
    limits: { ...defaultArchitecturePolicy.limits, ...(override.limits ?? {}) },
    frozenLegacy: { ...defaultArchitecturePolicy.frozenLegacy, ...(override.frozenLegacy ?? {}) },
    allowedLayers: { ...defaultArchitecturePolicy.allowedLayers, ...(override.allowedLayers ?? {}) },
    genericFileNames: override.genericFileNames ?? defaultArchitecturePolicy.genericFileNames
  };
}
