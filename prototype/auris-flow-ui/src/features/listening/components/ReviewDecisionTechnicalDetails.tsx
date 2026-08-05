import type { BackendAffectedObjectRef } from "../../../api/client";

export function ReviewDecisionTechnicalDetails({
  affectedObjects
}: {
  affectedObjects: BackendAffectedObjectRef[];
}) {
  if (affectedObjects.length === 0) return null;
  return (
    <details data-testid="listening-review-technical-details">
      <summary>技术详情</summary>
      <div>
        {affectedObjects.map((affectedObject) => (
          <span
            key={`${affectedObject.type}:${affectedObject.id}`}
            data-object-type={affectedObject.type}
            data-readback-url={affectedObject.readback_url ?? ""}
            title={affectedObject.readback_url}
          >
            <code>
              {affectedObject.type === "platform_callback"
                ? "平台回写"
                : affectedObject.type}
            </code>
            {" · "}
            {affectedObject.id}
            {affectedObject.resource_version
              ? ` · v${affectedObject.resource_version}`
              : ""}
            {affectedObject.readback_url
              ? ` · ${affectedObject.readback_url}`
              : ""}
          </span>
        ))}
      </div>
    </details>
  );
}
