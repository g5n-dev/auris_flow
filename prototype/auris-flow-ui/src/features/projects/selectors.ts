import type {
  ProjectSceneProfileBinding,
  SceneProfileManifest,
  SceneProfileVersion
} from "../../api/client";
import type { AutomotiveDemoProfile, ProjectRow } from "./types";

export function deriveSelectedProfile(
  activeSceneManifest: SceneProfileManifest | null,
  activeSceneVersion: SceneProfileVersion | null,
  demoProfile: AutomotiveDemoProfile | undefined
) {
  if (activeSceneManifest) {
    return {
      scene: `${activeSceneManifest.display_name} · ${activeSceneVersion?.version ?? ""}`,
      datasource: activeSceneManifest.data_contract_refs.join(" / "),
      labelVersion: activeSceneManifest.label_version_refs.join(" / "),
      modelChain: activeSceneManifest.capabilities.join(" / "),
      quality: `${activeSceneManifest.release_requirements.length} 个发布门禁 · 快照 ${activeSceneVersion?.manifest_sha256.slice(0, 10)}`,
      ownerTeam: activeSceneManifest.roles.map((role) => role.display_name).join(" / "),
      source: "SceneProfile"
    };
  }
  if (demoProfile) {
    return { ...demoProfile, source: "汽车演示 fixture" };
  }
  return {
    scene: "未绑定已发布 SceneProfile",
    datasource: "由场景清单声明数据契约",
    labelVersion: "由场景清单引用标签版本",
    modelChain: "由场景清单声明能力与任务类型",
    quality: "生产运行前必须通过三层评测门禁",
    ownerTeam: "待场景角色配置",
    source: "未配置"
  };
}

export function deriveProjectSources(
  activeSceneManifest: SceneProfileManifest | null,
  sceneBinding: ProjectSceneProfileBinding | null,
  projectionSource: "bff" | "mock",
  selectedProject: ProjectRow
) {
  if (activeSceneManifest) {
    return activeSceneManifest.data_contract_refs.map((contractRef, index) => ({
      name: contractRef,
      type: index === 0 ? "主数据契约" : "场景数据契约",
      status: sceneBinding?.status === "active" ? "已绑定" : "待配置",
      key: contractRef,
      detail: `由 ${activeSceneManifest.display_name} 声明，实际连接器在任务配置中绑定`
    }));
  }
  if (projectionSource === "mock") {
    return [
      { name: "平台账号登录", type: "认证前置", status: "已配置", key: "platform_session", detail: "汽车演示：账户名密码换取 access_token" },
      { name: "租户/门店接口", type: "REST Source", status: "已配置", key: "tenant_list_api", detail: "汽车演示：tenant_id / store_id / org_path" },
      { name: "员工/工牌接口", type: "REST Source", status: "已配置", key: "employee_list_api", detail: "汽车演示：employee_id / badge_id / role" },
      { name: "音频 URL 接口", type: "音频引用", status: selectedProject.status === "待接入" ? "待配置" : "运行中", key: "recording_url_api", detail: "汽车演示：recording_id / audio_url / duration" }
    ];
  }
  return [{ name: "场景数据契约", type: "未配置", status: "待配置", key: "scene_profile_required", detail: "先生成或导入 SceneProfile，再进入任务配置绑定连接器" }];
}

export function deriveProjectMembers(selectedProject: ProjectRow) {
  return [
    { name: selectedProject.owner, role: "项目负责人", scope: "配置、成员、质量目标", status: "启用" },
    { name: "李准", role: "数据接入", scope: "数据源 / 任务配置", status: "启用" },
    { name: "赵研", role: "标注主管", scope: "标签体系 / 人工复核", status: "启用" },
    { name: "陈澈", role: "只读审计", scope: "运行记录 / 导出记录", status: "待确认" }
  ];
}

export function deriveProjectLabelRows(
  activeSceneManifest: SceneProfileManifest | null,
  activeSceneVersion: SceneProfileVersion | null,
  projectionSource: "bff" | "mock",
  labelVersion: string
): Array<[string, string, string]> {
  if (activeSceneManifest) {
    return [
      ["实体定义", activeSceneManifest.entities.map((item) => item.display_name).join(" / ") || "未声明", labelVersion],
      ["事件定义", activeSceneManifest.events.map((item) => item.display_name).join(" / ") || "未声明", labelVersion],
      ["文档定义", activeSceneManifest.document_types.map((item) => item.display_name).join(" / ") || "未声明", labelVersion],
      ["标签版本", activeSceneManifest.label_version_refs.join(" / "), activeSceneVersion?.version ?? ""]
    ];
  }
  if (projectionSource === "mock") {
    return [
      ["L4 实体标签", "车型 / 指导价 / 优惠金额", labelVersion],
      ["L5 意图标签", "报价承诺 / 价格异议 / 试驾承接", labelVersion],
      ["L6 质检标签", "金额冲突 / 低置信 / 串音待排除", labelVersion],
      ["L7 单据事件", "报价单创建 / 试驾预约回填", labelVersion]
    ];
  }
  return [["场景标签", "绑定 SceneProfile 后显示版本化标签引用", "未配置"]];
}

export function deriveProjectQualityRows(
  activeSceneManifest: SceneProfileManifest | null,
  pendingSceneVersionCount: number,
  selectedProject: ProjectRow
): Array<[string, number, string]> {
  if (activeSceneManifest) {
    return activeSceneManifest.release_requirements.slice(0, 4).map((requirement) => [
      activeSceneManifest.metrics.find((metric) => metric.metric_key === requirement.metric_key)?.display_name ?? requirement.metric_key,
      requirement.threshold_ppm / 10_000,
      `${requirement.gate_kind} · ${requirement.operator} ${(requirement.threshold_ppm / 10_000).toFixed(1)}%`
    ]);
  }
  return [
    ["自动通过率", Number.parseFloat(selectedProject.pass) || 0, selectedProject.pass],
    ["数据源健康", selectedProject.asset === "健康" ? 94 : selectedProject.asset === "待接入" ? 12 : 68, selectedProject.asset],
    ["待处理压降", Math.max(0, 100 - selectedProject.pending), `${selectedProject.pending} 待处理`],
    ["场景配置完整度", pendingSceneVersionCount ? 48 : 0, pendingSceneVersionCount ? "存在候选版本" : "未配置"]
  ];
}
