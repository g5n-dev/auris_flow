import { existsSync, readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { isAbsolute, relative, resolve, sep } from "node:path";
import ts from "typescript";

import { mergeArchitecturePolicy } from "./frontend-architecture-policy.mjs";

const SOURCE_EXTENSION = /\.(?:[cm]?[jt]sx?|d\.[cm]?[jt]s)$/;
const NON_CODE_EXTENSION = /\.(?:css|less|scss|sass|json|svg|png|jpe?g|gif|webp|woff2?|ttf|mp3|wav)$/i;

export function analyzeProject({
  rootDir,
  debtPath = "scripts/frontend-architecture-debt.json",
  policy: policyOverride,
  requireZeroDebt = false
}) {
  const absoluteRoot = resolve(rootDir);
  const policy = mergeArchitecturePolicy(policyOverride);
  const errors = [];
  const debt = readDebt(absoluteRoot, debtPath, errors);
  validateDebtDocument(debt, errors);
  const parsed = readTsConfig(absoluteRoot, errors);
  if (!parsed) return finishReport(errors, [], [], {});

  const discoveredSourceFiles = ts.sys.readDirectory(
    resolve(absoluteRoot, "src"),
    [".ts", ".tsx", ".mts", ".cts"],
    undefined,
    undefined
  );
  const rootNames = [...new Set([...parsed.fileNames, ...discoveredSourceFiles])];
  const program = ts.createProgram({ rootNames, options: parsed.options });
  const sourceFiles = program.getSourceFiles()
    .filter((sourceFile) => isProjectSource(absoluteRoot, sourceFile.fileName))
    .sort((left, right) => projectPath(absoluteRoot, left.fileName).localeCompare(projectPath(absoluteRoot, right.fileName)));
  const nodes = new Map();
  const edges = [];
  const appMetrics = { lines: 0, useStateCalls: 0, directApiImports: [] };
  const transition = debt?.transitions?.["src/App.tsx"];

  for (const sourceFile of sourceFiles) {
    const path = projectPath(absoluteRoot, sourceFile.fileName);
    const classification = classifySource(path);
    nodes.set(path, classification);
    if (!classification) {
      addError(errors, "UNCLASSIFIED_SOURCE", path, 1, "源文件不属于已登记的架构层");
      continue;
    }
    inspectFile({
      absoluteRoot,
      appMetrics,
      compilerOptions: parsed.options,
      errors,
      sourceFile,
      path,
      policy,
      transition,
      edges
    });
  }

  for (const edge of edges) {
    const from = nodes.get(edge.importer) ?? classifySource(edge.importer);
    const to = nodes.get(edge.target) ?? classifySource(edge.target);
    if (!from || !to) continue;
    validateEdge({ edge, from, to, transition, policy, errors });
  }

  const cycles = findCycles([...nodes.keys()], edges);
  for (const cycle of cycles) {
    const firstEdge = cycle.edges[0];
    addError(
      errors,
      "CYCLE",
      firstEdge?.importer ?? cycle.nodes[0],
      firstEdge?.line ?? 1,
      `检测到循环依赖：${cycle.nodes.join(" -> ")} -> ${cycle.nodes[0]}`
    );
  }

  validateAppDebt({ appMetrics, transition, requireZeroDebt, limits: policy.limits, errors });
  return finishReport(errors, edges, cycles, { app: appMetrics, files: sourceFiles.length });
}

export function formatReport(report) {
  if (report.ok) return "frontend architecture ok";
  return [
    `frontend architecture failed (${report.errors.length})`,
    ...report.errors.map((error) => `${error.code} ${error.path}:${error.line} ${error.message}`)
  ].join("\n");
}

export function hashImportSymbols(symbols) {
  return createHash("sha256").update(symbols.join("\n")).digest("hex");
}

function readDebt(rootDir, debtPath, errors) {
  if (!debtPath) return null;
  const path = isAbsolute(debtPath) ? debtPath : resolve(rootDir, debtPath);
  if (!existsSync(path)) return null;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    addError(errors, "DEBT_SCHEMA", projectPath(rootDir, path), 1, `迁移债务 JSON 无法解析：${String(error)}`);
    return null;
  }
}

