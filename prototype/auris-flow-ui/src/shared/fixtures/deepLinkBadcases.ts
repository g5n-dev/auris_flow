export const deepLinkBadcaseRegistry: Record<string, {
  label: string;
  sampleId: string;
  dataAssetId: string;
  assetKey: string;
  capability: string;
  window: string;
}> = {
  "B-2031": {
    label: "边界过碎 badcase",
    sampleId: "sample-af-128",
    dataAssetId: "AF-128",
    assetKey: "auris/audio/voice_segments",
    capability: "boundary",
    window: "12:23 - 12:33"
  },
  "T-8812": {
    label: "报价金额标签冲突 badcase",
    sampleId: "sample-af-128",
    dataAssetId: "AF-128",
    assetKey: "auris/label/event_tags",
    capability: "tagging",
    window: "12:27:18 - 12:28:01"
  },
  "C-1028": {
    label: "串音误归属 badcase",
    sampleId: "sample-af-129",
    dataAssetId: "AF-129",
    assetKey: "auris/audio/voice_segments",
    capability: "diarization",
    window: "12:27 - 12:29"
  },
  "A-4107": {
    label: "ASR 热词误识别 badcase",
    sampleId: "sample-af-131",
    dataAssetId: "AF-131",
    assetKey: "auris/model/asr_transcripts",
    capability: "asr-hotword",
    window: "09:16:08 - 09:16:40"
  }
};
