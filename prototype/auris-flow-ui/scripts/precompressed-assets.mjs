import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync
} from "node:fs";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join, relative, resolve, sep } from "node:path";
import {
  brotliCompressSync,
  brotliDecompressSync,
  constants as zlibConstants
} from "node:zlib";

export const BROTLI_MANIFEST_PATH = ".vite/brotli-manifest.json";
export const BROTLI_QUALITY = 11;

const compressiblePattern = /\.(?:html|js|css|json)$/;

function normalizePath(path) {
  return path.split(sep).join("/");
}

function walkFiles(directory) {
  if (!existsSync(directory)) return [];
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? walkFiles(path) : [path];
  });
}

export function listProductionResources(distDir) {
  const root = resolve(distDir);
  const resources = [];
  const indexPath = join(root, "index.html");
  if (existsSync(indexPath)) resources.push(indexPath);
  resources.push(...walkFiles(join(root, "assets")));
  return resources
    .filter((path) => !path.endsWith(".br"))
    .sort((left, right) => normalizePath(relative(root, left)).localeCompare(normalizePath(relative(root, right))));
}

export function listCompressibleResources(distDir) {
  return listProductionResources(distDir).filter((path) => compressiblePattern.test(path));
}

export function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export function compressBrotliQ11(source) {
  return brotliCompressSync(source, {
    params: {
      [zlibConstants.BROTLI_PARAM_MODE]: zlibConstants.BROTLI_MODE_TEXT,
      [zlibConstants.BROTLI_PARAM_QUALITY]: BROTLI_QUALITY,
      [zlibConstants.BROTLI_PARAM_SIZE_HINT]: source.length
    }
  });
}

function atomicWrite(path, bytes) {
  mkdirSync(dirname(path), { recursive: true });
  const temporaryPath = `${path}.tmp-${process.pid}`;
  writeFileSync(temporaryPath, bytes);
  renameSync(temporaryPath, path);
}

