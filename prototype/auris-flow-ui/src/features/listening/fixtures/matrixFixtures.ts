import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";
import type { MatrixDimension, MatrixFinding, MatrixStatus } from "../components/matrix/matrixModeTypes";
import type listeningFixtureSchema from "./data/listening-fixtures.json";

const listeningFixture = await loadJsonFixture<typeof listeningFixtureSchema>(
  new URL("./data/listening-fixtures.json", import.meta.url),
  "调听 fixture"
);
const matrixFixture = listeningFixture.matrixFixtures;

export const matrixStatusMeta =
  matrixFixture.statusMeta as Record<MatrixStatus, { label: string; severity: number }>;
export const matrixRows = matrixFixture.matrixRows;
export const matrixFindings = matrixFixture.matrixFindings as MatrixFinding[];
export const matrixDimensions = matrixFixture.matrixDimensions as unknown as MatrixDimension[];
