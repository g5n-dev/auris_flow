import type { DagsterBinding } from "../types";
import type canvasFixtureSchema from "./data/canvas-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasFixture = await loadJsonFixture<typeof canvasFixtureSchema>(
  new URL("./data/canvas-fixtures.json", import.meta.url),
  "画布 fixture"
);


export const baseDagsterBindingsSource: Record<string, Omit<DagsterBinding, "partition">> = (canvasFixture.dagsterBindings.baseDagsterBindingsSource as unknown as Record<string, Omit<DagsterBinding, "partition">>);
