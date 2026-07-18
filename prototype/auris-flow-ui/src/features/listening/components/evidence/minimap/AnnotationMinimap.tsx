import type { AnnotationMinimapProps } from "./annotationMinimapTypes";
import { AnnotationMinimapView } from "./AnnotationMinimapView";
import { useAnnotationMinimapController } from "./useAnnotationMinimapController";

export function AnnotationMinimap(props: AnnotationMinimapProps) {
  const controller = useAnnotationMinimapController(props);
  return <AnnotationMinimapView controller={controller} />;
}
