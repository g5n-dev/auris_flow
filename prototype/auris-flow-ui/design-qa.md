# Design QA

source: external annotation workbench reference (not distributed with this repository)
prototype: `http://127.0.0.1:5173/`
viewport: `1440x1024`

## Checked

- Evidence review now uses the annotation workbench structure: `atl`, `bdg-bar`, `mm`, `ib`, `wv`, `tk`, `sp`, `sn`.
- Simple listening mode remains available and keeps its tag editing panel.
- Tag tracks support segment navigation, manual annotation creation, tag hiding, true track visibility removal, and custom layer creation.
- Custom layer creation is constrained by explicit L-level selection: L4 entity, L5 intent, L6 QA, L7 document events, L8 crosstalk evidence, and L9 Agent actions.
- Created layers are inserted after their target base track and show fixed level labels such as `L7+`; selecting a constrained tag updates the default layer name.
- Track regions support moving, left/right boundary adjustment, and automatic overlap lane stacking.
- Business document events, crosstalk evidence, ASR diff, and Agent actions remain connected to the Auris Flow mock data.
- Minimap shows imported voice-to-document event associations in event-match mode, including 10 nodes and 5 links.
- Page has no document-level overflow at `1440x1024`.
- Small viewport overflow is handled with visible scrolling: document fallback scroll, workspace horizontal scroll, annotation-main vertical scroll, and visible thin scrollbars on chip rails, track selectors, event islands, and tag lists.
- The annotation track timeline itself has horizontal and vertical scrolling; the timeline canvas keeps a wide working width and the left L-level track label column stays pinned while scrolling.
- Simple listening mode now supports speaker annotation separately from entity/intent tags: the left panel exposes role presets such as seat, sales, customer, ambient mic, crosstalk, and unknown; the right panel supports L/R/LR channel editing and saving for the active segment.
- Speaker edits are segment-scoped and reflected in the active transcript card, left role panel, and right segment editor.
- Production build passes.

## Residual Differences

- The annotation workbench is embedded inside Auris Flow's shell and right evidence panel instead of replacing the whole product chrome.
- The original `annotation.html` has deeper backend/debug tooling; this prototype keeps the user-facing workbench interactions relevant to 调听证据审查.

final result: passed

## Task Configuration Interaction Audit

source: existing design docs and task-canvas screenshots
prototype: `http://127.0.0.1:5173/`
viewport: `2048x1152`
date: `2026-06-30`

## Verdict

The current prototype is the better interaction baseline for the main product intent.

The older design direction treated canvas as a parallel top-level concept: data canvas, task canvas, asset canvas. That made the flow feel wrong because a canvas is not itself the user's primary object. The current prototype correctly centers the task configuration object: task type, canvas version, task version, schedule, A/B experiment, model service binding, output asset, and external callback/export.

## Checked

- Task configuration defaults to `流程配置`, not a generic canvas list.
- A task type can hold multiple canvas versions: production, candidate, shadow, hotfix, and experiment.
- Stage tabs filter the visible nodes so the canvas does not show every node at once.
- The right drawer explains node overview, field mapping, execution plan, run records, service binding, and Dagster mapping.
- Dagster details are visible only as configuration/diagnostic mapping, not as the business user's main language.
- ASR service integration is configured under model settings; the ASR canvas node only binds service version and IO contract.
- ASR transcription and speaker separation are modeled as separate assets: `transcript_asset` and `speaker_turns`; `speaker_transcript_view` is derived.
- Output/export is modeled as task output nodes: processed WAV upload to OBS/S3, callback URL, label result callback, evidence package export.
- `生成映射建议`, `设置定时`, `运行一次`, `发布版本`, and `同步输出资产` must each produce visible async state and result feedback.

## Design Decision

- Keep the current prototype interaction model.
- Update design docs to use `任务` as the primary navigation and put canvas management under task configuration.
- Use business terms in normal pages, and progressively reveal Dagster / ModelService / Asset Key / IO Manager / Partition / Deps in task configuration, model settings, run diagnostics, and asset backfill.
- Empty project/data states should drive users to connector-based import, not manual creation of isolated data entities.

final result: current prototype accepted as updated baseline
