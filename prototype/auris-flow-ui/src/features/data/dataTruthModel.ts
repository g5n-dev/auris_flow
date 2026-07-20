export type TruthDataCatalogProjection = {
  name: string;
  assetKey: string;
  quality: null;
};

const PROCESSING_PRODUCT_LABELS: Record<string, string> = {
  vad: "VAD",
  asr_transcript: "ASR transcript",
  raw_wav: "raw wav",
  voice_segments: "voice_segments"
};

export function normalizeSessionConfidence(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    return null;
  }
  return value;
}

export function formatSessionConfidence(value: number | null): string {
  return value === null ? "未提供" : `${Math.round(value * 100)}%`;
}

export function projectTruthAssetCatalog(item: {
  assetKey: string;
  confidence: number | null;
}): TruthDataCatalogProjection {
  const assetKey = item.assetKey.trim();
  return {
    name: assetKey || "未绑定数据资产",
    assetKey,
    // audio-session confidence describes the session projection. It is not an
    // asset check or quality score and must never be promoted to one.
    quality: null
  };
}

export function authoritativeProcessingProducts(value: unknown): string[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const rawProducts = (value as Record<string, unknown>).processing_products;
  if (!Array.isArray(rawProducts)) return [];
  const products: string[] = [];
  for (const rawProduct of rawProducts) {
    if (typeof rawProduct !== "string") continue;
    const label = PROCESSING_PRODUCT_LABELS[rawProduct.trim()];
    if (label && !products.includes(label)) products.push(label);
  }
  return products;
}
