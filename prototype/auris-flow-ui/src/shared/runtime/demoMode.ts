// Mock candidates and metrics are opt-in. Any unset or malformed value is production truth mode.
export const LABEL_DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";