function manifestBytes(manifest) {
  return Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`);
}

export function generatePrecompressedAssets(distDir) {
  const root = resolve(distDir);
  if (!existsSync(join(root, "index.html"))) {
    throw new Error(`缺少生产入口 ${join(root, "index.html")}`);
  }

  for (const path of walkFiles(root)) {
    if (path.endsWith(".br")) rmSync(path, { force: true });
  }

  const entries = {};
  for (const sourcePath of listCompressibleResources(root)) {
    const source = readFileSync(sourcePath);
    const compressed = compressBrotliQ11(source);
    const resourcePath = normalizePath(relative(root, sourcePath));
    atomicWrite(`${sourcePath}.br`, compressed);
    entries[resourcePath] = {
      sourceSha256: sha256(source),
      brotliSha256: sha256(compressed),
      rawBytes: source.length,
      brotliBytes: compressed.length
    };
  }

  const manifest = {
    schemaVersion: 1,
    algorithm: "br",
    quality: BROTLI_QUALITY,
    params: {
      mode: "text",
      sizeHint: "rawBytes"
    },
    entries
  };
  atomicWrite(join(root, BROTLI_MANIFEST_PATH), manifestBytes(manifest));
  const audit = auditPrecompressedAssets(root);
  if (!audit.ok) {
    throw new Error(`Brotli 预压缩自检失败：${JSON.stringify(audit.errors)}`);
  }
  return { manifest, audit };
}

function addError(errors, code, detail) {
  errors.push({ code, detail });
}

function isManifestEntry(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    /^[a-f0-9]{64}$/.test(value.sourceSha256) &&
    /^[a-f0-9]{64}$/.test(value.brotliSha256) &&
    Number.isInteger(value.rawBytes) &&
    value.rawBytes >= 0 &&
    Number.isInteger(value.brotliBytes) &&
    value.brotliBytes >= 0
  );
}

export function auditPrecompressedAssets(distDir) {
  const root = resolve(distDir);
  const errors = [];
  const manifestPath = join(root, BROTLI_MANIFEST_PATH);
  if (!existsSync(manifestPath)) {
    addError(errors, "PRECOMPRESS_MANIFEST_MISSING", { path: BROTLI_MANIFEST_PATH });
    return { ok: false, errors, manifest: null, resources: [] };
  }

  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (error) {
    addError(errors, "PRECOMPRESS_SCHEMA_INVALID", { message: String(error) });
    return { ok: false, errors, manifest: null, resources: [] };
  }

  if (
    manifest?.schemaVersion !== 1 ||
    manifest?.algorithm !== "br" ||
    manifest?.quality !== BROTLI_QUALITY ||
    manifest?.params?.mode !== "text" ||
    manifest?.params?.sizeHint !== "rawBytes" ||
    !manifest?.entries ||
    typeof manifest.entries !== "object" ||
    Array.isArray(manifest.entries)
  ) {
    addError(errors, "PRECOMPRESS_SCHEMA_INVALID", { manifest });
  }

  const sources = listCompressibleResources(root);
  const sourceByResource = new Map(
    sources.map((path) => [normalizePath(relative(root, path)), path])
  );
  const manifestEntries = Object.entries(manifest?.entries ?? {});
  const manifestPaths = new Set(manifestEntries.map(([path]) => path));
  for (const path of sourceByResource.keys()) {
    if (!manifestPaths.has(path)) addError(errors, "PRECOMPRESS_ENTRY_MISSING", { path });
  }
  for (const [path, entry] of manifestEntries) {
    if (!sourceByResource.has(path)) addError(errors, "PRECOMPRESS_ENTRY_ORPHAN", { path });
    if (!isManifestEntry(entry)) addError(errors, "PRECOMPRESS_SCHEMA_INVALID", { path, entry });
  }

  const sidecarPaths = walkFiles(root)
    .filter((path) => path.endsWith(".br"))
    .map((path) => normalizePath(relative(root, path)));
  const expectedSidecars = new Set([...sourceByResource.keys()].map((path) => `${path}.br`));
  const actualSidecars = new Set(sidecarPaths);
  for (const path of expectedSidecars) {
    if (!actualSidecars.has(path)) addError(errors, "BROTLI_SIDECAR_MISSING", { path });
  }
  for (const path of actualSidecars) {
    if (!expectedSidecars.has(path)) addError(errors, "BROTLI_SIDECAR_ORPHAN", { path });
  }

  const resources = [];
  for (const [resourcePath, sourcePath] of sourceByResource) {
    const entry = manifest?.entries?.[resourcePath];
    const source = readFileSync(sourcePath);
    const sidecarPath = `${sourcePath}.br`;
    const resource = {
      path: resourcePath,
      sourcePath,
      sidecarPath,
      rawBytes: source.length,
      brotliBytes: null
    };
    resources.push(resource);
    if (entry && isManifestEntry(entry)) {
      if (entry.rawBytes !== source.length) {
        addError(errors, "SOURCE_SIZE_MISMATCH", { path: resourcePath, expected: entry.rawBytes, actual: source.length });
      }
      const sourceHash = sha256(source);
      if (entry.sourceSha256 !== sourceHash) {
        addError(errors, "SOURCE_HASH_MISMATCH", { path: resourcePath, expected: entry.sourceSha256, actual: sourceHash });
      }
    }
    if (!existsSync(sidecarPath)) continue;

    const compressed = readFileSync(sidecarPath);
    resource.brotliBytes = compressed.length;
    if (entry && isManifestEntry(entry)) {
      if (entry.brotliBytes !== compressed.length) {
        addError(errors, "BROTLI_SIZE_MISMATCH", { path: resourcePath, expected: entry.brotliBytes, actual: compressed.length });
      }
      const compressedHash = sha256(compressed);
      if (entry.brotliSha256 !== compressedHash) {
        addError(errors, "BROTLI_HASH_MISMATCH", { path: resourcePath, expected: entry.brotliSha256, actual: compressedHash });
      }
    }

    let decoded;
    try {
      decoded = brotliDecompressSync(compressed);
    } catch (error) {
      addError(errors, "BROTLI_DECOMPRESS_FAILED", { path: resourcePath, message: String(error) });
      continue;
    }
    if (!decoded.equals(source)) addError(errors, "BROTLI_DECODED_MISMATCH", { path: resourcePath });
    const canonical = compressBrotliQ11(source);
    if (!canonical.equals(compressed)) addError(errors, "BROTLI_NOT_CANONICAL_Q11", { path: resourcePath });
  }

  resources.sort((left, right) => left.path.localeCompare(right.path));
  return { ok: errors.length === 0, errors, manifest, resources };
}

function parseEncodingPreferences(header) {
  if (typeof header !== "string") return new Map();
  const preferences = new Map();
  for (const part of header.split(",")) {
    const [rawName, ...parameters] = part.trim().split(";");
    const name = rawName.trim().toLowerCase();
    if (!name) continue;
    let quality = 1;
    for (const parameter of parameters) {
      const [key, rawValue] = parameter.trim().split("=");
      if (key?.toLowerCase() !== "q") continue;
      const value = Number(rawValue);
      quality = Number.isFinite(value) && value >= 0 && value <= 1 ? value : 0;
    }
    preferences.set(name, quality);
  }
  return preferences;
}

export function acceptsBrotli(header) {
  const preferences = parseEncodingPreferences(header);
  if (preferences.has("br")) return preferences.get("br") > 0;
  return (preferences.get("*") ?? 0) > 0;
}

export function appendVaryHeader(current, value) {
  const values = String(current ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!values.some((item) => item.toLowerCase() === value.toLowerCase())) values.push(value);
  return values.join(", ");
}

const modulePath = fileURLToPath(import.meta.url);
if (process.argv[1] && resolve(process.argv[1]) === modulePath) {
  const root = resolve(dirname(modulePath), "..");
  const distDir = process.env.AURIS_DIST_DIR ? resolve(root, process.env.AURIS_DIST_DIR) : join(root, "dist");
  const result = generatePrecompressedAssets(distDir);
  const totals = Object.values(result.manifest.entries).reduce(
    (sum, entry) => ({
      rawBytes: sum.rawBytes + entry.rawBytes,
      brotliBytes: sum.brotliBytes + entry.brotliBytes
    }),
    { rawBytes: 0, brotliBytes: 0 }
  );
  process.stdout.write(`${JSON.stringify({
    status: "ok",
    quality: BROTLI_QUALITY,
    resources: Object.keys(result.manifest.entries).length,
    totals
  }, null, 2)}\n`);
}
