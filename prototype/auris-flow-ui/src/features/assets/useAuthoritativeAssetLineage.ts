import { useEffect, useReducer, useRef } from "react";

import { apiRequest } from "../../api/client";
import {
  assetLineageIsEmpty,
  assetLineageStateReducer,
  initialAssetLineageState,
  parseAuthoritativeAssetLineage,
  readStateForSelectedAsset
} from "./authoritativeAssetLineage";

export function useAuthoritativeAssetLineage(assetKey: string, scopeKey: string) {
  const [state, dispatch] = useReducer(assetLineageStateReducer, initialAssetLineageState);
  const requestGeneration = useRef(0);

  useEffect(() => {
    const generation = requestGeneration.current + 1;
    requestGeneration.current = generation;
    const requestKey = `${scopeKey}:${assetKey}#${generation}`;
    const controller = new AbortController();
    dispatch({ type: "begin", assetKey, scopeKey, requestKey });

    void apiRequest<unknown>(
      `/v1/data-assets/${encodeURIComponent(assetKey)}/lineage`,
      { signal: controller.signal }
    )
      .then((response) => {
        if (controller.signal.aborted || requestGeneration.current !== generation) return;
        const parsed = parseAuthoritativeAssetLineage(response.data, assetKey);
        if (!parsed.ok) {
          dispatch({ type: "error", requestKey, reason: parsed.reason });
          return;
        }
        dispatch({
          type: assetLineageIsEmpty(parsed.value) ? "empty" : "ready",
          requestKey,
          value: parsed.value
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        dispatch({
          type: "error",
          requestKey,
          reason: error instanceof Error ? error.message : "资产 lineage 读取失败"
        });
      });

    return () => controller.abort();
  }, [assetKey, scopeKey]);

  return readStateForSelectedAsset(state, assetKey, scopeKey);
}
