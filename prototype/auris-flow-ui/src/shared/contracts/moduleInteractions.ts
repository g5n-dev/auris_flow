import type { LinkableObjectRef, ModuleDeepLink, ModuleKey } from "./navigation";

export type ModuleInteractionModel = {
  searchPlaceholder: string;
  search: LinkableObjectRef[];
  filters: Array<{ label: string; result: string; detail: string }>;
  crud: Array<{
    action: string;
    target: string;
    route: ModuleKey;
    detail: string;
    deepLink?: ModuleDeepLink;
  }>;
  exportName: string;
};
