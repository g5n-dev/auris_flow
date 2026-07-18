import { Building2, LockKeyhole, LogIn, Mail, ShieldCheck, UserCheck, UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { AuthSession } from "../shared/contracts/auth";
import { AuthFormState, AuthMode } from "../shared/contracts/application";
import { defaultAuthForm } from "./authFormModel";

export function AuthPage({
  onLogin,
  onAuth,
  restoreError,
  onRetryRestore
}: {
  onLogin: (email: string, password: string) => Promise<AuthSession>;
  onAuth: (session: AuthSession, remember: boolean) => void;
  restoreError?: string | null;
  onRetryRestore?: () => void;
}) {
  const [mode, setMode] = useState<AuthMode>("login");
  const [form, setForm] = useState<AuthFormState>(defaultAuthForm);
  const [error, setError] = useState("");
  const [authStep, setAuthStep] = useState<"idle" | "checking" | "ready">("idle");

  const updateForm = <K extends keyof AuthFormState>(key: K, value: AuthFormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setError("");
  };

  const runLogin = async (email: string, password: string, remember: boolean) => {
    setAuthStep("checking");
    setError("");
    try {
      const session = await onLogin(email, password);
      onAuth(session, remember);
      setAuthStep("ready");
    } catch (caught) {
      setAuthStep("idle");
      setError(caught instanceof Error ? caught.message : "认证服务暂时不可用，请稍后重试。");
    }
  };

  const submitAuth = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const email = form.email.trim();
    const password = form.password.trim();
    const name = form.name.trim();
    if (!email || !email.includes("@")) {
      setError("请输入有效邮箱。");
      return;
    }
    if (password.length < 6) {
      setError("密码至少 6 位。");
      return;
    }
    if (mode === "register" && !name) {
      setError("注册需要填写姓名。");
      return;
    }
    if (mode === "register" && !form.tenant.trim()) {
      setError("请选择或填写组织。");
      return;
    }
    if (mode === "register") {
      setError("当前开源基线不提供浏览器自助注册；请由租户管理员或外部 IdP 创建账户。 ");
      return;
    }
    await runLogin(email, password, form.remember);
  };

  const quickLogin = async () => {
    setForm((current) => ({
      ...current,
      email: "demo.operator@auris.local",
      password: "auris-demo"
    }));
    await runLogin("demo.operator@auris.local", "auris-demo", form.remember);
  };

  const switchMode = (nextMode: AuthMode) => {
    setMode(nextMode);
    setError("");
    setAuthStep("idle");
    setForm((current) => ({
      ...current,
      name: nextMode === "register" ? current.name || "Demo Operator" : current.name,
      password: nextMode === "register" ? "" : current.password
    }));
  };

  return (
    <main className="auth-shell">
      <section className="auth-panel" aria-label="登录注册">
        <div className="auth-brand">
          <div className="brand-mark">A</div>
          <div>
            <span>Auris Flow</span>
            <strong>{mode === "login" ? "登录工作台" : "创建账户"}</strong>
          </div>
        </div>

        {restoreError && (
          <div className="auth-restore-error" role="alert">
            <span>{restoreError}</span>
            <button type="button" onClick={onRetryRestore} data-action-key="retry-auth-restore">
              重试恢复
            </button>
          </div>
        )}

        <div className="auth-mode-switch" role="tablist" aria-label="认证方式">
          <button type="button" role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}>
            <LogIn size={15} />
            登录
          </button>
          <button type="button" role="tab" aria-selected={mode === "register"} className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}>
            <UserPlus size={15} />
            注册
          </button>
        </div>

        <form className="auth-form" onSubmit={submitAuth}>
          {mode === "register" && (
            <label>
              <span>姓名</span>
              <div className="auth-input">
                <UserCheck size={15} />
                <input value={form.name} onChange={(event) => updateForm("name", event.target.value)} placeholder="输入姓名" autoComplete="name" />
              </div>
            </label>
          )}
          <label>
            <span>邮箱</span>
            <div className="auth-input">
              <Mail size={15} />
              <input value={form.email} onChange={(event) => updateForm("email", event.target.value)} placeholder="name@company.com" autoComplete="email" />
            </div>
          </label>
          <label>
            <span>密码</span>
            <div className="auth-input">
              <LockKeyhole size={15} />
              <input value={form.password} onChange={(event) => updateForm("password", event.target.value)} placeholder="至少 6 位" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} />
            </div>
          </label>
          {mode === "register" && (
            <>
              <label>
                <span>组织</span>
                <div className="auth-input">
                  <Building2 size={15} />
                  <input value={form.tenant} onChange={(event) => updateForm("tenant", event.target.value)} placeholder="公司或租户名称" autoComplete="organization" />
                </div>
              </label>
              <label>
                <span>邀请码</span>
                <div className="auth-input">
                  <ShieldCheck size={15} />
                  <input value={form.inviteCode} onChange={(event) => updateForm("inviteCode", event.target.value)} placeholder="可选" />
                </div>
              </label>
            </>
          )}
          <div className="auth-form-row">
            <label className="auth-check">
              <input type="checkbox" checked={form.remember} onChange={(event) => updateForm("remember", event.target.checked)} />
              <span>保持登录</span>
            </label>
            <button type="button" onClick={quickLogin} disabled={authStep === "checking"}>演示账号</button>
          </div>
          {mode === "register" && (
            <div className="auth-info">账户创建由租户管理员或外部 IdP 完成；本地开发环境只签发预置账户的短期会话。</div>
          )}
          {error && <div className="auth-error">{error}</div>}
          <button type="submit" className="auth-submit" disabled={authStep === "checking"}>
            {authStep === "checking" ? "校验中" : mode === "login" ? "登录" : "注册并进入"}
          </button>
        </form>

      </section>
    </main>
  );
}
