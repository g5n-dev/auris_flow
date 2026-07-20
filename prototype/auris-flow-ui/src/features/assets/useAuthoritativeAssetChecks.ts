import { useEffect, useReducer, useRef } from "react";

import { apiRequest } from "../../api/client";
import {
  assetChecksStateReducer,
  initialAssetChecksState,
  parseAuthoritativeAssetChecks,
  readChecksStateForSelectedAsset
} from "./authoritativeAssetChecks";

export function useAuthoritativeAssetChecks(
  assetKey: string,
  scopeKey: string,
  enabled = true
) {
  const [state, dispatch] = useReducer(assetChecksStateReducer, initialAssetChecksState);
  const requestGeneration = useRef(0);

  useEffect(() => {
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    if (!enabled) return;

    const requestKey = `${scopeKey}:${assetKey}#${generation}`;
    const controller = new AbortController();
    dispatch({ type: "begin", assetKey, scopeKey, requestKey });

    void apiRequest<unknown>(
      `/v1/data-assets/${encodeURIComponent(assetKey)}`,
      { signal: controller.signal }
    )
      .then((response) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        const parsed = parseAuthoritativeAssetChecks(response.data, assetKey);
        if (!parsed.ok) {
          dispatch({ type: "error", requestKey, reason: parsed.reason });
          return;
        }
        dispatch({
          type: parsed.value.checks.length ? "ready" : "empty",
          requestKey,
          value: parsed.value
        });
      })
      .catch((error) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        dispatch({
          type: "error",
          requestKey,
          reason: error instanceof Error ? error.message : "资产 checks 读取失败"
        });
      });

    return () => controller.abort();
  }, [assetKey, enabled, scopeKey]);

  if (!enabled) {
    return {
      ...initialAssetChecksState,
      assetKey,
      scopeKey
    };
  }
  return readChecksStateForSelectedAsset(state, assetKey, scopeKey);
}