function validateDebtDocument(debt, errors) {
  if (!debt) return;
  if (debt.schemaVersion !== 1 || !debt.transitions || typeof debt.transitions !== "object") {
    addError(errors, "DEBT_SCHEMA", "scripts/frontend-architecture-debt.json", 1, "迁移债务 schemaVersion/transitions 无效");
    return;
  }
  for (const [path, transition] of Object.entries(debt.transitions)) {
    const direct = transition.directApiImports;
    if (!direct || !Array.isArray(direct.allowedSymbols)) {
      addError(errors, "DEBT_SCHEMA", path, 1, "directApiImports.allowedSymbols 缺失");
      continue;
    }
    const sorted = [...direct.allowedSymbols].sort();
    const unique = [...new Set(sorted)];
    if (unique.length !== direct.allowedSymbols.length
      || sorted.some((symbol, index) => symbol !== direct.allowedSymbols[index])) {
      addError(errors, "DEBT_SCHEMA", path, 1, "allowedSymbols 必须唯一并按字典序排序");
    }
    if (direct.count !== direct.allowedSymbols.length) {
      addError(errors, "DEBT_SCHEMA", path, 1, `directApiImports.count 应为 ${direct.allowedSymbols.length}`);
    }
    const expectedHash = hashImportSymbols(direct.allowedSymbols);
    if (direct.setSha256 !== expectedHash) {
      addError(errors, "DEBT_SCHEMA", path, 1, `directApiImports.setSha256 应为 ${expectedHash}`);
    }
    if (!Array.isArray(transition.allowedBoundaryTargets)) {
      addError(errors, "DEBT_SCHEMA", path, 1, "allowedBoundaryTargets 缺失");
    }
  }
}

function readTsConfig(rootDir, errors) {
  const configPath = resolve(rootDir, "tsconfig.json");
  const config = ts.readConfigFile(configPath, ts.sys.readFile);
  if (config.error) {
    addError(errors, "TSCONFIG", "tsconfig.json", 1, flattenDiagnostic(config.error));
    return null;
  }
  const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, rootDir, undefined, configPath);
  for (const diagnostic of parsed.errors) {
    addError(errors, "TSCONFIG", "tsconfig.json", 1, flattenDiagnostic(diagnostic));
  }
  return parsed;
}

