import type { EntitySelectOption } from "../../shared/ui/EntitySelect";
import type { AutomotiveDemoProfile, ProjectRow } from "./types";

export const projectRows: ProjectRow[] = [
  {
    "name": "销售话术质检",
    "owner": "王敏",
    "status": "运行中",
    "added": "2,846",
    "pending": 42,
    "pass": "88.6%",
    "asset": "健康"
  },
  {
    "name": "试驾流程分析",
    "owner": "李准",
    "status": "评测中",
    "added": "1,204",
    "pending": 18,
    "pass": "83.2%",
    "asset": "待回填"
  },
  {
    "name": "门店接待洞察",
    "owner": "赵研",
    "status": "运行中",
    "added": "3,612",
    "pending": 21,
    "pass": "91.8%",
    "asset": "健康"
  },
  {
    "name": "售后维修工单关联",
    "owner": "陈澈",
    "status": "异常",
    "added": "764",
    "pending": 37,
    "pass": "72.5%",
    "asset": "失败 2"
  }
];

export const projectSceneOptions: EntitySelectOption[] = [
  {
    "value": "AI 生成场景候选",
    "label": "AI 生成场景候选",
    "description": "根据业务目标、数据契约和知识资产生成可审查 Profile"
  },
  {
    "value": "导入场景配置",
    "label": "导入场景配置",
    "description": "导入符合 scene-profile/1 的版本化清单"
  },
  {
    "value": "稍后配置",
    "label": "稍后配置",
    "description": "只创建项目边界，场景未发布前禁止生产运行"
  }
];

export const projectDataModeOptions: EntitySelectOption[] = [
  {
    "value": "连接器画布导入",
    "label": "连接器画布导入",
    "description": "立即进入账号、API、音频 URL、事件接口配置"
  },
  {
    "value": "先创建空项目",
    "label": "先创建空项目",
    "description": "只建项目壳，稍后再接入数据"
  }
];

export const projectLabelVersionOptions: EntitySelectOption[] = [
  {
    "value": "由 SceneProfile 候选生成",
    "label": "随场景候选生成",
    "description": "模型提出标签引用，校验时必须解析为真实版本"
  },
  {
    "value": "导入版本引用",
    "label": "导入版本引用",
    "description": "由导入的 scene-profile/1 清单声明强 ID"
  },
  {
    "value": "稍后绑定",
    "label": "稍后绑定",
    "description": "项目可创建，但绑定标签版本前禁止生产发布"
  }
];

export const automotiveDemoProfiles: Record<string, AutomotiveDemoProfile> = {
  "销售话术质检": {
    "scene": "汽车门店销售质检",
    "datasource": "后台登录 + 租户/员工接口 + 音频 URL + 认证事件",
    "labelVersion": "v1.8.4",
    "modelChain": "VAD v2.4-fast / ASR v2.3.1 / Tagging v1.8.4",
    "quality": "自动通过率 88%+ / 金额冲突必审",
    "ownerTeam": "销售质检组"
  },
  "试驾流程分析": {
    "scene": "试驾预约与车载音频核验",
    "datasource": "试驾单 API + 车载设备音频 URL",
    "labelVersion": "v1.8.4",
    "modelChain": "VAD v2.4-fast / ASR v2.3.1 / EventMatch v1.2",
    "quality": "事件召回 92%+ / 缺音频必回填",
    "ownerTeam": "门店运营组"
  },
  "门店接待洞察": {
    "scene": "门店接待质量和客流洞察",
    "datasource": "门店麦克风 / 工牌 / 接待事件",
    "labelVersion": "v1.9.0-rc2",
    "modelChain": "Diar v1.6 / Insight Agent v3",
    "quality": "门店异常召回 90%+",
    "ownerTeam": "洞察分析组"
  },
  "售后维修工单关联": {
    "scene": "售后工单与电话录音关联",
    "datasource": "工单 API + PBX 录音",
    "labelVersion": "v1.8.4",
    "modelChain": "ASR v2.3.1 / RepairTag v1.1",
    "quality": "工单关联准确率 86%+",
    "ownerTeam": "售后质检组"
  }
};
