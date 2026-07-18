export const deepLinkEvidenceRegistry: Record<string, {
  label: string;
  sampleId: string;
  dataAssetId: string;
  assetKey: string;
  window: string;
  labelIntentKey: string;
  badcaseId?: string;
}> = {
  "EVP-quote-128": {
    label: "报价金额冲突证据包",
    sampleId: "sample-af-128",
    dataAssetId: "AF-128",
    assetKey: "auris/label/event_tags",
    window: "12:27:18 - 12:28:01",
    labelIntentKey: "quote",
    badcaseId: "T-8812"
  },
  "EVP-drive-129": {
    label: "试驾承接补全证据包",
    sampleId: "sample-af-129",
    dataAssetId: "AF-129",
    assetKey: "auris/audio/voice_segments",
    window: "12:28:01 - 12:29:28",
    labelIntentKey: "testDrive",
    badcaseId: "C-1028"
  },
  "EVP-asr-131": {
    label: "低置信 ASR 证据包",
    sampleId: "sample-af-131",
    dataAssetId: "AF-131",
    assetKey: "auris/model/asr_transcripts",
    window: "09:16:08 - 09:16:40",
    labelIntentKey: "dealIntent",
    badcaseId: "A-4107"
  }
};

export const deepLinkDataAssetRegistry: Record<string, {
  label: string;
  sampleId: string;
  assetKey: string;
  evidenceId: string;
  window: string;
}> = {
  "AF-128": {
    label: "AF-128 金额冲突证据资产",
    sampleId: "sample-af-128",
    assetKey: "auris/label/event_tags",
    evidenceId: "EVP-quote-128",
    window: "12:23 - 12:33"
  },
  "AF-129": {
    label: "AF-129 串音候选证据资产",
    sampleId: "sample-af-129",
    assetKey: "auris/audio/voice_segments",
    evidenceId: "EVP-drive-129",
    window: "12:28:01 - 12:29:28"
  },
  "AF-131": {
    label: "AF-131 低置信 ASR 证据资产",
    sampleId: "sample-af-131",
    assetKey: "auris/model/asr_transcripts",
    evidenceId: "EVP-asr-131",
    window: "09:15 - 09:18"
  }
};

export const deepLinkLabelCaseRegistry: Record<string, {
  label: string;
  sampleId: string;
  intentKey: string;
  evidenceId: string;
  window: string;
}> = {
  "LC-quote-001": {
    label: "报价金额冲突打标样本",
    sampleId: "sample-af-128",
    intentKey: "quote",
    evidenceId: "EVP-quote-128",
    window: "12:27:18 - 12:27:50"
  },
  "LC-cross-001": {
    label: "串音疑似打标样本",
    sampleId: "sample-af-129",
    intentKey: "crosstalk",
    evidenceId: "EVP-drive-129",
    window: "12:27:00 - 12:29:00"
  }
};
