import { Component, lazy, type ReactNode } from "react";

export const KnowledgeModule = lazy(() => import("../modules/knowledge/index"));

export class KnowledgeModuleLoadBoundary extends Component<
  { children: ReactNode },
  { error: string | null }
> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    return {
      error: error instanceof Error ? error.message : "知识库模块加载失败"
    };
  }

  render() {
    if (this.state.error) {
      return (
        <section className="module-panel wide" data-testid="knowledge-module-load-error" role="alert">
          <div className="operation-toast is-error">
            <strong>知识库模块加载失败</strong>
            <span>{this.state.error}。请检查网络后重新加载。</span>
          </div>
          <button type="button" onClick={() => window.location.reload()}>重新加载</button>
        </section>
      );
    }
    return this.props.children;
  }
}
