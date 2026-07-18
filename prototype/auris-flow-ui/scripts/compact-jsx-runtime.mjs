import ts from "typescript";
import { brotliCompressSync, constants as zlibConstants } from "node:zlib";

const runtimeChunkMarker = "_auris-react-jsx-runtime";

function propertyName(name) {
  if (ts.isIdentifier(name) || ts.isStringLiteralLike(name)) return name.text;
  return null;
}

function collectBindingNames(name, output) {
  if (ts.isIdentifier(name)) {
    output.push(name.text);
    return;
  }
  if (!ts.isObjectBindingPattern(name) && !ts.isArrayBindingPattern(name)) return;
  for (const element of name.elements) {
    if (ts.isBindingElement(element)) collectBindingNames(element.name, output);
  }
}

function isFunctionScope(node) {
  return ts.isFunctionLike(node);
}

function isLexicalScope(node) {
  return (
    ts.isSourceFile(node) ||
    isFunctionScope(node) ||
    ts.isBlock(node) ||
    ts.isCaseBlock(node) ||
    ts.isCatchClause(node) ||
    ts.isForStatement(node) ||
    ts.isForInStatement(node) ||
    ts.isForOfStatement(node)
  );
}

function nearestScope(node, predicate) {
  let current = node.parent;
  while (current && !predicate(current)) current = current.parent;
  return current;
}

function lexicalScope(node) {
  return nearestScope(node, isLexicalScope);
}

function functionScope(node) {
  return nearestScope(node, (candidate) => ts.isSourceFile(candidate) || isFunctionScope(candidate));
}

function scopeDepth(scope) {
  let depth = 0;
  for (let current = scope; current; current = current.parent) depth += 1;
  return depth;
}

function scopeContains(scope, node) {
  for (let current = node; current; current = current.parent) {
    if (current === scope) return true;
  }
  return false;
}

function freshName(identifiers, prefix) {
  for (let index = 0; ; index += 1) {
    const name = `${prefix}${index.toString(36)}`;
    if (!identifiers.has(name)) {
      identifiers.add(name);
      return name;
    }
  }
}

function createBindingRegistry(sourceFile) {
  const bindings = new Map();

  const register = (name, scope, kind, declaration) => {
    if (!scope) return;
    const records = bindings.get(name) ?? [];
    records.push({ scope, depth: scopeDepth(scope), kind, declaration });
    bindings.set(name, records);
  };

  const registerName = (name, scope, kind, declaration) => {
    const names = [];
    collectBindingNames(name, names);
    for (const binding of names) register(binding, scope, kind, declaration);
  };

  const visit = (node) => {
    if (ts.isImportClause(node)) {
      if (node.name) register(node.name.text, sourceFile, "import", node);
    } else if (ts.isImportSpecifier(node) || ts.isNamespaceImport(node)) {
      register(node.name.text, sourceFile, "import", node);
    } else if (ts.isParameter(node)) {
      registerName(node.name, nearestScope(node, isFunctionScope), "parameter", node);
    } else if (ts.isVariableDeclaration(node)) {
      const declarationList = ts.findAncestor(node, ts.isVariableDeclarationList);
      const blockScoped = Boolean(declarationList?.flags & ts.NodeFlags.BlockScoped);
      registerName(node.name, blockScoped ? lexicalScope(node) : functionScope(node), "variable", node);
    } else if (ts.isFunctionDeclaration(node) && node.name) {
      register(node.name.text, lexicalScope(node), "function", node);
    } else if ((ts.isFunctionExpression(node) || ts.isClassExpression(node)) && node.name) {
      register(node.name.text, node, "local-name", node);
    } else if (ts.isClassDeclaration(node) && node.name) {
      register(node.name.text, lexicalScope(node), "class", node);
    } else if (ts.isCatchClause(node) && node.variableDeclaration) {
      registerName(node.variableDeclaration.name, node, "catch", node.variableDeclaration);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);

  const resolve = (identifier) => {
    const candidates = (bindings.get(identifier.text) ?? [])
      .filter((record) => scopeContains(record.scope, identifier))
      .sort((left, right) => right.depth - left.depth);
    if (!candidates.length) return null;
    const nearestDepth = candidates[0].depth;
    const nearest = candidates.filter((candidate) => candidate.depth === nearestDepth);
    return nearest.length === 1 ? nearest[0] : null;
  };

  return { bindings, resolve };
}

function runtimeImportBindings(sourceFile) {
  const declarations = new Set();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    if (!statement.moduleSpecifier.text.includes(runtimeChunkMarker)) continue;
    const namedBindings = statement.importClause?.namedBindings;
    if (!namedBindings || !ts.isNamedImports(namedBindings)) continue;
    for (const element of namedBindings.elements) declarations.add(element);
  }
  return declarations;
}