function inspectFile({
  absoluteRoot,
  appMetrics,
  compilerOptions,
  errors,
  sourceFile,
  path,
  policy,
  transition,
  edges
}) {
  const lines = countLines(sourceFile.text);
  const frozen = policy.frozenLegacy[path];
  if (frozen && lines > frozen.maxLines) {
    addError(errors, "FROZEN_LEGACY_GROWTH", path, 1, `${lines} 行超过冻结上限 ${frozen.maxLines}`);
  }

  const classification = classifySource(path);
  const managed = isManagedSource(path, classification);
  if (managed && !frozen) validateFileSize(path, lines, policy.limits, transition, errors);
  if (managed && isGenericFileName(path, policy.genericFileNames)) {
    addError(errors, "GENERIC_FILENAME", path, 1, "禁止使用无领域语义的大杂烩文件名");
  }

  if (path === "src/App.tsx") appMetrics.lines = lines;
  const skipFunctionLimit = frozen?.skipFunctionLimit || (path === "src/App.tsx" && transition);
  const appApiSymbols = new Set();
  const reactBindings = findReactUseStateBindings(sourceFile);

  const addEdgeForSpecifier = (node, specifier, kind, symbols = []) => {
    const line = lineOf(sourceFile, node);
    const resolvedTarget = resolveInternalModule({
      absoluteRoot,
      compilerOptions,
      importerFile: sourceFile.fileName,
      specifier
    });
    if (!resolvedTarget) {
      if (isExpectedInternalSpecifier(specifier, compilerOptions) && !NON_CODE_EXTENSION.test(specifier)) {
        addError(errors, "UNRESOLVED_INTERNAL_IMPORT", path, line, `无法解析内部导入 ${JSON.stringify(specifier)}`);
      }
      return;
    }
    const edge = { importer: path, target: resolvedTarget, line, kind, specifier, symbols };
    edges.push(edge);
    if (path === "src/App.tsx" && classifySource(resolvedTarget)?.layer === "api") {
      for (const symbol of symbols) appApiSymbols.add(symbol);
    }
  };

  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteralLike(node.moduleSpecifier)) {
      addEdgeForSpecifier(node, node.moduleSpecifier.text, importKind(node), importedSymbols(node));
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteralLike(node.moduleSpecifier)) {
      addEdgeForSpecifier(
        node,
        node.moduleSpecifier.text,
        node.isTypeOnly ? "type" : "re-export",
        exportedSymbols(node)
      );
    } else if (ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
      && ts.isStringLiteralLike(node.moduleReference.expression)) {
      addEdgeForSpecifier(node, node.moduleReference.expression.text, node.isTypeOnly ? "type" : "runtime", [node.name.text]);
    } else if (ts.isImportTypeNode(node)
      && ts.isLiteralTypeNode(node.argument)
      && ts.isStringLiteralLike(node.argument.literal)) {
      addEdgeForSpecifier(node, node.argument.literal.text, "type", ["*import-type*"]);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const argument = node.arguments[0];
      if (!argument || !ts.isStringLiteralLike(argument)) {
        addError(errors, "NON_LITERAL_DYNAMIC_IMPORT", path, lineOf(sourceFile, node), "动态 import 必须使用字面量路径");
      } else {
        addEdgeForSpecifier(node, argument.text, "dynamic", ["*dynamic*"]);
      }
    } else if (ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "require") {
      const argument = node.arguments[0];
      if (!argument || !ts.isStringLiteralLike(argument)) {
        addError(errors, "NON_LITERAL_REQUIRE", path, lineOf(sourceFile, node), "require 必须使用字面量路径");
      } else {
        addEdgeForSpecifier(node, argument.text, "runtime", ["*require*"]);
      }
    }

    if (path === "src/App.tsx" && ts.isCallExpression(node) && isUseStateCall(node.expression, reactBindings)) {
      appMetrics.useStateCalls += 1;
    }
    if (!skipFunctionLimit && isFunctionNode(node)) {
      const start = lineOf(sourceFile, node);
      const end = sourceFile.getLineAndCharacterOfPosition(node.end).line + 1;
      const size = end - start + 1;
      if (size > policy.limits.functionLines) {
        addError(errors, "FUNCTION_SIZE", path, start, `${size} 行超过函数/组件上限 ${policy.limits.functionLines}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  if (path === "src/App.tsx") appMetrics.directApiImports = [...appApiSymbols].sort();
}

function validateFileSize(path, lines, limits, transition, errors) {
  let limit = limits.managedFileLines;
  if (path === "src/App.tsx") limit = transition?.maxLines ?? limits.appLines;
  else if (isModuleEntry(path)) {
    limit = limits.featureEntryLines;
  } else if (isHookOrService(path)) {
    limit = limits.hookOrServiceLines;
  }
  if (lines > limit) addError(errors, "FILE_SIZE", path, 1, `${lines} 行超过文件上限 ${limit}`);
}

function validateEdge({ edge, from, to, transition, policy, errors }) {
  if (edge.target === "src/App.tsx") {
    if (edge.importer !== "src/main.tsx") {
      addError(errors, "APP_REVERSE_DEPENDENCY", edge.importer, edge.line, "任何文件都不得从 App.tsx 导入符号");
      return;
    }
    if (edge.kind !== "dynamic") {
      addError(errors, "APP_BOOTSTRAP_IMPORT", edge.importer, edge.line, "main.tsx 只能通过字面量 dynamic import 加载 App 默认导出");
    }
    return;
  }

  if (from.layer === "root-app" && transition) {
    const normallyAllowed = policy.allowedLayers["root-app"].includes(to.layer);
    const debtAllowed = transition.allowedBoundaryTargets?.includes(edge.target);
    if (!normallyAllowed && !debtAllowed) {
      addError(errors, "APP_BOUNDARY_GROWTH", edge.importer, edge.line, `App 新增了未登记边界 ${edge.target}`);
    }
    return;
  }

  if (to.layer === "feature" && from.layer === "feature" && from.feature !== to.feature) {
    if (!isFeaturePublicEntry(edge.target)) {
      addError(errors, "FEATURE_INTERNAL_IMPORT", edge.importer, edge.line, `feature ${from.feature} 不得导入 ${to.feature} 的内部实现`);
    }
    return;
  }
  if (to.layer === "feature" && ["workspace", "shell"].includes(from.layer) && !isFeaturePublicEntry(edge.target)) {
    addError(errors, "FEATURE_INTERNAL_IMPORT", edge.importer, edge.line, `${from.layer} 只能导入 feature 公共入口`);
    return;
  }
  if (to.layer === "feature"
    && ["workspace", "shell"].includes(from.layer)
    && edge.kind !== "dynamic"
    && edge.kind !== "type") {
    addError(errors, "FEATURE_MUST_BE_LAZY", edge.importer, edge.line, `${from.layer} 必须通过字面量 dynamic import 加载顶级 feature`);
    return;
  }

  const allowed = policy.allowedLayers[from.layer] ?? [];
  if (!allowed.includes(to.layer)) {
    addError(errors, "LAYER_VIOLATION", edge.importer, edge.line, `${from.layer} 不得依赖 ${to.layer}（${edge.target}）`);
  }
}

function validateAppDebt({ appMetrics, transition, requireZeroDebt, limits, errors }) {
  if (transition) {
    if (requireZeroDebt) {
      addError(errors, "TRANSITION_DEBT", "src/App.tsx", 1, "最终门禁要求清零 App 迁移债务");
    }
    if (appMetrics.useStateCalls > transition.maxDirectUseStateCalls) {
      addError(errors, "APP_USE_STATE_GROWTH", "src/App.tsx", 1, `${appMetrics.useStateCalls} 次超过迁移上限 ${transition.maxDirectUseStateCalls}`);
    }
    const allowed = new Set(transition.directApiImports?.allowedSymbols ?? []);
    const additions = appMetrics.directApiImports.filter((symbol) => !allowed.has(symbol));
    if (additions.length > 0) {
      addError(errors, "APP_API_IMPORT_GROWTH", "src/App.tsx", 1, `新增 API import：${additions.join(", ")}`);
    }
    return;
  }
  if (appMetrics.lines > 0 && appMetrics.useStateCalls > limits.appUseStateCalls) {
    addError(errors, "APP_USE_STATE", "src/App.tsx", 1, `${appMetrics.useStateCalls} 次超过最终上限 ${limits.appUseStateCalls}`);
  }
  if (appMetrics.directApiImports.length > 0) {
    addError(errors, "APP_API_IMPORT", "src/App.tsx", 1, `App 不得直接导入业务 API：${appMetrics.directApiImports.join(", ")}`);
  }
}

function resolveInternalModule({ absoluteRoot, compilerOptions, importerFile, specifier }) {
  const resolved = ts.resolveModuleName(specifier, importerFile, compilerOptions, ts.sys).resolvedModule;
  if (!resolved || resolved.isExternalLibraryImport) return null;
  const target = projectPath(absoluteRoot, resolved.resolvedFileName);
  if (!target.startsWith("src/") || !SOURCE_EXTENSION.test(target)) return null;
  return target;
}

function findCycles(paths, edges) {
  const adjacency = new Map(paths.map((path) => [path, []]));
  for (const edge of edges) {
    if (adjacency.has(edge.importer) && adjacency.has(edge.target)) adjacency.get(edge.importer).push(edge);
  }
  for (const outgoing of adjacency.values()) outgoing.sort(compareEdges);

  const indexByPath = new Map();
  const lowLink = new Map();
  const stack = [];
  const onStack = new Set();
  const components = [];
  let index = 0;

  const strongConnect = (path) => {
    indexByPath.set(path, index);
    lowLink.set(path, index);
    index += 1;
    stack.push(path);
    onStack.add(path);
    for (const edge of adjacency.get(path) ?? []) {
      if (!indexByPath.has(edge.target)) {
        strongConnect(edge.target);
        lowLink.set(path, Math.min(lowLink.get(path), lowLink.get(edge.target)));
      } else if (onStack.has(edge.target)) {
        lowLink.set(path, Math.min(lowLink.get(path), indexByPath.get(edge.target)));
      }
    }
    if (lowLink.get(path) === indexByPath.get(path)) {
      const component = [];
      let current;
      do {
        current = stack.pop();
        onStack.delete(current);
        component.push(current);
      } while (current !== path);
      components.push(component.sort());
    }
  };
  for (const path of [...paths].sort()) if (!indexByPath.has(path)) strongConnect(path);

  return components
    .filter((component) => component.length > 1 || (adjacency.get(component[0]) ?? []).some((edge) => edge.target === component[0]))
    .map((component) => materializeCycle(component, adjacency))
    .sort((left, right) => left.nodes.join("|").localeCompare(right.nodes.join("|")));
}

function materializeCycle(component, adjacency) {
  const allowed = new Set(component);
  const start = component[0];
  const search = (current, visited, pathEdges) => {
    for (const edge of adjacency.get(current) ?? []) {
      if (!allowed.has(edge.target)) continue;
      if (edge.target === start) return [...pathEdges, edge];
      if (!visited.has(edge.target)) {
        const next = new Set(visited).add(edge.target);
        const found = search(edge.target, next, [...pathEdges, edge]);
        if (found) return found;
      }
    }
    return null;
  };
  const edges = search(start, new Set([start]), []) ?? [];
  return { nodes: edges.length > 0 ? edges.map((edge) => edge.importer) : component, edges };
}

function classifySource(path) {
  if (path === "src/App.tsx") return { layer: "root-app" };
  if (path === "src/main.tsx") return { layer: "bootstrap" };
  if (path === "src/vite-env.d.ts") return { layer: "ambient" };
  if (path.startsWith("src/shared/")) return { layer: "shared" };
  if (path.startsWith("src/api/")) return { layer: "api" };
  if (path.startsWith("src/app/")) return { layer: "app-core" };
  if (path.startsWith("src/shell/")) return { layer: "shell" };
  if (path.startsWith("src/workspace/")) return { layer: "workspace" };
  const feature = path.match(/^src\/features\/([^/]+)\//);
  if (feature) return { layer: "feature", feature: feature[1] };
  const moduleFeature = path.match(/^src\/modules\/([^/]+)\//);
  if (moduleFeature && !["catalogLoader", "moduleCatalog", "staticCatalog"].includes(moduleFeature[1])) {
    return { layer: "feature", feature: moduleFeature[1] };
  }
  if (path === "src/modules/voiceprint.tsx") return { layer: "feature", feature: "voiceprint" };
  if ([
    "src/modules/catalogLoader.ts",
    "src/modules/moduleCatalog.ts",
    "src/modules/staticCatalog.ts"
  ].includes(path)) return { layer: "catalog" };
  return null;
}

function isManagedSource(path, classification) {
  if (!classification) return false;
  return classification.layer !== "ambient";
}

function importedSymbols(node) {
  const clause = node.importClause;
  if (!clause) return ["*side-effect*"];
  const names = [];
  if (clause.name) names.push("default");
  if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) names.push("*");
  if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
    for (const element of clause.namedBindings.elements) names.push(element.propertyName?.text ?? element.name.text);
  }
  return names;
}

function exportedSymbols(node) {
  if (!node.exportClause) return ["*"];
  if (ts.isNamespaceExport(node.exportClause)) return [node.exportClause.name.text];
  return node.exportClause.elements.map((element) => element.propertyName?.text ?? element.name.text);
}

function importKind(node) {
  const clause = node.importClause;
  if (!clause) return "runtime";
  if (clause.isTypeOnly) return "type";
  if (clause.name) return "runtime";
  if (ts.isNamedImports(clause.namedBindings)
    && clause.namedBindings.elements.length > 0
    && clause.namedBindings.elements.every((element) => element.isTypeOnly)) return "type";
  return "runtime";
}

function findReactUseStateBindings(sourceFile) {
  const direct = new Set();
  const namespaces = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement)
      || !ts.isStringLiteralLike(statement.moduleSpecifier)
      || statement.moduleSpecifier.text !== "react"
      || !statement.importClause) continue;
    if (statement.importClause.name) namespaces.add(statement.importClause.name.text);
    const bindings = statement.importClause.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings)) namespaces.add(bindings.name.text);
    if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        if ((element.propertyName?.text ?? element.name.text) === "useState") direct.add(element.name.text);
      }
    }
  }
  return { direct, namespaces };
}

function isUseStateCall(expression, bindings) {
  if (ts.isIdentifier(expression)) return bindings.direct.has(expression.text);
  return ts.isPropertyAccessExpression(expression)
    && expression.name.text === "useState"
    && ts.isIdentifier(expression.expression)
    && bindings.namespaces.has(expression.expression.text);
}

function isFunctionNode(node) {
  return ts.isFunctionDeclaration(node)
    || ts.isFunctionExpression(node)
    || ts.isArrowFunction(node)
    || ts.isMethodDeclaration(node)
    || ts.isGetAccessorDeclaration(node)
    || ts.isSetAccessorDeclaration(node)
    || ts.isConstructorDeclaration(node);
}

function isHookOrService(path) {
  const name = path.split("/").at(-1) ?? "";
  const stem = name.replace(/(?:\.d)?\.[^.]+$/, "");
  return /\/(?:hooks?|services?)\//.test(path)
    || /\.(?:hook|service)\.[cm]?[jt]sx?$/.test(name)
    || /(?:Hook|Service|Action|Actions|Operation|Operations|Gateway|PollingActions)$/.test(stem)
    || /^use(?:[-_]|[A-Z])/.test(stem);
}

function isGenericFileName(path, names) {
  const raw = (path.split("/").at(-1) ?? "").replace(/(?:\.d)?\.[^.]+$/, "");
  const normalized = raw.toLowerCase().replace(/[-_]/g, "");
  return names.includes(normalized);
}

function isFeaturePublicEntry(path) {
  return /^src\/(?:features|modules)\/[^/]+\/index\.[cm]?[jt]sx?$/.test(path);
}

function isModuleEntry(path) {
  if (isFeaturePublicEntry(path)) return true;
  if (classifySource(path)?.layer !== "feature") return false;
  const name = path.split("/").at(-1) ?? "";
  return /(?:Module|Workspace)\.[cm]?[jt]sx?$/.test(name);
}

function isExpectedInternalSpecifier(specifier, compilerOptions) {
  if (specifier.startsWith(".")) return true;
  return Object.keys(compilerOptions.paths ?? {}).some((pattern) => {
    const prefix = pattern.split("*")[0];
    return specifier.startsWith(prefix);
  });
}

function isProjectSource(rootDir, fileName) {
  const path = projectPath(rootDir, fileName);
  return path.startsWith("src/") && SOURCE_EXTENSION.test(path);
}

function projectPath(rootDir, fileName) {
  return relative(rootDir, resolve(fileName)).split(sep).join("/");
}

function countLines(source) {
  if (source.length === 0) return 0;
  const lines = source.split(/\r?\n/).length;
  return /\r?\n$/.test(source) ? lines - 1 : lines;
}

function lineOf(sourceFile, node) {
  return sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
}

function flattenDiagnostic(diagnostic) {
  return ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n");
}

function addError(errors, code, path, line, message) {
  errors.push({ code, path, line, message });
}

function compareEdges(left, right) {
  return left.target.localeCompare(right.target)
    || left.importer.localeCompare(right.importer)
    || left.line - right.line
    || left.kind.localeCompare(right.kind);
}

function finishReport(errors, edges, cycles, metrics) {
  const unique = new Map();
  for (const error of errors) unique.set(`${error.code}\0${error.path}\0${error.line}\0${error.message}`, error);
  const sorted = [...unique.values()].sort((left, right) => left.path.localeCompare(right.path)
    || left.line - right.line
    || left.code.localeCompare(right.code)
    || left.message.localeCompare(right.message));
  return { ok: sorted.length === 0, errors: sorted, edges: [...edges].sort(compareEdges), cycles, metrics };
}
