import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const UI_ROOT = path.resolve(SCRIPT_DIR, "..");

const SCOPED_SOURCE_ROOTS = [
  "src/modules/knowledge",
  "src/features/settings",
  "src/features/evaluation",
  "src/features/canvas",
  "src/features/insights",
  "src/features/listening",
  "src/features/data"
];

const SOURCE_EXTENSIONS = new Set([".ts", ".tsx"]);
const SUCCESS_CLAIM =
  /status\s*:\s*["']success["']|set[A-Za-z0-9_]*\(\s*["']success["']|已(?:成功|完成|保存|发布|创建|生成|写入|更新|通过|回写|导出|加入|记录)/u;

async function collectSourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "fixtures" || entry.name === "preserved") continue;
      files.push(...await collectSourceFiles(target));
      continue;
    }
    if (SOURCE_EXTENSIONS.has(path.extname(entry.name))) files.push(target);
  }
  return files;
}

function callbackSource(call, sourceFile) {
  const callback = call.arguments[0];
  if (!callback || (!ts.isArrowFunction(callback) && !ts.isFunctionExpression(callback))) return "";
  return callback.getText(sourceFile);
}

export function findTimedSuccessClaims(source, filename = "source.ts") {
  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.ESNext,
    true,
    filename.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  );
  const findings = [];

  function visit(node) {
    if (
      ts.isCallExpression(node)
      && (
        (ts.isIdentifier(node.expression) && node.expression.text === "setTimeout")
        || (
          ts.isPropertyAccessExpression(node.expression)
          && node.expression.name.text === "setTimeout"
        )
      )
    ) {
      const callback = callbackSource(node, sourceFile);
      if (callback && SUCCESS_CLAIM.test(callback)) {
        const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
        findings.push({
          code: "TIMED_SUCCESS_CLAIM",
          file: filename,
          line: line + 1
        });
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return findings;
}

export async function scanProductionActionTruth(uiRoot = UI_ROOT) {
  const files = (
    await Promise.all(
      SCOPED_SOURCE_ROOTS.map((relativeRoot) =>
        collectSourceFiles(path.join(uiRoot, relativeRoot))
      )
    )
  ).flat();
  const findings = [];

  for (const file of files) {
    const source = await readFile(file, "utf8");
    const relativeFile = path.relative(uiRoot, file);
    findings.push(...findTimedSuccessClaims(source, relativeFile));
  }

  const [
    moduleContentSource,
    knowledgeSource,
    canvasExecutionSource,
    canvasToolbarSource,
    evidenceModeSource,
    authoritativeEvidenceEditorSource,
    simpleModeSource
  ] =
    await Promise.all([
      readFile(path.join(uiRoot, "src/workspace/moduleContentSource.ts"), "utf8"),
      readFile(path.join(uiRoot, "src/modules/knowledge/KnowledgeModule.tsx"), "utf8"),
      readFile(path.join(uiRoot, "src/features/canvas/controller/buildCanvasExecutionActions.ts"), "utf8"),
      readFile(path.join(uiRoot, "src/features/canvas/controller/buildCanvasToolbarModel.ts"), "utf8"),
      readFile(path.join(uiRoot, "src/features/listening/components/evidence/EvidenceMode.tsx"), "utf8"),
      readFile(path.join(uiRoot, "src/features/listening/components/evidence/AuthoritativeEvidenceEditor.tsx"), "utf8"),
      readFile(path.join(uiRoot, "src/features/listening/components/simple/SimpleMode.tsx"), "utf8")
    ]);

  for (const moduleKey of ["settings", "evaluation", "insights"]) {
    if (new RegExp(`["']${moduleKey}["']`).test(moduleContentSource.match(/AUTHORITATIVE_CONTENT_READERS[\s\S]*?\]\);/)?.[0] ?? "")) {
      findings.push({
        code: "UNAUTHORITATIVE_MODULE_MOUNTED_IN_PRODUCTION",
        file: "src/workspace/moduleContentSource.ts",
        moduleKey
      });
    }
  }

  if (
    !knowledgeSource.includes("knowledgeWriteDisabledReason")
    || !knowledgeSource.includes("\"aria-describedby\": knowledgeWriteDisabledReason")
    || !knowledgeSource.includes("id=\"knowledge-write-disabled-reason\"")
  ) {
    findings.push({
      code: "KNOWLEDGE_UNSUPPORTED_ACTIONS_NOT_DISABLED",
      file: "src/modules/knowledge/KnowledgeModule.tsx"
    });
  }

  if (
    !canvasExecutionSource.includes("if (!demoMode)")
    || !canvasToolbarSource.includes("canvasAssistantDisabledReason")
  ) {
    findings.push({
      code: "CANVAS_LOCAL_ASSISTANT_NOT_FAIL_CLOSED",
      file: "src/features/canvas/controller/buildCanvasExecutionActions.ts"
    });
  }

  if (
    !evidenceModeSource.includes("demoEditorsEnabled = LABEL_DEMO_MODE")
    || !evidenceModeSource.includes("authoritativeEditorsEnabled")
    || !evidenceModeSource.includes("AuthoritativeEvidenceEditor")
    || !evidenceModeSource.includes("listening-authoritative-editor-blocked")
    || authoritativeEvidenceEditorSource.includes("LABEL_DEMO_MODE")
    || authoritativeEvidenceEditorSource.includes("shared/fixtures")
    || !authoritativeEvidenceEditorSource.includes("data-testid=\"listening-authoritative-editor\"")
  ) {
    findings.push({
      code: "LISTENING_FIXTURE_EDITORS_NOT_FAIL_CLOSED",
      file: "src/features/listening/components/evidence/EvidenceMode.tsx"
    });
  }

  if (
    !simpleModeSource.includes("AuthoritativeSessionPlayback")
    || !simpleModeSource.includes("LABEL_DEMO_MODE")
    || !simpleModeSource.includes("AI 推荐与本地保存已禁用")
    || /setTimeout\s*\(/.test(simpleModeSource)
  ) {
    findings.push({
      code: "LISTENING_SIMPLE_MODE_LOCAL_SUCCESS_NOT_ISOLATED",
      file: "src/features/listening/components/simple/SimpleMode.tsx"
    });
  }

  return findings;
}
