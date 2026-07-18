import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, extname, normalize, resolve } from "node:path";
import ts from "typescript";

const virtualPrefix = "\0auris-feature-controller:";

function normalizePath(path) {
  return normalize(path).replaceAll("\\", "/");
}

function walkSourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) return walkSourceFiles(path);
    return /\.tsx?$/.test(entry.name) ? [normalizePath(path)] : [];
  });
}

function propertyNameText(name) {
  if (ts.isIdentifier(name) || ts.isStringLiteralLike(name) || ts.isNumericLiteral(name)) return name.text;
  return null;
}

function unwrapExpression(expression) {
  let current = expression;
  while (
    ts.isParenthesizedExpression(current) ||
    ts.isAsExpression(current) ||
    ts.isTypeAssertionExpression(current) ||
    ts.isNonNullExpression(current) ||
    ts.isSatisfiesExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}

function controllerBindingState(node, inherited) {
  if (!ts.isFunctionLike(node)) {
    if (ts.isCatchClause(node) && node.variableDeclaration && bindingNames(node.variableDeclaration.name).includes("controller")) {
      return false;
    }
    return inherited;
  }
  let declaresController = false;
  let receivesControllerProp = false;
  for (const parameter of node.parameters) {
    if (bindingNames(parameter.name).includes("controller")) declaresController = true;
    if (!ts.isObjectBindingPattern(parameter.name)) continue;
    for (const element of parameter.name.elements) {
      const property = element.propertyName
        ? propertyNameText(element.propertyName)
        : ts.isIdentifier(element.name)
          ? element.name.text
          : null;
      if (property === "controller" && bindingNames(element.name).includes("controller")) {
        receivesControllerProp = true;
      }
    }
  }
  return declaresController ? receivesControllerProp : inherited;
}

function assertNoControllerShadow(node, active, sourceFile) {
  if (!active || !ts.isVariableDeclaration(node) || !bindingNames(node.name).includes("controller")) return;
  const line = sourceFile.getLineAndCharacterOfPosition(node.getStart()).line + 1;
  throw new Error(`controller 组件参数不得被局部变量遮蔽：${sourceFile.fileName}:${line}`);
}

export function controllerPropertyNames(featureRoot, includePath = () => true) {
  const properties = new Set();
  for (const path of walkSourceFiles(featureRoot).filter(includePath)) {
    const source = readFileSync(path, "utf8");
    const kind = path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const sourceFile = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, kind);
    const visit = (node, inheritedActive = false) => {
      const active = controllerBindingState(node, inheritedActive);
      assertNoControllerShadow(node, active, sourceFile);
      if (active && ts.isPropertyAccessExpression(node)) {
        const expression = unwrapExpression(node.expression);
        if (ts.isIdentifier(expression) && expression.text === "controller") properties.add(node.name.text);
      } else if (active && ts.isElementAccessExpression(node)) {
        const expression = unwrapExpression(node.expression);
        if (ts.isIdentifier(expression) && expression.text === "controller") {
          const argument = node.argumentExpression && unwrapExpression(node.argumentExpression);
          if (!argument || !ts.isStringLiteralLike(argument)) {
            const line = sourceFile.getLineAndCharacterOfPosition(node.getStart()).line + 1;
            throw new Error(`controller 只允许静态键访问：${path}:${line}`);
          }
          properties.add(argument.text);
        }
      } else if (active && ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) {
        const initializer = unwrapExpression(node.initializer);
        if (ts.isIdentifier(initializer) && initializer.text === "controller") {
          for (const element of node.name.elements) {
            if (element.dotDotDotToken) throw new Error(`controller 不允许 rest 解构：${path}`);
            const key = element.propertyName
              ? propertyNameText(element.propertyName)
              : ts.isIdentifier(element.name)
                ? element.name.text
                : null;
            if (!key) throw new Error(`controller 解构键不可静态分析：${path}`);
            properties.add(key);
          }
        }
      }
      ts.forEachChild(node, (child) => visit(child, active));
    };
    visit(sourceFile);
  }
  return [...properties].sort();
}

