import { Component, type ReactNode } from "react";

export class FeatureLoadBoundary extends Component<
  { children: ReactNode; label: string; testId: string; visible?: boolean },
  { error: string | null }
> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : "模块资源加载失败" };
  }

  render() {
    if (this.state.error) {
      if (this.props.visible === false) return null;
      return (
        <section
          className="module-panel wide feature-module-error"
          data-testid={this.props.testId}
          role="alert"
          style={{ minHeight: 420 }}
        >
          <div className="operation-toast is-error">
            <strong>{this.props.label}加载失败</strong>
            <span>{this.state.error}。请检查网络后重新加载。</span>
          </div>
          <button type="button" onClick={() => window.location.reload()}>重新加载</button>
        </section>
      );
    }
    return this.props.children;
  }
}
