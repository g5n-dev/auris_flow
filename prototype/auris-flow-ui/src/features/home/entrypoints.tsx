import { BookOpen, Database, Gauge, GitBranch, Headphones, Tags } from "lucide-react";

import type { ModuleKey } from "../../shared/contracts/navigation";

export const homeModuleEntrypoints = [
  {
    key: "data" as ModuleKey,
    label: "数据管理",
    icon: Database,
    summary: "按时间、空间、事件、人物展开音频、单据和标签结果。",
    signal: "事件关联 92.1% / 漏单 39",
    action: "查看聚合树",
    tone: "teal",
    path: ["时间", "门店", "销售", "单据事件"]
  },
  {
    key: "listening" as ModuleKey,
    label: "调听证据",
    icon: Headphones,
    summary: "一天完整音频 Minimap、波形、ASR、标签轨道和串音排查。",
    signal: "串音候选 24 / 高优先 6",
    action: "进入证据审查",
    tone: "blue",
    path: ["Minimap", "波形", "标签轨道", "单据证据"]
  },
  {
    key: "labels" as ModuleKey,
    label: "标签治理",
    icon: Tags,
    summary: "L1-L9 标签层、版本、冲突仲裁和人工标注回写。",
    signal: "冲突样本 42 / 待仲裁 6",
    action: "管理标签版本",
    tone: "violet",
    path: ["实体", "意图", "质检", "单据事件"]
  },
  {
    key: "canvas" as ModuleKey,
    label: "处理画布",
    icon: GitBranch,
    summary: "配置接入、处理、输出节点，并映射到底层任务编排。",
    signal: "失败节点 3 / 可重跑 3",
    action: "查看任务配置",
    tone: "amber",
    path: ["输入", "Agent", "模型工具", "资产输出"]
  },
  {
    key: "evaluation" as ModuleKey,
    label: "评测回流",
    icon: Gauge,
    summary: "模型版本对比、badcase、低置信样本和发布阻断。",
    signal: "边界 F1 88.6 / -2.1",
    action: "查看模型对比",
    tone: "red",
    path: ["评测集", "badcase", "归因", "发布策略"]
  },
  {
    key: "assets" as ModuleKey,
    label: "数据资产",
    icon: BookOpen,
    summary: "资产目录、血缘、回填范围、质量状态和影响下游。",
    signal: "待回填 5 / 影响下游 12",
    action: "查看资产血缘",
    tone: "green",
    path: ["原始音频", "ASR", "事件标签", "质量报告"]
  }
];
