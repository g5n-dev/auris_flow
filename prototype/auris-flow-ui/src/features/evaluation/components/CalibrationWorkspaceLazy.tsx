import { lazy } from "react";

export const CalibrationWorkspace = lazy(
  () => import("../../../modules/calibration")
);
