import type { ModuleDeepLink, ModuleKey } from "../contracts/navigation";

export const withDeepLinkOrigin = (
  target: ModuleDeepLink,
  label: string,
  module: ModuleKey = "home",
  objectLabel?: string
): ModuleDeepLink => ({
  ...target,
  origin: {
    label,
    module,
    objectLabel,
    target: target.origin?.target
  }
});
