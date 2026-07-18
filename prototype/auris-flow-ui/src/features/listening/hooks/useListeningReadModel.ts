import { getAudioSession, getHumanReviewTask, listAudioSessions, listHumanReviewTasks } from "../../../api/client";
import type { HumanReviewTask } from "../../../api/client";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { backendId, isRecordValue, recordList } from "../../../shared/runtime/records";
import { backendReviewSample, emptyReviewSample, reviewSamples } from "../fixtures/reviewSamples";
import type { ReviewSample } from "../fixtures/reviewSamples";
import type { ListeningState } from "./useListeningState";
import { useEffect, useMemo } from "react";

export function useListeningReadModel(context: ListeningState) {
  const { activeModule, activeSampleId, backendReviewSamples, currentUser, listeningReadRetry, setActiveChip, setActiveQueue, setActiveSampleId, setAgentState, setBackendReviewSamples, setListeningReadDetail, setListeningReadState, setMarkState, setPanelTab, setSelectedAssetKey, setSelectedDataAssetId, setSelectedWindow, topbarContext } = context;
  useEffect(() => {
      if (LABEL_DEMO_MODE || !currentUser || activeModule !== "listening") return;
      let cancelled = false;
      setListeningReadState("loading");
      setListeningReadDetail("正在读取 HumanReviewTask 队列并关联 AudioSession、EvidencePack 与 ASR 轨道。 ");

      const loadListeningFacts = async () => {
        try {
          const [taskResponse, sessionResponse] = await Promise.all([
            listHumanReviewTasks({ limit: 50 }),
            listAudioSessions({ limit: 20 })
          ]);
          const tasks = taskResponse.data.items.filter((task) => backendId(task, "id", "review_task_id"));
          const sessionIds = Array.from(
            new Set(
              sessionResponse.data.items
                .map((session) => backendId(session, "audio_session_id", "id"))
                .filter(Boolean)
            )
          );
          const [taskDetails, sessionDetails] = await Promise.all([
            Promise.all(tasks.map(async (task) => {
              const taskId = backendId(task, "id", "review_task_id");
              const detail = await getHumanReviewTask(taskId);
              return { ...task, ...detail.data, id: taskId } as HumanReviewTask;
            })),
            Promise.all(sessionIds.map(async (sessionId) => (await getAudioSession(sessionId)).data))
          ]);
          if (cancelled) return;
          const samples = taskDetails.flatMap((task, index) => {
            const taskEvidence = isRecordValue(task.evidence_pack) ? task.evidence_pack : {};
            const evidencePackId = backendId(taskEvidence, "evidence_pack_id", "id") || backendId(task, "evidence_pack_id");
            const directSessionId = backendId(task, "audio_session_id") || backendId(taskEvidence, "audio_session_id");
            const session = sessionDetails.find((candidate) => {
              const candidateId = backendId(candidate, "audio_session_id", "id");
              if (directSessionId && candidateId === directSessionId) return true;
              return recordList(candidate.evidence_packs).some(
                (pack) => backendId(pack, "evidence_pack_id", "id") === evidencePackId
              );
            });
            return session ? [backendReviewSample(task, session, index)] : [];
          }).map((sample, _index, all) => ({ ...sample, progressTotal: all.length }));
          setBackendReviewSamples(samples);
          if (samples.length === 0) {
            setListeningReadState("empty");
            setListeningReadDetail(
              tasks.length === 0
                ? "当前租户/项目没有可读的 HumanReviewTask；未回退本地样本。"
                : `${tasks.length} 个 HumanReviewTask 未关联到可读 AudioSession/EvidencePack，已阻断错误展示。`
            );
            return;
          }
          setActiveSampleId(samples[0].id);
          setActiveQueue(samples[0].queue);
          setActiveChip(samples[0].queue);
          setSelectedDataAssetId(samples[0].dataAssetId);
          setSelectedAssetKey(samples[0].assetKey);
          setListeningReadState("ready");
          setListeningReadDetail(
            `已从 BFF 读取 ${samples.length} 个复核对象、${sessionDetails.length} 个音频会话；写操作后将回读同一任务。`
          );
        } catch (error) {
          if (cancelled) return;
          setBackendReviewSamples([]);
          setListeningReadState("error");
          setListeningReadDetail(error instanceof Error ? error.message : "调听权威事实读取失败");
        }
      };
      void loadListeningFacts();
      return () => {
        cancelled = true;
      };
    }, [activeModule, currentUser?.userId, listeningReadRetry, topbarContext.project, topbarContext.tenant]);

  const reviewSamplePool = LABEL_DEMO_MODE ? reviewSamples : backendReviewSamples;

  const activeSample = useMemo(
      () => reviewSamplePool.find((sample) => sample.id === activeSampleId) ?? reviewSamplePool[0] ?? emptyReviewSample,
      [activeSampleId, reviewSamplePool]
    );

  const selectReviewSample = (sample: ReviewSample) => {
      setActiveSampleId(sample.id);
      setSelectedDataAssetId(sample.dataAssetId);
      setSelectedAssetKey(sample.assetKey);
      setActiveQueue(sample.queue);
      setActiveChip(sample.queue);
      setSelectedWindow(sample.window);
      setAgentState("pending");
      setMarkState("none");
      setPanelTab("agent");
    };

  return { ...context, reviewSamplePool, activeSample, selectReviewSample };
}

export type ListeningReadModel = ReturnType<typeof useListeningReadModel>;