function shortPropertyName(index) {
  const alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  let value = index;
  let output = "";
  do {
    output = alphabet[value % alphabet.length] + output;
    value = Math.floor(value / alphabet.length) - 1;
  } while (value >= 0);
  return output;
}

function createPropertyMap(featureRoot, includePath) {
  return new Map(controllerPropertyNames(featureRoot, includePath).map((name, index) => [name, shortPropertyName(index)]));
}

function resolveSourceFile(fromFile, specifier) {
  if (!specifier.startsWith(".") && !specifier.startsWith("/")) return specifier;
  const candidate = specifier.startsWith("/")
    ? specifier
    : resolve(dirname(fromFile), specifier);
  if (extname(candidate)) return normalizePath(candidate);
  for (const extension of [".ts", ".tsx", ".js", ".jsx"]) {
    if (existsSync(`${candidate}${extension}`)) return normalizePath(`${candidate}${extension}`);
  }
  if (existsSync(candidate)) return normalizePath(candidate);
  throw new Error(`无法解析内联依赖：${specifier}（来自 ${fromFile}）`);
}

function bindingNames(name, output = []) {
  if (ts.isIdentifier(name)) output.push(name.text);
  else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
    for (const element of name.elements) {
      if (ts.isBindingElement(element)) bindingNames(element.name, output);
    }
  }
  return output;
}

function topLevelBindings(statement) {
  const names = [];
  if (ts.isVariableStatement(statement)) {
    for (const declaration of statement.declarationList.declarations) {
      bindingNames(declaration.name, names);
    }
  } else if (
    (ts.isFunctionDeclaration(statement) ||
      ts.isClassDeclaration(statement) ||
      ts.isEnumDeclaration(statement) ||
      ts.isTypeAliasDeclaration(statement) ||
      ts.isInterfaceDeclaration(statement)) &&
    statement.name
  ) {
    names.push(statement.name.text);
  }
  return names;
}

function objectBindingDependencies(statement, parameterNames) {
  if (!ts.isVariableStatement(statement) || statement.declarationList.declarations.length !== 1) return null;
  const declaration = statement.declarationList.declarations[0];
  if (!ts.isObjectBindingPattern(declaration.name) || !ts.isIdentifier(declaration.initializer)) return null;
  if (!parameterNames.has(declaration.initializer.text)) return null;
  return bindingNames(declaration.name);
}

