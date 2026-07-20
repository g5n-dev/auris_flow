import type { AssetBackfillDraft, HotwordBackfillRecovery } from "./types";

export const HOTWORD_RECOVERY_LOADING_REASON = "正在读取当前租户项目的已发布词包与权威 ASR 物化。";

export function loadingHotwordRecovery(scopeKey: string): HotwordBackfillRecovery {
  return {
    scopeKey,
    status: "loading",
    reason: HOTWORD_RECOVERY_LOADING_REASON
  };
}

export function resolveHotwordRecoveryForScope(
  recovery: HotwordBackfillRecovery,
  currentScopeKey: string
): HotwordBackfillRecovery {
  return recovery.scopeKey === currentScopeKey
    ? recovery
    : loadingHotwordRecovery(currentScopeKey);
}

export function backfillDraftForScope(
  draft: AssetBackfillDraft | null,
  currentScopeKey: string
): AssetBackfillDraft | null {
  return draft?.scopeKey === currentScopeKey ? draft : null;
}
