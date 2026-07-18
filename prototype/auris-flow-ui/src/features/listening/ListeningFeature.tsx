import { ListeningFeatureView } from "./components/ListeningFeatureView";
import { useListeningController } from "./hooks/useListeningController";
import type { ListeningFeatureProps } from "./types";

export default function ListeningFeature(props: ListeningFeatureProps) {
  const controller = useListeningController(props);
  if (!props.active) return null;
  return <ListeningFeatureView controller={controller} />;
}
