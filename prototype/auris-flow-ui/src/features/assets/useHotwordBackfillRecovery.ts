import { useEffect, type Dispatch, type SetStateAction } from "react";

import {
  apiRequest,
  getBackendRun,
  getHotwordPackVersion,
  getTaskVersion,
  listHotwordPacks,
  listHotwordPackVersions
} from "../../api/client";
import {
  HOTWORD_PACK_DOMAIN,
  hotwordVersionView
} from "../../shared/runtime/hotwordVersionViews";
import type {
  AssetMaterializationProjection,
  HotwordBackfillBinding,
  HotwordBackfillRecovery
} from "./types";

export function useHotwordBackfillRecovery(
  setRecovery: Dispatch<SetStateAction<HotwordBackfillRecovery>>
) {
  useEffect(() => {
    let active = true;
    const sourceAssetKey = "auris/model/asr_transcripts";
    void Promise.all([
      listHotwordPacks(),
      apiRequest<{ items: AssetMaterializationProjection[] }>(
        `/v1/data-assets/${encodeURIComponent(sourceAssetKey)}/materializations?limit=100`
      )
    ])
      .then(async ([packsResponse, materializationsResponse]) => {
        const pack = packsResponse.data.items.find((item) => item.domain === HOTWORD_PACK_DOMAIN);
        const packId = typeof pack?.pack_id === "string" ? pack.pack_id : typeof pack?.id === "string" ? pack.id : null;
        const currentVersionId = typeof pack?.current_version_id === "string" ? pack.current_version_id : null;
        if (!packId || !currentVersionId) throw new Error("热词包当前版本缺失");
        const versionsResponse = await listHotwordPackVersions(packId, { limit: 100 });
        const currentVersionSummary = versionsResponse.data.items
          .map((raw) => hotwordVersionView(raw))
          .find((version) => version?.id === currentVersionId);
        if (!currentVersionSummary || currentVersionSummary.status !== "published") {
          throw new Error(`${currentVersionId} 未发布`);
        }
        const detailResponse = await getHotwordPackVersion(currentVersionId);
        const version = hotwordVersionView(detailResponse.data);
        if (!version) throw new Error(`${currentVersionId} 详情无效`);
        let taskVersionId = version.taskVersionId;
        if (!taskVersionId && version.publishRunId) {
          const publishRun = await getBackendRun(version.publishRunId);
          const raw = publishRun.data.raw;
          const nestedPublish = raw.hotword_publish && typeof raw.hotword_publish === "object" && !Array.isArray(raw.hotword_publish)
            ? raw.hotword_publish as Record<string, unknown>
            : null;
          taskVersionId = typeof raw.task_version_id === "string"
            ? raw.task_version_id
            : typeof nestedPublish?.task_version_id === "string"
              ? nestedPublish.task_version_id
              : null;
        }
        const sourceMaterialization = materializationsResponse.data.items.find((item) =>
          item.status === "success" &&
          item.materialization_id &&
          !item.source_materialization_id &&
          !item.hotword_pack_version_id
        );
        const sourceMaterializationId = sourceMaterialization?.materialization_id;
        const missing = [
          !version.evalRunId && "eval_run_id",
          !taskVersionId && "task_version_id",
          !version.rootTraceId && "root_trace_id",
          !sourceMaterializationId && "source_materialization_id"
        ].filter(Boolean) as string[];
        if (missing.length) throw new Error(`${currentVersionId} 缺少 ${missing.join("、")}`);
        const binding = {
          hotwordPackVersionId: version.id,
          evalRunId: version.evalRunId as string,
          taskVersionId: taskVersionId as string,
          rootTraceId: version.rootTraceId as string,
          sourceAsset: sourceAssetKey,
          sourceMaterializationId: sourceMaterializationId as string
        } satisfies HotwordBackfillBinding;
        const taskVersionResponse = await getTaskVersion(binding.taskVersionId);
        const taskVersion = taskVersionResponse.data;
        const taskAudio = taskVersion.audio_intelligence && typeof taskVersion.audio_intelligence === "object" && !Array.isArray(taskVersion.audio_intelligence)
          ? taskVersion.audio_intelligence as Record<string, unknown>
          : null;
        const taskHotwordVersionId = typeof taskVersion.hotword_pack_version_id === "string"
          ? taskVersion.hotword_pack_version_id
          : typeof taskAudio?.hotword_pack_version_id === "string"
            ? taskAudio.hotword_pack_version_id
            : null;
        if (taskHotwordVersionId !== binding.hotwordPackVersionId) {
          throw new Error(`${binding.taskVersionId} 词包绑定不一致`);
        }
        const taskRootTraceId = typeof taskVersion.root_trace_id === "string" ? taskVersion.root_trace_id : null;
        if (taskRootTraceId && taskRootTraceId !== binding.rootTraceId) {
          throw new Error(`${binding.taskVersionId} root_trace_id 不一致`);
        }
        return {
          binding,
          taskStatus: typeof taskVersion.status === "string" ? taskVersion.status.toLowerCase() : "unknown"
        };
      })
      .then(({ binding, taskStatus }) => {
        if (!active) return;
        if (taskStatus !== "published") {
          setRecovery({
            status: "blocked",
            binding,
            reason: `TaskVersion ${binding.taskVersionId} 当前为 ${taskStatus}；请先完成人工发布。`
          });
          return;
        }
        setRecovery({
          status: "ready",
          binding,
          reason: `${binding.hotwordPackVersionId} / ${binding.evalRunId} / ${binding.taskVersionId}`
        });
      })
      .catch((error) => {
        if (!active) return;
        setRecovery({
          status: "blocked",
          reason: error instanceof Error ? error.message : "受控回填绑定恢复失败"
        });
      });
    return () => {
      active = false;
    };
  }, []);
}
