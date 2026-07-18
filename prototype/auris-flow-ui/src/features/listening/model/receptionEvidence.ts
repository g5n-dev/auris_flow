import type { ReceptionOrderCandidate } from "../../../shared/contracts/reception";
import { eventLinks } from "../../../shared/fixtures/eventLinks";

export const getReceptionLocatorEvents = (candidate?: ReceptionOrderCandidate) => {
  if (!candidate) return [];
  const byPriority = (ids: string[]) => ids.map((id) => eventLinks.find((event) => event.id === id)).filter((event): event is (typeof eventLinks)[number] => Boolean(event));
  if (candidate.id === "reception-129-cross") {
    return byPriority(["EVT-试驾-01", "EVT-报价-02", "EVT-异议-01"]);
  }
  if (candidate.id === "reception-131-missing") {
    return byPriority(["EVT-试驾-02", "EVT-试驾-01"]);
  }
  return byPriority(["EVT-报价-02", "EVT-异议-01", "EVT-报价-01"]);
};
