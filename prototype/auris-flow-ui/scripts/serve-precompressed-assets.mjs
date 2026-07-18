import { readFileSync } from "node:fs";
import { extname, join, resolve } from "node:path";

import {
  acceptsBrotli,
  appendVaryHeader,
  auditPrecompressedAssets
} from "./precompressed-assets.mjs";

const contentTypes = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8"
};

function requestPath(url) {
  try {
    return decodeURIComponent(new URL(url ?? "/", "http://auris-preview.local").pathname);
  } catch {
    return null;
  }
}

function encodingHeader(value) {
  return Array.isArray(value) ? value.join(",") : value;
}

export function createPrecompressedMiddleware(distDir) {
  const root = resolve(distDir);
  const audit = auditPrecompressedAssets(root);
  if (!audit.ok || !audit.manifest) {
    throw new Error(`生产 Brotli 产物审计失败：${JSON.stringify(audit.errors)}`);
  }
  const entries = new Map(Object.entries(audit.manifest.entries));

  return function servePrecompressedAsset(request, response, next) {
    if (request.method !== "GET" && request.method !== "HEAD") return next();
    const pathname = requestPath(request.url);
    if (!pathname) return next();
    if (pathname.endsWith(".br")) {
      response.statusCode = 404;
      response.end();
      return;
    }
    const resourcePath = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
    const entry = entries.get(resourcePath);
    if (!entry) return next();

    const useBrotli = acceptsBrotli(encodingHeader(request.headers["accept-encoding"]));
    const sourcePath = join(root, resourcePath);
    const body = readFileSync(useBrotli ? `${sourcePath}.br` : sourcePath);
    response.statusCode = 200;
    response.setHeader("Content-Type", contentTypes[extname(resourcePath)] ?? "application/octet-stream");
    response.setHeader("Content-Length", String(body.length));
    response.setHeader("Vary", appendVaryHeader(response.getHeader("Vary"), "Accept-Encoding"));
    if (useBrotli) response.setHeader("Content-Encoding", "br");
    if (request.method === "HEAD") response.end();
    else response.end(body);
  };
}

export function precompressedPreview() {
  return {
    name: "auris-precompressed-preview",
    apply: "serve",
    configurePreviewServer(server) {
      const distDir = resolve(server.config.root, server.config.build.outDir);
      server.middlewares.use(createPrecompressedMiddleware(distDir));
    }
  };
}
