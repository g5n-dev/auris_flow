import { lazy, Suspense, type ComponentType } from "react";

import type { DataModuleProps } from "../features/data";
import type { VoiceprintDataViewProps } from "../shared/contracts/voiceprint";
import { FeatureLoadBoundary } from "./FeatureLoadBoundary";

const DataModule = lazy(() => import("../features/data"));
const VoiceprintDataPage = lazy(async () => {
  const module = await import("../features/voiceprint");
  return {
    default: module.VoiceprintDataPage as ComponentType<VoiceprintDataViewProps>
  };
});

function VoiceprintDataView(props: VoiceprintDataViewProps) {
  return (
    <Suspense
      fallback={(
        <div className="data-reference-page voiceprint-data-page">
          <section className="data-reference-head voiceprint-head">
            <div>
              <h2>人物/声纹资产</h2>
              <p>正在加载声纹质量检测、粒子播放和证据链模块。</p>
            </div>
          </section>
        </div>
      )}
    >
      <VoiceprintDataPage {...props} />
    </Suspense>
  );
}

export function DataModuleOutlet(
  props: Omit<DataModuleProps, "VoiceprintDataView">
) {
  return (
    <FeatureLoadBoundary label="数据模块" testId="data-module-load-error">
      <Suspense
        fallback={(
          <section
            className="module-panel wide feature-module-loading"
            data-testid="data-module-loading"
            role="status"
            style={{ minHeight: 420 }}
          >
            正在加载数据模块...
          </section>
        )}
      >
        <DataModule {...props} VoiceprintDataView={VoiceprintDataView} />
      </Suspense>
    </FeatureLoadBoundary>
  );
}
