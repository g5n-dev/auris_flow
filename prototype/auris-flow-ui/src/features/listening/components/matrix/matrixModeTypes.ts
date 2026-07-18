import type { MarkState, Mode } from "../../types";

export type MatrixModeProps = {
  setMode: (mode: Mode) => void;
  selectedWindow: string;
  setSelectedWindow: (value: string) => void;
  setMarkState: (state: MarkState) => void;
};

export type MatrixStatus = "main" | "danger" | "candidate" | "low" | "missing" | "watch" | "normal";

export type MatrixFinding = {
    key: string;
    rowId: string;
    col: number;
    status: MatrixStatus;
    label: string;
    score: number;
    overlap: number;
    records: number;
    reason: string;
    evidence: string[];
  };

export type MatrixDimension = {
    key: "time" | "space" | "event" | "person" | "doc";
    label: string;
    route: string;
    summary: string;
    cols: number[];
    rowIds: string[];
    defaultCell: string;
  };
