export type ApiAuthEvent =
  | { kind: "reauth-required" }
  | { kind: "scope-rejected" };

const listeners = new Set<(event: ApiAuthEvent) => void>();
const controllers = new Set<AbortController>();
let epoch = 0;

export function subscribeApiAuthEvents(listener: (event: ApiAuthEvent) => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function emitApiAuthEvent(event: ApiAuthEvent) {
  listeners.forEach((listener) => listener(event));
}

export function advanceScopeEpoch() {
  epoch += 1;
  controllers.forEach((controller) => controller.abort("workspace-scope-changed"));
  controllers.clear();
}

export function beginScopeRequest(externalSignal?: AbortSignal | null) {
  const requestEpoch = epoch;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  controllers.add(controller);

  return {
    signal: controller.signal,
    assertCurrent() {
      if (epoch !== requestEpoch) {
        throw new DOMException("Workspace scope changed", "AbortError");
      }
    },
    release() {
      controllers.delete(controller);
      externalSignal?.removeEventListener("abort", abortFromCaller);
    }
  };
}
