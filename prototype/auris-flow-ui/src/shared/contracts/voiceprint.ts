import type { DataAssetItem } from "./dataAssets";

export type VoiceprintRecord = {
  id: string;
  employee: string;
  speakerId: string;
  wav: string;
  assetId: string;
  sourceAsset: string;
  voiceAsset: string;
  session: string;
  device: string;
  store: string;
  window: string;
  status: string;
  gate: string;
  quality: {
    overall: number;
    duration: number;
    snr: number;
    purity: number;
    stability: number;
  };
  consistency: {
    ab: number;
    ac: number;
    bc: number;
  };
  samples: Array<[string, string, string, number]>;
  risks: string[];
  lineage: string[];
};

export type VoiceprintDataViewProps = {
  records: VoiceprintRecord[];
  dataAssets: DataAssetItem[];
  setActiveModule: (module: "canvas") => void;
  setSelectedAssetId: (id: string) => void;
  openListeningFromDataAsset: (asset: DataAssetItem) => void;
  openAssetsFromDataAsset: (asset: DataAssetItem) => void;
};
