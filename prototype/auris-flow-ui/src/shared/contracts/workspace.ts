export type TopbarContextKey = "tenant" | "project" | "store" | "date" | "model" | "label";

export type TopbarContextState = Record<TopbarContextKey, string>;
