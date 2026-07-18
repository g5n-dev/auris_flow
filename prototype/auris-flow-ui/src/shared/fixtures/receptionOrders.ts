import type { ReceptionOrderCandidate } from "../contracts/reception";
import type sharedListeningFixtureSchema from "./data/listening-shared-fixtures.json";
import { loadJsonFixture } from "../runtime/jsonFixture";

const sharedListeningFixture = await loadJsonFixture<typeof sharedListeningFixtureSchema>(
  new URL("./data/listening-shared-fixtures.json", import.meta.url),
  "共享调听 fixture"
);


export const receptionOrderCandidates: ReceptionOrderCandidate[] = (sharedListeningFixture.receptionOrders.receptionOrderCandidates as unknown as ReceptionOrderCandidate[]);
