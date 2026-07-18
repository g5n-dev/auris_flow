import type { ExecutionState } from "../types";
import type canvasFixtureSchema from "./data/canvas-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasFixture = await loadJsonFixture<typeof canvasFixtureSchema>(
  new URL("./data/canvas-fixtures.json", import.meta.url),
  "画布 fixture"
);


export const asrServiceProfileSource = (canvasFixture.audioRuntime.asrServiceProfileSource as unknown as { name: string; serviceId: string; endpoint: string; version: string; auth: string; timeout: string; retry: string; owner: string; ioManager: string; providers: string[][]; request: string[][]; response: string[][]; assets: string[][]; dagster: string[][]; });

export const audioNodeRuntimeParamsSource: Record<"vad" | "diar" | "asr", string[][]> = (canvasFixture.audioRuntime.audioNodeRuntimeParamsSource as unknown as Record<"vad" | "diar" | "asr", string[][]>);

export const executionStateMetaSource: Record<ExecutionState, { label: string; detail: string }> = (canvasFixture.audioRuntime.executionStateMetaSource as unknown as Record<ExecutionState, { label: string; detail: string; }>);
