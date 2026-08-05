export type ModuleKey =
  | "home"
  | "tenants"
  | "projects"
  | "canvas"
  | "data"
  | "knowledge"
  | "listening"
  | "labels"
  | "insights"
  | "evaluation"
  | "assets"
  | "settings";

export type DeepLinkObjectKind =
  | "module"
  | "audioSession"
  | "reviewSample"
  | "dataAsset"
  | "asset"
  | "evidence"
  | "knowledge"
  | "labelIntent"
  | "labelCandidate"
  | "labelReview"
  | "evaluationBadcase"
  | "evaluationCase"
  | "evaluationDataset"
  | "evaluationCapability"
  | "canvasNode"
  | "taskVersion"
  | "insightFact"
  | "setting";

export type DeepLinkFocusMode =
  | "detail"
  | "evidence"
  | "lineage"
  | "matrix"
  | "edit"
  | "source"
  | "chunk"
  | "gap"
  | "path"
  | "gate"
  | "effect"
  | "run";

export type DeepLinkOrigin = {
  label: string;
  module?: ModuleKey;
  objectLabel?: string;
  target?: ModuleDeepLink;
};

export type ModuleDeepLink = {
  module: ModuleKey;
  tab?: string;
  objectKind?: DeepLinkObjectKind;
  objectId?: string;
  audioSessionId?: string;
  reviewTaskId?: string;
  rootTraceId?: string;
  title?: string;
  detail?: string;
  window?: string;
  focusMode?: DeepLinkFocusMode;
  origin?: DeepLinkOrigin;
};

export type LinkableObjectRef = {
  label: string;
  meta: string;
  route: ModuleKey;
  target?: ModuleDeepLink;
};
