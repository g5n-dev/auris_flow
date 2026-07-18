import { useEffect, useState } from "react";
import { getBackendHealth } from "../api/client";
import type { BackendStatus } from "../shared/contracts/operations";

export function useBackendHealth() {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>("checking");

  useEffect(() => {
    let mounted = true;
    getBackendHealth().then((status) => {
      if (mounted) setBackendStatus(status);
    });
    const timer = window.setInterval(() => {
      getBackendHealth().then((status) => {
        if (mounted) setBackendStatus(status);
      });
    }, 30000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, []);

  return backendStatus;
}