function directParameterReferences(statement, parameterNames) {
  const references = new Set();
  const visit = (node) => {
    if (ts.isIdentifier(node) && parameterNames.has(node.text)) {
      const parent = node.parent;
      const isPropertyName =
        (ts.isPropertyAccessExpression(parent) && parent.name === node)
        || (ts.isPropertyAssignment(parent) && parent.name === node)
        || (ts.isMethodDeclaration(parent) && parent.name === node)
        || (ts.isPropertyDeclaration(parent) && parent.name === node)
        || (ts.isPropertySignature(parent) && parent.name === node);
      if (!isPropertyName) references.add(node.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(statement);
  return references;
}

function sameFileRuntimeReferences(fn, sourceFile) {
  const moduleBindings = new Map();
  for (const statement of sourceFile.statements) {
    if (statement === fn) continue;
    if (!(
      ts.isVariableStatement(statement)
      || ts.isFunctionDeclaration(statement)
      || ts.isClassDeclaration(statement)
      || ts.isEnumDeclaration(statement)
    )) continue;
    const line = sourceFile.getLineAndCharacterOfPosition(statement.getStart()).line + 1;
    for (const name of topLevelBindings(statement)) moduleBindings.set(name, line);
  }
  if (!moduleBindings.size) return new Map();

  const localBindings = new Set();
  for (const parameter of fn.parameters) {
    for (const name of bindingNames(parameter.name)) localBindings.add(name);
  }
  const collectLocals = (node) => {
    if (ts.isVariableDeclaration(node) || ts.isParameter(node)) {
      for (const name of bindingNames(node.name)) localBindings.add(name);
    } else if (
      (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isEnumDeclaration(node))
      && node.name
    ) {
      localBindings.add(node.name.text);
    } else if (ts.isCatchClause(node) && node.variableDeclaration) {
      for (const name of bindingNames(node.variableDeclaration.name)) localBindings.add(name);
    }
    ts.forEachChild(node, collectLocals);
  };
  collectLocals(fn.body);

  const references = new Map();
  const isTypePosition = (node) => {
    let current = node.parent;
    while (current && current !== fn.body) {
      if (ts.isTypeNode(current)) return true;
      if (ts.isExpression(current) || ts.isStatement(current)) return false;
      current = current.parent;
    }
    return false;
  };
  const visit = (node) => {
    if (ts.isIdentifier(node) && moduleBindings.has(node.text) && !localBindings.has(node.text)) {
      const parent = node.parent;
      const isPropertyName =
        (ts.isPropertyAccessExpression(parent) && parent.name === node)
        || (ts.isPropertyAssignment(parent) && parent.name === node)
        || (ts.isMethodDeclaration(parent) && parent.name === node)
        || (ts.isPropertyDeclaration(parent) && parent.name === node)
        || (ts.isPropertySignature(parent) && parent.name === node)
        || (ts.isQualifiedName(parent) && parent.right === node)
        || (ts.isLabeledStatement(parent) && parent.label === node)
        || (ts.isBreakOrContinueStatement(parent) && parent.label === node);
      if (!isPropertyName && !isTypePosition(node)) {
        references.set(node.text, moduleBindings.get(node.text));
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(fn.body);
  return references;
}

function runtimeImports(sourceFile, sourcePath, importRegistry) {
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteralLike(statement.moduleSpecifier)) continue;
    const modulePath = resolveSourceFile(sourcePath, statement.moduleSpecifier.text);
    const clause = statement.importClause;
    if (!clause) {
      importRegistry.addSideEffect(modulePath);
      continue;
    }
    if (clause.isTypeOnly) continue;
    if (clause.name) importRegistry.addDefault(clause.name.text, modulePath);
    const bindings = clause.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings)) {
      importRegistry.addNamespace(bindings.name.text, modulePath);
    } else if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        if (element.isTypeOnly) continue;
        importRegistry.addNamed(
          element.propertyName?.text ?? element.name.text,
          element.name.text,
          modulePath
        );
      }
    }
  }
}

function createImportRegistry() {
  const byLocal = new Map();
  const entries = [];
  const sideEffects = new Set();
  const add = (entry) => {
    const signature = JSON.stringify(entry);
    const prior = byLocal.get(entry.local);
    if (prior && prior !== signature) {
      throw new Error(`内联模块存在冲突 import：${entry.local}`);
    }
    if (prior) return;
    byLocal.set(entry.local, signature);
    entries.push(entry);
  };
  return {
    addDefault(local, modulePath) {
      add({ kind: "default", local, modulePath });
    },
    addNamespace(local, modulePath) {
      add({ kind: "namespace", local, modulePath });
    },
    addNamed(imported, local, modulePath) {
      add({ kind: "named", imported, local, modulePath });
    },
    addSideEffect(modulePath) {
      sideEffects.add(modulePath);
    },
    hasLocal(local) {
      return byLocal.has(local);
    },
    render() {
      const lines = [...sideEffects].sort().map((modulePath) => `import ${JSON.stringify(modulePath)};`);
      for (const entry of entries) {
        if (entry.kind === "default") {
          lines.push(`import ${entry.local} from ${JSON.stringify(entry.modulePath)};`);
        } else if (entry.kind === "namespace") {
          lines.push(`import * as ${entry.local} from ${JSON.stringify(entry.modulePath)};`);
        } else {
          const binding = entry.imported === entry.local
            ? entry.local
            : `${entry.imported} as ${entry.local}`;
          lines.push(`import { ${binding} } from ${JSON.stringify(entry.modulePath)};`);
        }
      }
      return lines.join("\n");
    }
  };
}

