import { createHash } from "node:crypto";
import { copyFileSync, cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

function parseArgs(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || !value || value.startsWith("--")) throw new Error(`无效参数：${key ?? "<missing>"}`);
    values[key.slice(2)] = value;
    index += 1;
  }
  for (const required of ["base", "candidate", "report", "output", "accept", "reason"]) {
    if (!values[required]) throw new Error(`缺少 --${required}`);
  }
  return values;
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

const args = parseArgs(process.argv.slice(2));
const baseDirectory = resolve(args.base);
const candidateDirectory = resolve(args.candidate);
const reportPath = resolve(args.report);
const outputDirectory = resolve(args.output);
const accepted = args.accept.split(",").map((file) => file.trim()).filter(Boolean).sort();
const reportBuffer = readFileSync(reportPath);
const report = JSON.parse(reportBuffer.toString("utf8"));
const reportedFailures = report.failed.map((item) => item.file).sort();

if (resolve(report.expectedDirectory) !== baseDirectory || resolve(report.actualDirectory) !== candidateDirectory) {
  throw new Error("差异报告与输入目录不匹配");
}
if (JSON.stringify(reportedFailures) !== JSON.stringify(accepted)) {
  throw new Error(`显式接纳项必须精确覆盖全部超阈值差异：failed=${reportedFailures.join(",")} accepted=${accepted.join(",")}`);
}
if (report.missing.length || report.extra.length || report.geometry.status !== "ok") {
  throw new Error("存在截图缺失、额外截图或关键几何失败，禁止派生基线");
}
if (existsSync(outputDirectory)) throw new Error(`输出目录已存在：${outputDirectory}`);

mkdirSync(dirname(outputDirectory), { recursive: true });
cpSync(baseDirectory, outputDirectory, { recursive: true, errorOnExist: true, force: false });
const acceptedFiles = accepted.map((file) => {
  const source = join(candidateDirectory, "screenshots", file);
  const target = join(outputDirectory, "screenshots", file);
  const before = readFileSync(target);
  const after = readFileSync(source);
  copyFileSync(source, target);
  const comparison = report.failed.find((item) => item.file === file);
  return {
    file,
    baseSha256: sha256(before),
    candidateSha256: sha256(after),
    diffPixels: comparison.diffPixels,
    ratio: comparison.ratio,
    diffBoundingBox: comparison.diffBoundingBox
  };
});
const provenance = {
  schemaVersion: 1,
  kind: "derived-visual-baseline",
  baseDirectory,
  candidateDirectory,
  report: basename(reportPath),
  reportSha256: sha256(reportBuffer),
  baseFingerprint: report.expectedFingerprint,
  candidateFingerprint: report.actualFingerprint,
  reason: args.reason,
  geometryPolicy: "继承 Phase 0 geometry；候选相对 Phase 0 最大偏差不超过 0.5px",
  acceptedFiles
};
copyFileSync(reportPath, join(outputDirectory, basename(reportPath)));
writeFileSync(join(outputDirectory, "provenance.json"), `${JSON.stringify(provenance, null, 2)}\n`, "utf8");
process.stdout.write(`${JSON.stringify({ status: "ok", outputDirectory, acceptedFiles }, null, 2)}\n`);
