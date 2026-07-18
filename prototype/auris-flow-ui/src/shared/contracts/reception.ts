export type ReceptionLinkStatus = "AI建议" | "人工确认" | "改绑候选" | "解除关联" | "补单草稿";

export type ReceptionOrderCandidate = {
  id: string;
  sampleIds: string[];
  orderNo: string;
  title: string;
  customer: string;
  employee: string;
  store: string;
  window: string;
  source: string;
  status: ReceptionLinkStatus;
  match: number;
  risk: string;
  joinKeys: string[];
  docs: string[];
  diffs: Array<{ field: string; audio: string; order: string; state: string }>;
  eventLinkId?: string;
  writeRef: string;
  asset: string;
};
