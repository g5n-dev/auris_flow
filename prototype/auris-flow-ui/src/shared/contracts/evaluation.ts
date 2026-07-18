export type EvaluationCapabilityKey = "asr" | "asr-hotword" | "boundary" | "diarization" | "tagging" | "prompt";

export type EvaluationCapabilityRow = {
  key: EvaluationCapabilityKey;
  ability: string;
  baseline: string;
  candidate: string;
  delta: string;
  blocker: string;
  gate: string;
  dataset: string;
  samples: number;
  badcases: number;
  humanQueue: number;
  assetKey: string;
  sample: string;
  evidence: string;
  owner: string;
};
