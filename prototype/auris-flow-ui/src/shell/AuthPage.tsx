import { Building2, LockKeyhole, LogIn, Mail, ShieldCheck, UserCheck, UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { AuthSession } from "../shared/contracts/auth";
import { AuthFormState, AuthMode } from "../shared/contracts/application";
import { defaultAuthForm } from "./authFormModel";

const DEMO_AUTH_ENABLED = import.meta.env.DEV;

export function AuthPage({
  onLogin,
  onAuth,
  onOidcLogin,
  restoreError,
  onRetryRestore
}: {
  onLogin: (email: string, password: string) => Promise<AuthSession>;
  onAuth: (session: AuthSession) => void;
  onOidcLogin: () => void;
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

  const runLogin = async (email: string, password: string) => {
    setAuthStep("checking");
    setError("");
    try {
      const session = await onLogin(email, password);
      onAuth(session);
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
    if (mode === "register" && (!name || !form.tenant.trim())) {
      setError(!name ? "注册需要填写姓名。" : "请选择或填写组织。");
      return;
    }
    if (mode === "register") {
      setError("当前开源基线不提供浏览器自助注册；请由租户管理员或外部 IdP 创建账户。");
      return;
    }
    await runLogin(email, password);
  };

  const quickLogin = async () => {
    setForm((current) => ({
      ...current,
      email: "demo.operator@auris.local",
      password: "auris-demo"
    }));
    await runLogin("demo.operator@auris.local", "auris-demo");
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

  const startOidcLogin = () => {
    setAuthStep("checking");
    setError("");
    try {
      onOidcLogin();
    } catch (caught) {
      setAuthStep("idle");
      setError(caught instanceof Error ? caught.message : "无法跳转身份提供方，请稍后重试。");
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-panel" aria-label="登录">
        <div className="auth-brand">
          <div className="brand-mark">A</div>
          <div>
            <span>Auris Flow</span>
            <strong>{DEMO_AUTH_ENABLED && mode === "register" ? "创建账户" : "登录工作台"}</strong>
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

        {!DEMO_AUTH_ENABLED ? (
          <div className="auth-form">
            <div className="auth-info">
              使用组织的 OpenID Connect 身份提供方登录。认证凭据不会写入浏览器存储。
            </div>
            {error && <div className="auth-error" role="alert">{error}</div>}
            <button
              type="button"
              className="auth-submit"
              disabled={authStep === "checking"}
              onClick={startOidcLogin}
              data-action-key="oidc-login"
            >
              <LogIn size={16} />
              {authStep === "checking" ? "正在跳转身份提供方" : "使用组织账号登录"}
            </button>
          </div>
        ) : (
          <>
            <div className="auth-mode-switch" role="tablist" aria-label="开发认证方式">
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
                <span className="auth-info">仅开发演示；会话凭据只保留在内存和 HttpOnly Cookie。</span>
                <button type="button" onClick={quickLogin} disabled={authStep === "checking"}>演示账号</button>
              </div>
              {mode === "register" && (
                <div className="auth-info">账户创建由租户管理员或外部 IdP 完成。</div>
              )}
              {error && <div className="auth-error" role="alert">{error}</div>}
              <button type="submit" className="auth-submit" disabled={authStep === "checking"}>
                {authStep === "checking" ? "校验中" : mode === "login" ? "登录" : "注册并进入"}
              </button>
            </form>
          </>
        )}
      </section>
    </main>
  );
}
