import { Component, Suspense, type ReactNode } from "react";

type LazyBranchBoundaryProps = {
  children: ReactNode;
  label: string;
  minHeight?: number;
  resetKey: string;
  testId: string;
};

class LazyBranchErrorBoundary extends Component<LazyBranchBoundaryProps, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : "资源加载失败" };
  }

  componentDidUpdate(previous: LazyBranchBoundaryProps) {
    if (this.state.error && previous.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section
        className="module-panel wide feature-module-error"
        data-testid={`${this.props.testId}-error`}
        role="alert"
        style={{ minHeight: this.props.minHeight ?? 240 }}
      >
        <div className="operation-toast is-error">
          <strong>{this.props.label}加载失败</strong>
          <span>{this.state.error}。请检查网络后重新加载。</span>
        </div>
        <button type="button" onClick={() => window.location.reload()}>重新加载</button>
      </section>
    );
  }
}

export function LazyBranchBoundary(props: LazyBranchBoundaryProps) {
  const fallback = (
    <section
      className="module-panel wide"
      data-testid={`${props.testId}-loading`}
      role="status"
      aria-live="polite"
      style={{ minHeight: props.minHeight ?? 240 }}
    >
      <div className="operation-toast is-pending">
        <strong>正在加载{props.label}</strong>
        <span>正在获取当前交互分支资源。</span>
      </div>
    </section>
  );
  return (
    <LazyBranchErrorBoundary {...props}>
      <Suspense fallback={fallback}>{props.children}</Suspense>
    </LazyBranchErrorBoundary>
  );
}
