import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = (path) => readFile(new URL(path, import.meta.url), "utf8");

test("提交决定必须以 receipt 清单为权威，按任务、EvidencePack、全部受影响对象、下一待办的顺序写后回读", async () => {
  const [actionSource, apiSource, modelSource] = await Promise.all([
    source("./hooks/reviewDecisionActions.ts"),
    source("../../api/humanReviewClient.ts"),
    source("./model/reviewDecisionModel.ts")
  ]);
  const taskIndex = actionSource.indexOf("getHumanReviewTask(");
  const evidenceIndex = actionSource.indexOf("getEvidencePack(");
  const affectedIndex = actionSource.indexOf("readHumanReviewAffectedObjects(");
  const nextIndex = actionSource.indexOf("refreshPendingReviewQueue(");

  assert.ok(taskIndex >= 0, "缺少 HumanReviewTask 写后回读");
  assert.ok(evidenceIndex > taskIndex, "EvidencePack 必须在 HumanReviewTask 后回读");
  assert.ok(affectedIndex > evidenceIndex, "受影响对象必须在 EvidencePack 后回读");
  assert.ok(nextIndex > affectedIndex, "只有所有回读一致后才能重新查询下一通");
  assert.match(actionSource, /validateHumanReviewDecisionClosure/);
  assert.match(
    actionSource,
    /readHumanReviewAffectedObjects\(\s*decision\.data\.affected_objects/,
    "必须读取 receipt 的完整 affected_objects，不能只回读本次 request.changes"
  );
  assert.match(apiSource, /affectedObject\.readback_url/);
  assert.match(apiSource, /Promise\.all/);
  assert.match(modelSource, /for \(const affectedObject of input\.affectedObjects\)/);
  assert.doesNotMatch(actionSource, /reviewSamplePool\.find\([^]*nextSample/);
});

test("下一通和队列切换都必须重新 GET pending 队列，空队列进入明确完成态", async () => {
  const [apiSource, readModelSource, queueLoaderSource, actionSource, navigationSource, navigationUrlSource, viewSource, e2eSource] = await Promise.all([
    source("../../api/humanReviewClient.ts"),
    source("./hooks/useListeningReadModel.ts"),
    source("./hooks/listeningQueueLoader.ts"),
    source("./hooks/reviewDecisionActions.ts"),
    source("../../shell/useShellNavigation.ts"),
    source("../../shell/navigationUrlState.ts"),
    source("./components/ListeningFeatureView.tsx"),
    source("../../../e2e/platform-bff.mjs")
  ]);

  assert.match(apiSource, /status[^]*pending[^]*queue/);
  assert.match(apiSource, /\/v1\/human-review-tasks\?/);
  assert.match(queueLoaderSource, /listPendingHumanReviewTasks/);
  assert.match(queueLoaderSource, /next_cursor/);
  assert.match(queueLoaderSource, /observedCursors/);
  assert.doesNotMatch(queueLoaderSource, /listAudioSessions/);
  assert.doesNotMatch(queueLoaderSource, /limit:\s*100/);
  assert.match(queueLoaderSource, /getAudioSession\(audioSessionId\)/);
  assert.match(
    queueLoaderSource,
    /backendId\(task,\s*"audio_session_id"\)[^]*getAudioSession\(audioSessionId\)/,
    "必须用 HumanReviewTask.audio_session_id 精确读取会话"
  );
  assert.match(readModelSource, /setListeningReadState\("complete"\)/);
  assert.match(
    readModelSource,
    /samples\.length === 0 && loaded\.taskCount > 0[^]*不能宣称队列已完成/,
    "仍有 pending 任务但证据关联失败时不得退化为空队列完成态"
  );
  assert.match(actionSource, /refreshPendingReviewQueue/);
  assert.match(actionSource, /navigateToTarget\(\{[^]*audioSessionId:\s*nextSample\.sessionId[^]*reviewTaskId:\s*nextSample\.reviewTaskId[^]*rootTraceId:\s*nextSample\.rootTraceId/);
  assert.match(navigationUrlSource, /audio_session_id/);
  assert.match(navigationUrlSource, /review_task_id/);
  assert.match(navigationUrlSource, /root_trace_id/);
  assert.match(navigationUrlSource, /return_to/);
  assert.match(navigationSource, /addEventListener\("popstate"/);
  assert.match(readModelSource + queueLoaderSource, /requestedReviewTaskId/);
  assert.match(queueLoaderSource, /requestedRootTraceId/);
  assert.match(
    queueLoaderSource,
    /const requestedPendingTask = requestedReviewTaskId[^]*const tasksToHydrate = requestedPendingTask \? \[requestedPendingTask\] : tasks/,
    "精确 review_task_id 深链只能物化目标任务，不能被同批次其他未就绪任务拖垮"
  );
  assert.match(viewSource, /当前队列复核完成/);
  assert.match(e2eSource, /listening-read-error/);
  assert.match(e2eSource, /listening-simple-mode/);
  assert.match(e2eSource, /listening-module-load-error/);
  assert.match(e2eSource, /listening-evidence-mode-error/);
  assert.match(e2eSource, /listeningSurfaceSnapshot/);
  assert.match(e2eSource, /pageErrors:\s*pageErrors\.slice/);
  assert.match(e2eSource, /structured review CTA must issue the HumanReviewDecision POST/);
  assert.match(e2eSource, /reviewRequestSequence/);
  assert.match(e2eSource, /HumanReviewDecision POST must pass the strong request contract/);
  assert.match(e2eSource, /decisionBody: decisionResponseJson/);
  assert.match(e2eSource, /submitReviewButton\.isEnabled/);
  assert.match(e2eSource, /name: "低置信覆盖 low_confidence"/);
  assert.match(e2eSource, /lowConfidenceReviewButton\.getAttribute\("aria-pressed"\)/);
  assert.match(e2eSource, /refreshable review URL must restore a ready authoritative read model/);
  assert.match(e2eSource, /next-call URL intent must restore audio_session_id, review_task_id and root_trace_id after refresh/);
});

test("DEMO 队列按业务标签归一化，production 仍把精确 queueKey 交给 BFF", async () => {
  const [readModelSource, queueLoaderSource, apiSource] = await Promise.all([
    source("./hooks/useListeningReadModel.ts"),
    source("./hooks/listeningQueueLoader.ts"),
    source("../../api/humanReviewClient.ts")
  ]);

  assert.match(readModelSource, /if \(LABEL_DEMO_MODE\)[^]*requestedDemoQueueKey[^]*reviewQueueKeyForLabel\(sample\.queueKey \?\? sample\.queue\)/);
  assert.match(
    readModelSource + await source("./hooks/reviewDecisionActions.ts"),
    /objectKind:\s*LABEL_DEMO_MODE \? "reviewSample" : "audioSession"[^]*objectId:\s*LABEL_DEMO_MODE \? samples\[0\]\.id : samples\[0\]\.sessionId/
  );
  assert.match(queueLoaderSource, /listPendingHumanReviewTasks\(queueKey(?:,|\))/);
  assert.match(apiSource, /query\.set\("queue", queue\)/);
});

test("写后回读必须核对业务根 Trace，成功态保存 root trace 并提供查看入口", async () => {
  const [modelSource, sampleSource, actionSource, viewSource] = await Promise.all([
    source("./model/reviewDecisionModel.ts"),
    source("./fixtures/reviewSamples.ts"),
    source("./hooks/reviewDecisionActions.ts"),
    source("./components/ListeningFeatureView.tsx")
  ]);

  assert.match(modelSource, /expectedRootTraceId/);
  assert.match(modelSource, /receiptRootTraceId/);
  assert.match(modelSource, /root_trace_id/);
  assert.match(sampleSource, /taskRootTraceId\s*=\s*backendId\(task,\s*"root_trace_id"\)/);
  assert.match(sampleSource, /evidenceRootTraceId\s*=\s*backendId\(evidencePack,\s*"root_trace_id"\)/);
  assert.match(sampleSource, /sessionRootTraceId\s*=\s*backendId\(session,\s*"root_trace_id"\)/);
  assert.match(sampleSource, /taskRootTraceId === evidenceRootTraceId/);
  assert.match(sampleSource, /evidenceRootTraceId === sessionRootTraceId/);
  assert.match(actionSource, /decision\.data\.raw\.root_trace_id/);
  assert.doesNotMatch(actionSource, /decision\.data\.raw\.trace_id/);
  assert.doesNotMatch(actionSource, /traceId:\s*decision\.meta\?\.trace_id/);
  assert.match(actionSource, /rootTraceId:\s*decisionRootTraceId/);
  assert.match(actionSource, /Trace \$\{decisionRootTraceId\}/);
  assert.match(viewSource, /data-testid="listening-view-trace"/);
  assert.match(viewSource, /apiRequest/);
  assert.match(viewSource, /\/v1\/traces\//);
  assert.match(viewSource, /response\.data\.trace_id !== rootTraceId/);
});

test("决定技术详情展示回执对象和 callback 强回读 URL，不新增页面", async () => {
  const [actionSource, typeSource, viewSource, technicalDetailsSource] = await Promise.all([
    source("./hooks/reviewDecisionActions.ts"),
    source("./types.ts"),
    source("./components/ListeningFeatureView.tsx"),
    source("./components/ReviewDecisionTechnicalDetails.tsx")
  ]);

  assert.match(typeSource, /affectedObjects/);
  assert.match(actionSource, /const affectedObjects = decision\.data\.affected_objects/);
  assert.match(actionSource, /rootTraceId:\s*decisionRootTraceId,\s*affectedObjects/);
  assert.match(viewSource, /ReviewDecisionTechnicalDetails/);
  assert.match(technicalDetailsSource, /listening-review-technical-details/);
  assert.match(technicalDetailsSource, /platform_callback/);
  assert.match(technicalDetailsSource, /readback_url/);
});

test("HumanReviewDecisionRequest.changes 覆盖五类人工修订并由各编辑器上报", async () => {
  const [modelSource, boundarySource, eventLinkSource, labelSource, spineSource] = await Promise.all([
    source("./model/reviewDecisionModel.ts"),
    source("./components/evidence/minimap/conversationBoundaryActions.ts"),
    source("./components/evidence/ReceptionLinkPanel.tsx"),
    source("./components/evidence/waveform/trackEditorActions.ts"),
    source("./components/evidence/trackDisplays.tsx")
  ]);

  for (const token of [
    "recording_disposition",
    "low_confidence",
    '"conversation_boundary"',
    '"event_link"',
    '"label_candidate"'
  ]) {
    assert.match(modelSource + boundarySource + eventLinkSource + labelSource, new RegExp(token));
  }
  assert.match(boundarySource, /onReviewChange/);
  assert.doesNotMatch(boundarySource, /saveConversationBoundary/);
  assert.match(eventLinkSource, /onReviewChange/);
  assert.doesNotMatch(eventLinkSource, /createEventLink|patchEventLink/);
  assert.match(labelSource, /onReviewChange/);
  assert.match(spineSource, /lowConfidence/);
});

test("首屏只有一个提交下一通主 CTA，production 假重跑和无绑定编辑必须禁用并解释", async () => {
  const [viewSource, presentationSource, spineSource, trackSource] = await Promise.all([
    source("./components/ListeningFeatureView.tsx"),
    source("./model/listeningPresentation.ts"),
    source("./components/evidence/trackDisplays.tsx"),
    source("./components/evidence/waveform/TrackEditor.tsx")
  ]);

  assert.equal((spineSource.match(/className="pr"/g) ?? []).length, 1);
  assert.match(spineSource, /提交决定并进入下一通/);
  assert.doesNotMatch(spineSource, />\s*跳过\s*</);
  assert.match(viewSource, /disabled=\{!LABEL_DEMO_MODE/);
  assert.match(viewSource, /生产模式.*未接入受控重跑/);
  assert.match(presentationSource, /if \(!LABEL_DEMO_MODE\)/);
  assert.match(trackSource, /disabled=\{!canReviseLabel/);
  assert.match(trackSource, /当前任务未绑定可修订的标签候选/);
});

test("生产证据编辑器只消费 BFF 权威目标，复杂 fixture 轨道仍只属于 DEMO", async () => {
  const [evidenceSource, authoritativeEditorSource, sampleSource, viewSource, actionSource] = await Promise.all([
    source("./components/evidence/EvidenceMode.tsx"),
    source("./components/evidence/AuthoritativeEvidenceEditor.tsx"),
    source("./fixtures/reviewSamples.ts"),
    source("./components/ListeningFeatureView.tsx"),
    source("./hooks/reviewDecisionActions.ts")
  ]);

  assert.match(evidenceSource, /const demoEditorsEnabled = LABEL_DEMO_MODE/);
  assert.match(
    evidenceSource,
    /data-testid="listening-evidence-mode"/,
    "成功进入证据审查后必须输出稳定的可观察模式标记"
  );
  assert.match(evidenceSource, /AuthoritativeEvidenceEditor/);
  assert.match(
    evidenceSource,
    /!LABEL_DEMO_MODE[^]*props\.sample\.reviewTaskId[^]*props\.sample\.rootTraceId/,
    "生产编辑器必须同时绑定 HumanReviewTask 和统一根 Trace"
  );
  assert.match(authoritativeEditorSource, /data-testid="listening-authoritative-editor"/);
  assert.doesNotMatch(authoritativeEditorSource, /LABEL_DEMO_MODE|shared\/fixtures|fixtures\/evidenceFixtures|fixtures\/boundaryFixtures/);
  for (const token of [
    "recording_disposition",
    "low_confidence",
    '"conversation_boundary"',
    '"event_link"',
    '"label_candidate"'
  ]) {
    assert.match(authoritativeEditorSource, new RegExp(token));
  }
  assert.match(sampleSource, /evidenceWindowStartMs/);
  assert.match(sampleSource, /authoritativeEventLinks/);
  assert.match(sampleSource, /authoritativeLabelCandidates/);
  assert.match(viewSource, /item\.id === "matrix"/);
  assert.match(viewSource, /生产串音矩阵尚未读取权威设备关系/);
  assert.match(actionSource, /!activeSample\.reviewTaskId/);
  assert.match(actionSource, /当前会话尚无待审任务/);
});

test("SimpleMode 生产只保留精确音频播放，AI 推荐和本地已保存仅属于显式 DEMO", async () => {
  const simpleSource = await source("./components/simple/SimpleMode.tsx");

  assert.match(simpleSource, /AuthoritativeSessionPlayback/);
  assert.match(simpleSource, /data-testid="listening-authoritative-playback"/);
  assert.match(simpleSource, /AI 推荐与本地保存已禁用/);
  assert.match(simpleSource, /data-testid="listening-simple-demo"[^]*data-source="demo"/);
  assert.match(simpleSource, /LABEL_DEMO_MODE[^]*DemoSimpleMode[^]*AuthoritativeSessionPlayback/);
  assert.doesNotMatch(simpleSource, /setTimeout\s*\(/);
});

test("生产调听不展示未实现快捷键或伪造保存时间", async () => {
  const [toolbarSource, spineSource, actionSource] = await Promise.all([
    source("./components/evidence/annotationControls.tsx"),
    source("./components/evidence/trackDisplays.tsx"),
    source("./hooks/reviewDecisionSecondaryActions.ts")
  ]);

  assert.doesNotMatch(toolbarSource, /已保存 12:31:08/);
  assert.match(toolbarSource, /写入状态以底部决定栏回读为准/);
  assert.doesNotMatch(spineSource, /J 上一通|K 停|L 下一通|Space 播|←→ ±5s|↵ 确认/);
  assert.match(actionSource, /title: "人工修订待提交"/);
  assert.match(actionSource, /title:[^]*?"主录音修订待提交"/);
  assert.match(actionSource, /title:[^]*?"低置信修订待提交"/);
  assert.doesNotMatch(actionSource, /title: "已标记主录音"|title: value \? "已标记低置信"/);
});

test("真实人审首屏的深链、成功提示、播放时间与技术元信息满足 AA 文本对比度基线", async () => {
  const [polishCss, annotationCss] = await Promise.all([
    source("../../styles/features/listening/polish.css"),
    source("../../styles/features/listening/annotation-structure.css")
  ]);

  assert.match(
    polishCss,
    /\.operation-toast\.is-success\s*\{[^}]*color:\s*#0b6b2e;/,
    "成功提示不能继续使用浅绿色小字号文本"
  );
  assert.match(
    polishCss,
    /\.deep-link-source-bar em\s*\{[^}]*color:\s*#4e5969;/,
    "深链详情文字必须达到浅色背景 AA 对比度"
  );
  assert.match(
    annotationCss,
    /\[data-theme="light"\] \.annotation-main\s*\{[^}]*--tx-3:\s*#566b72;/,
    "播放工具栏与任务元信息的弱文本色必须达到 AA 对比度"
  );
  assert.match(
    annotationCss,
    /\.atm\s*\{[^}]*color:\s*#a64b00;/,
    "播放时间不能继续使用低对比度亮橙色"
  );
});