export function controllerGroups(controllerPath, exportName) {
  const source = readFileSync(controllerPath, "utf8");
  const sourceFile = ts.createSourceFile(controllerPath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const importedGroups = new Map();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteralLike(statement.moduleSpecifier)) continue;
    if (!statement.moduleSpecifier.text.startsWith(".")) continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      if (element.isTypeOnly) continue;
      const imported = element.propertyName?.text ?? element.name.text;
      if (!/^(?:use|build|create)[A-Z]/.test(imported)) continue;
      importedGroups.set(element.name.text, {
        functionName: imported,
        sourcePath: resolveSourceFile(controllerPath, statement.moduleSpecifier.text)
      });
    }
  }
  const controller = sourceFile.statements.find(
    (statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === exportName
  );
  if (!controller || !ts.isFunctionDeclaration(controller) || !controller.body) {
    throw new Error(`未找到 controller 函数 ${exportName}：${controllerPath}`);
  }
  const groups = [];
  const seen = new Set();
  const visit = (node) => {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      const group = importedGroups.get(node.expression.text);
      if (group) {
        if (seen.has(node.expression.text)) throw new Error(`${exportName} 重复调用 ${node.expression.text}`);
        seen.add(node.expression.text);
        groups.push(group);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(controller.body);
  if (groups.length < 2 || groups.length !== importedGroups.size) {
    throw new Error(`${exportName} 调用序列与分组 import 不一致：calls=${groups.length}, imports=${importedGroups.size}`);
  }
  return groups;
}

export function generateController(spec) {
  const imports = createImportRegistry();
  const printer = ts.createPrinter({ newLine: ts.NewLineKind.LineFeed, removeComments: false });
  const body = [];
  const declared = new Set();
  const dependencies = new Set();
  const returnedProperties = new Map();
  const watchedFiles = [spec.controllerPath];

  for (const group of controllerGroups(spec.controllerPath, spec.exportName)) {
    watchedFiles.push(group.sourcePath);
    const source = readFileSync(group.sourcePath, "utf8");
    const kind = group.sourcePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
    const sourceFile = ts.createSourceFile(group.sourcePath, source, ts.ScriptTarget.Latest, true, kind);
    runtimeImports(sourceFile, group.sourcePath, imports);
    const fn = sourceFile.statements.find(
      (statement) => ts.isFunctionDeclaration(statement) && statement.name?.text === group.functionName
    );
    if (!fn || !ts.isFunctionDeclaration(fn) || !fn.body) {
      throw new Error(`未找到内联函数 ${group.functionName}：${group.sourcePath}`);
    }
    const sameFileReferences = sameFileRuntimeReferences(fn, sourceFile);
    if (sameFileReferences.size) {
      throw new Error(
        `${group.functionName} 依赖不会随函数体内联的同文件模块级运行时绑定：${[...sameFileReferences]
          .map(([name, line]) => `${name}@${line}`)
          .join(", ")}；请移入函数体或提取为独立 import`
      );
    }

    const parameterNames = new Set();
    for (const parameter of fn.parameters) {
      for (const name of bindingNames(parameter.name)) parameterNames.add(name);
    }
    const statements = [...fn.body.statements];
    const finalReturn = statements.at(-1);
    if (!finalReturn || !ts.isReturnStatement(finalReturn) || !finalReturn.expression) {
      throw new Error(`${group.functionName} 必须以 return 收口`);
    }
    if (!ts.isObjectLiteralExpression(finalReturn.expression) && !ts.isIdentifier(finalReturn.expression)) {
      throw new Error(`${group.functionName} 的 return 必须是对象字面量或原 context`);
    }

    for (const parameterName of parameterNames) {
      if (parameterName !== "scope" && parameterName !== "props") dependencies.add(parameterName);
    }

    for (const statement of statements.slice(0, -1)) {
      const generatedDependencies = objectBindingDependencies(statement, parameterNames);
      if (generatedDependencies) {
        for (const name of generatedDependencies) dependencies.add(name);
        continue;
      }
      const eliminatedObjectParameters = new Set(
        [...parameterNames].filter((name) => name === "scope" || name === "props")
      );
      const directParameters = directParameterReferences(statement, eliminatedObjectParameters);
      if (directParameters.size) {
        throw new Error(
          `${group.functionName} 内联后仍直接引用形参 ${[...directParameters].sort().join(", ")}`
        );
      }
      for (const name of topLevelBindings(statement)) {
        if (declared.has(name)) throw new Error(`内联后出现重复顶层声明 ${name}：${group.functionName}`);
        declared.add(name);
      }
      body.push(printer.printNode(ts.EmitHint.Unspecified, statement, sourceFile));
    }

    for (const property of ts.isObjectLiteralExpression(finalReturn.expression) ? finalReturn.expression.properties : []) {
      if (ts.isSpreadAssignment(property)) {
        const expression = unwrapExpression(property.expression);
        if (ts.isIdentifier(expression) && parameterNames.has(expression.text)) continue;
        throw new Error(`${group.functionName} 只允许透传原始 context/props spread：${group.sourcePath}`);
      }
      if (ts.isShorthandPropertyAssignment(property)) {
        returnedProperties.set(property.name.text, property.name.text);
      } else if (ts.isPropertyAssignment(property)) {
        const key = propertyNameText(property.name);
        if (!key) throw new Error(`${group.functionName} 返回键不可静态分析`);
        returnedProperties.set(key, printer.printNode(ts.EmitHint.Expression, property.initializer, sourceFile));
      } else {
        throw new Error(`${group.functionName} 返回对象只允许显式属性：${group.sourcePath}`);
      }
    }
  }

  const propDependencies = [...dependencies]
    .filter((name) => !declared.has(name) && !imports.hasLocal(name) && name !== "props")
    .sort();
  const propsLine = propDependencies.length
    ? `  const { ${propDependencies.join(", ")} } = props;\n`
    : "";
  const returnLines = [...spec.propertyMap].map(([property, alias]) => {
    const expression = returnedProperties.get(property) ?? `props.${property}`;
    return `    ${alias}: ${expression}`;
  }).join(",\n");
  const code = `${imports.render()}\n\nexport function ${spec.exportName}(props) {\n${propsLine}${body.map((statement) => `  ${statement.replaceAll("\n", "\n  ")}`).join("\n\n")}\n\n  return {\n${returnLines}\n  };\n}\n`;
  return { code, watchedFiles };
}

export function transformControllerConsumers(code, id, propertyMap) {
  const kind = id.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(id, code, ts.ScriptTarget.Latest, true, kind);
  const transformer = (context) => {
    const { factory } = context;
    const visit = (node, inheritedActive = false) => {
      const active = controllerBindingState(node, inheritedActive);
      assertNoControllerShadow(node, active, sourceFile);
      if (active && ts.isPropertyAccessExpression(node)) {
        const expression = unwrapExpression(node.expression);
        const alias = ts.isIdentifier(expression) && expression.text === "controller"
          ? propertyMap.get(node.name.text)
          : undefined;
        if (alias) return factory.updatePropertyAccessExpression(node, node.expression, factory.createIdentifier(alias));
      }
      if (active && ts.isElementAccessExpression(node)) {
        const expression = unwrapExpression(node.expression);
        const argument = node.argumentExpression && unwrapExpression(node.argumentExpression);
        const alias = ts.isIdentifier(expression) && expression.text === "controller" && argument && ts.isStringLiteralLike(argument)
          ? propertyMap.get(argument.text)
          : undefined;
        if (alias) return factory.createPropertyAccessExpression(node.expression, alias);
      }
      if (active && ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) {
        const initializer = unwrapExpression(node.initializer);
        if (ts.isIdentifier(initializer) && initializer.text === "controller") {
          const elements = node.name.elements.map((element) => {
            const key = element.propertyName
              ? propertyNameText(element.propertyName)
              : ts.isIdentifier(element.name)
                ? element.name.text
                : null;
            const alias = key ? propertyMap.get(key) : undefined;
            if (!alias) return element;
            return factory.updateBindingElement(
              element,
              element.dotDotDotToken,
              factory.createIdentifier(alias),
              element.name,
              element.initializer
            );
          });
          return factory.updateVariableDeclaration(
            node,
            factory.updateObjectBindingPattern(node.name, elements),
            node.exclamationToken,
            node.type,
            node.initializer
          );
        }
      }
      return ts.visitEachChild(node, (child) => visit(child, active), context);
    };
    return (node) => ts.visitNode(node, (child) => visit(child, false));
  };
  const result = ts.transform(sourceFile, [transformer]);
  try {
    return ts.createPrinter({ newLine: ts.NewLineKind.LineFeed, removeComments: false })
      .printFile(result.transformed[0]);
  } finally {
    result.dispose();
  }
}

export function inlineFeatureControllers(root) {
  const specs = [
    ["canvas", "src/features/canvas/controller/useCanvasController.ts", "useCanvasController", "src/features/canvas"],
    ["labels", "src/features/labels/controller/useLabelsController.ts", "useLabelsController", "src/features/labels"],
    ["evaluation", "src/features/evaluation/controller/useEvaluationController.ts", "useEvaluationController", "src/features/evaluation"],
    ["insights", "src/features/insights/controller/useInsightsController.ts", "useInsightsController", "src/features/insights"],
    ["listening", "src/features/listening/hooks/useListeningController.ts", "useListeningController", "src/features/listening"],
    ["listening-matrix", "src/features/listening/components/matrix/useMatrixController.ts", "useMatrixController", "src/features/listening/components/matrix"],
    ["listening-annotation", "src/features/listening/components/evidence/minimap/useAnnotationMinimapController.ts", "useAnnotationMinimapController", "src/features/listening/components/evidence/minimap"],
    ["listening-waveform", "src/features/listening/components/evidence/waveform/useWaveformPanelController.ts", "useWaveformPanelController", "src/features/listening/components/evidence/waveform"],
    ["listening-panel", "src/features/listening/components/evidence/panel/useEvidencePanelController.ts", "useEvidencePanelController", "src/features/listening/components/evidence/panel"]
  ].map(([name, path, exportName, featureRootPath]) => {
    const featureRoot = normalizePath(resolve(root, featureRootPath));
    const includeConsumerPath = name === "labels"
      ? (sourcePath) => !/\/components\/(?:Legacy|LabelsLegacy)/.test(normalizePath(sourcePath))
      : name === "listening"
        ? (sourcePath) => normalizePath(sourcePath) === normalizePath(resolve(featureRoot, "components/ListeningFeatureView.tsx"))
        : () => true;
    const propertyMap = createPropertyMap(featureRoot, includeConsumerPath);
    if (propertyMap.size === 0) {
      throw new Error(`${name} controller 没有可验证的显式消费属性；禁止通过对象 spread 隐式传递`);
    }
    return {
      name,
      controllerPath: normalizePath(resolve(root, path)),
      featureRoot,
      includeConsumerPath,
      exportName,
      virtualId: `${virtualPrefix}${name}`,
      propertyMap
    };
  });

  const byVirtualId = new Map(specs.map((spec) => [spec.virtualId, spec]));
  return {
    name: "auris-inline-feature-controllers",
    apply: "build",
    enforce: "pre",
    resolveId(source, importer) {
      if (!importer) return null;
      const importerPath = importer.split("?")[0].replace(/^\0/, "");
      const unresolved = source.startsWith("/") ? source : resolve(dirname(importerPath), source);
      const normalized = normalizePath(unresolved).replace(/\.(?:tsx?|jsx?)$/, "");
      const spec = specs.find((candidate) =>
        candidate.controllerPath.replace(/\.(?:tsx?|jsx?)$/, "") === normalized
      );
      return spec?.virtualId ?? null;
    },
    load(id) {
      const spec = byVirtualId.get(id);
      if (!spec) return null;
      const generated = generateController(spec);
      for (const path of generated.watchedFiles) this.addWatchFile(path);
      return { code: generated.code, moduleType: "tsx" };
    },
    transform(code, id) {
      const cleanId = normalizePath(id.split("?")[0]);
      const spec = specs.find((candidate) =>
        cleanId.startsWith(`${candidate.featureRoot}/`) && candidate.includeConsumerPath(cleanId)
      );
      if (!spec || !/\.tsx?$/.test(cleanId) || cleanId === spec.controllerPath) return null;
      return {
        code: transformControllerConsumers(code, cleanId, spec.propertyMap),
        map: null,
        moduleType: cleanId.endsWith(".tsx") ? "tsx" : "ts"
      };
    }
  };
}
