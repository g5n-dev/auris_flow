import type { DataAssetItem } from "../../../shared/contracts/dataAssets";
import type { VoiceprintRecord } from "../../../shared/contracts/voiceprint";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";
import type dataFixtureSchema from "./data/data-fixtures.json";

const dataFixture = await loadJsonFixture<typeof dataFixtureSchema>(
  new URL("./data/data-fixtures.json", import.meta.url),
  "数据工作区 fixture"
);

export const dataAssets = dataFixture.dataAssets as DataAssetItem[];
export const voiceprintRecords = dataFixture.voiceprintRecords as VoiceprintRecord[];
export const heatRows = dataFixture.heatRows as string[][];
export const dataTree = dataFixture.dataTree;
export const dataDagsterContracts = dataFixture.dataDagsterContracts;