function helperInsertionPoint(sourceFile) {
  let position = sourceFile.statements[0]?.getStart(sourceFile) ?? sourceFile.end;
  for (const statement of sourceFile.statements) {
    const directive = ts.isExpressionStatement(statement) && ts.isStringLiteral(statement.expression);
    if (!ts.isImportDeclaration(statement) && !directive) break;
    position = statement.end;
  }
  return position;
}

export function compactJsxRuntimeChunk(code, id = "chunk.js", options = {}) {
  const {
    aliasTags = true,
    aliasProps = true,
    aliasValues = true,
    compactChildren = true
  } = options;
  const sourceFile = ts.createSourceFile(id, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const runtimeImports = runtimeImportBindings(sourceFile);
  if (!runtimeImports.size) return { code, changed: false, summaries: [] };

  const identifiers = new Set();
  const registry = createBindingRegistry(sourceFile);
  for (const records of registry.bindings.values()) {
    for (const record of records) {
      const name = record.declaration.name;
      if (name && ts.isIdentifier(name)) identifiers.add(name.text);
    }
  }
  const collectIdentifiers = (node) => {
    if (ts.isIdentifier(node)) identifiers.add(node.text);
    ts.forEachChild(node, collectIdentifiers);
  };
  collectIdentifiers(sourceFile);

  const topLevelObjectBinding = (registry.bindings.get("Object") ?? []).some(
    (record) => record.scope === sourceFile
  );

  const candidates = new Map();
  const tagUsages = new Map();
  const propUsages = new Map();
  const valueUsages = new Map();
  let runtimeCalls = 0;
  const isRuntimeCall = (node) => {
    if (!ts.isCallExpression(node) || !ts.isIdentifier(node.expression)) return false;
    const binding = registry.resolve(node.expression);
    return Boolean(binding && runtimeImports.has(binding.declaration));
  };
  const isSafeStringValue = (node) => {
    const parent = node.parent;
    if (
      (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) &&
      parent.moduleSpecifier === node
    ) return false;
    if (ts.isExpressionStatement(parent) && parent.expression === node) return false;
    if (ts.isTaggedTemplateExpression(parent) && parent.template === node) return false;
    if ("name" in parent && parent.name === node) return false;
    if (
      ts.isCallExpression(parent) &&
      parent.arguments[0] === node &&
      (parent.expression.kind === ts.SyntaxKind.ImportKeyword ||
        (ts.isIdentifier(parent.expression) && ["eval", "require", "Function"].includes(parent.expression.text)))
    ) return false;
    if (
      ts.isNewExpression(parent) &&
      parent.arguments?.[0] === node &&
      ts.isIdentifier(parent.expression) &&
      parent.expression.text === "Function"
    ) return false;
    return true;
  };
  const collectValueLiterals = (node) => {
    if (ts.isStringLiteralLike(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      const parent = node.parent;
      if (ts.isCallExpression(parent) && parent.arguments[0] === node && isRuntimeCall(parent)) return;
      if (!isSafeStringValue(node)) return;
      const usage = valueUsages.get(node.text) ?? {
        literal: code.slice(node.getStart(sourceFile), node.end),
        nodes: new Set()
      };
      usage.nodes.add(node);
      valueUsages.set(node.text, usage);
      return;
    }
    ts.forEachChild(node, collectValueLiterals);
  };
  const scan = (node) => {
    if (isRuntimeCall(node)) {
        runtimeCalls += 1;
        const tag = node.arguments[0];
        if (tag && (ts.isStringLiteralLike(tag) || ts.isNoSubstitutionTemplateLiteral(tag))) {
          const usage = tagUsages.get(tag.text) ?? { literal: code.slice(tag.getStart(sourceFile), tag.end), nodes: [] };
          usage.nodes.push(tag);
          tagUsages.set(tag.text, usage);
        }
        const props = node.arguments[1];
        const childrenProperty = props && ts.isObjectLiteralExpression(props)
          ? props.properties.at(-1)
          : null;
        const supportedArguments =
          (node.arguments.length === 2 || node.arguments.length === 3) &&
          !node.arguments.some(ts.isSpreadElement);
        if (
          supportedArguments &&
          childrenProperty &&
          ts.isPropertyAssignment(childrenProperty) &&
          propertyName(childrenProperty.name) === "children"
        ) {
          candidates.set(node, { props, childrenProperty });
        }
        if (props && ts.isObjectLiteralExpression(props)) {
          for (const prop of props.properties) {
            if (ts.isPropertyAssignment(prop)) {
              collectValueLiterals(prop.initializer);
              if (ts.isComputedPropertyName(prop.name)) continue;
              const name = propertyName(prop.name);
              if (!name || name === "__proto__" || prop === childrenProperty) continue;
              const usage = propUsages.get(name) ?? [];
              usage.push(prop.name);
              propUsages.set(name, usage);
            } else if (ts.isSpreadAssignment(prop)) {
              collectValueLiterals(prop.expression);
            }
          }
        }
        if (node.arguments[2]) collectValueLiterals(node.arguments[2]);
    }
    ts.forEachChild(node, scan);
  };
  scan(sourceFile);

  const tagNodeAliases = new Map();
  const propNodeAliases = new Map();
  const valueNodeAliases = new Map();
  const declarations = [];
  for (const usage of (aliasTags ? [...tagUsages.values()] : []).sort((left, right) => right.nodes.length - left.nodes.length)) {
    const alias = freshName(identifiers, "h");
    const declarationCost = alias.length + usage.literal.length + 2;
    const savings = usage.nodes.length * (usage.literal.length - alias.length) - declarationCost;
    if (savings <= 0) continue;
    declarations.push(`${alias}=${usage.literal}`);
    for (const node of usage.nodes) tagNodeAliases.set(node, alias);
  }
  for (const [name, nodes] of (aliasProps ? [...propUsages] : []).sort((left, right) => right[1].length - left[1].length)) {
    const alias = freshName(identifiers, "k");
    const literal = JSON.stringify(name);
    const declarationCost = alias.length + literal.length + 2;
    const savings = nodes.reduce(
      (total, node) => total + code.slice(node.getStart(sourceFile), node.end).length - alias.length - 2,
      -declarationCost
    );
    if (savings <= 0) continue;
    declarations.push(`${alias}=${literal}`);
    for (const node of nodes) propNodeAliases.set(node, alias);
  }
  for (const usage of (aliasValues ? [...valueUsages.values()] : [])
    .filter((item) => item.nodes.size > 1)
    .sort((left, right) => right.nodes.size - left.nodes.size)) {
    const nodes = [...usage.nodes];
    const alias = freshName(identifiers, "v");
    const declarationCost = alias.length + usage.literal.length + 2;
    const savings = nodes.reduce(
      (total, node) => total + code.slice(node.getStart(sourceFile), node.end).length - alias.length,
      -declarationCost
    );
    if (savings <= 0) continue;
    declarations.push(`${alias}=${usage.literal}`);
    for (const node of nodes) valueNodeAliases.set(node, alias);
  }

  let propsHelper = null;
  if (!compactChildren) {
    candidates.clear();
  } else if (!topLevelObjectBinding && candidates.size) {
    const definePropertyAlias = freshName(identifiers, "d");
    propsHelper = freshName(identifiers, "p");
    declarations.push(`${definePropertyAlias}=Object.defineProperty`);
    declarations.push(
      `${propsHelper}=(e,t)=>(${definePropertyAlias}(e,"children",` +
      `{value:t,writable:!0,enumerable:!0,configurable:!0}),e)`
    );
  } else if (topLevelObjectBinding) {
    candidates.clear();
  }

  if (!candidates.size && !tagNodeAliases.size && !propNodeAliases.size && !valueNodeAliases.size) {
    const skipped = topLevelObjectBinding ? "top-level-Object-binding" : undefined;
    return { code, changed: false, summaries: [{ transformed: 0, runtimeCalls, skipped }] };
  }
  const helper = `;var ${declarations.join(",")};`;

  const applyEdits = (start, end, edits) => {
    let output = code.slice(start, end);
    for (const edit of edits.sort((left, right) => right.start - left.start)) {
      output = `${output.slice(0, edit.start - start)}${edit.text}${output.slice(edit.end - start)}`;
    }
    return output;
  };

  const renderCandidate = (node, candidate) => {
    const renderedArguments = node.arguments.map(renderNode);
    const prefixProperties = candidate.props.properties.slice(0, -1).map(renderNode).join(",");
    const child = renderNode(candidate.childrenProperty.initializer);
    renderedArguments[1] = `${propsHelper}({${prefixProperties}},${child})`;
    return `${node.expression.text}(${renderedArguments.join(",")})`;
  };

  const renderNode = (node) => {
    const valueAlias = valueNodeAliases.get(node);
    if (valueAlias) return valueAlias;
    const propAlias = propNodeAliases.get(node);
    if (propAlias) return `[${propAlias}]`;
    const tagAlias = tagNodeAliases.get(node);
    if (tagAlias) return tagAlias;
    const candidate = candidates.get(node);
    if (candidate && ts.isCallExpression(node)) return renderCandidate(node, candidate);
    const nested = [];
    const visit = (child) => {
      const childValueAlias = valueNodeAliases.get(child);
      if (childValueAlias) {
        nested.push({ start: child.getStart(sourceFile), end: child.end, text: childValueAlias });
        return;
      }
      const childPropAlias = propNodeAliases.get(child);
      if (childPropAlias) {
        nested.push({ start: child.getStart(sourceFile), end: child.end, text: `[${childPropAlias}]` });
        return;
      }
      const childTagAlias = tagNodeAliases.get(child);
      if (childTagAlias) {
        nested.push({ start: child.getStart(sourceFile), end: child.end, text: childTagAlias });
        return;
      }
      const childCandidate = candidates.get(child);
      if (childCandidate && ts.isCallExpression(child)) {
        nested.push({
          start: child.getStart(sourceFile),
          end: child.end,
          text: renderCandidate(child, childCandidate)
        });
        return;
      }
      ts.forEachChild(child, visit);
    };
    ts.forEachChild(node, visit);
    return applyEdits(node.getStart(sourceFile), node.end, nested);
  };

  const edits = [];
  const selectOutermost = (node) => {
    const candidate = candidates.get(node);
    if (candidate && ts.isCallExpression(node)) {
      edits.push({
        start: node.getStart(sourceFile),
        end: node.end,
        text: renderCandidate(node, candidate)
      });
      return;
    }
    const valueAlias = valueNodeAliases.get(node);
    if (valueAlias) {
      edits.push({ start: node.getStart(sourceFile), end: node.end, text: valueAlias });
      return;
    }
    const tagAlias = tagNodeAliases.get(node);
    if (tagAlias) {
      edits.push({ start: node.getStart(sourceFile), end: node.end, text: tagAlias });
      return;
    }
    const propAlias = propNodeAliases.get(node);
    if (propAlias) {
      edits.push({ start: node.getStart(sourceFile), end: node.end, text: `[${propAlias}]` });
      return;
    }
    ts.forEachChild(node, selectOutermost);
  };
  selectOutermost(sourceFile);
  const insertAt = helperInsertionPoint(sourceFile);
  edits.push({ start: insertAt, end: insertAt, text: helper });

  let output = code;
  for (const edit of edits.sort((left, right) => right.start - left.start || right.end - left.end)) {
    output = `${output.slice(0, edit.start)}${edit.text}${output.slice(edit.end)}`;
  }
  const summaries = [{
    transformed: candidates.size,
    aliasedTags: tagNodeAliases.size,
    aliasedProps: propNodeAliases.size,
    aliasedValues: valueNodeAliases.size,
    runtimeCalls,
    skipped: topLevelObjectBinding ? "children:top-level-Object-binding" : undefined
  }];
  if (output.length >= code.length) return { code, changed: false, summaries };
  return { code: output, changed: true, summaries };
}

export function compactJsxRuntimeChunkForBrotli(code, id = "chunk.js", options = {}) {
  const maxBrotliCostPerRawByte = options.maxBrotliCostPerRawByte ?? 0.007;
  const variants = [
    compactJsxRuntimeChunk(code, id, {
      compactChildren: true,
      aliasTags: false,
      aliasProps: false,
      aliasValues: false
    }),
    compactJsxRuntimeChunk(code, id, {
      compactChildren: false,
      aliasTags: true,
      aliasProps: false,
      aliasValues: false
    })
  ].filter((result) => result.changed);
  if (!variants.length) return { code, changed: false, summaries: [] };

  const brotliBytes = (source) => brotliCompressSync(source, {
    params: { [zlibConstants.BROTLI_PARAM_QUALITY]: 11 }
  }).length;
  const baseRawBytes = Buffer.byteLength(code);
  const baseBrotliBytes = brotliBytes(code);
  const candidates = variants
    .map((result) => {
      const rawBytes = Buffer.byteLength(result.code);
      const savedRawBytes = baseRawBytes - rawBytes;
      const brotliDeltaBytes = brotliBytes(result.code) - baseBrotliBytes;
      return {
        ...result,
        rawBytes,
        savedRawBytes,
        brotliDeltaBytes,
        brotliCostPerRawByte: savedRawBytes > 0 ? brotliDeltaBytes / savedRawBytes : Infinity
      };
    })
    .filter((result) =>
      result.savedRawBytes > 0 &&
      result.brotliCostPerRawByte <= maxBrotliCostPerRawByte
    )
    .sort((left, right) =>
      left.rawBytes - right.rawBytes ||
      left.brotliDeltaBytes - right.brotliDeltaBytes
    );
  const selected = candidates[0];
  if (!selected) return { code, changed: false, summaries: [] };
  return {
    code: selected.code,
    changed: true,
    summaries: selected.summaries.map((summary) => ({
      ...summary,
      savedRawBytes: selected.savedRawBytes,
      brotliDeltaBytes: selected.brotliDeltaBytes
    }))
  };
}

export function compactJsxRuntime(options = {}) {
  return {
    name: "auris-compact-jsx-runtime",
    apply: "build",
    enforce: "post",
    renderChunk(code, chunk) {
      const result = options.compressionAware
        ? compactJsxRuntimeChunkForBrotli(code, chunk.fileName, options)
        : compactJsxRuntimeChunk(code, chunk.fileName, options);
      return result.changed ? { code: result.code, map: null } : null;
    }
  };
}
