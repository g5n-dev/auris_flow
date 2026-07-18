export const audioServiceParamGroupsSource = [
  {
    title: "Provider 路由",
    rows: [
      ["provider_ref", "auris-audio-stack / ali-nls-prod / volc-bigmodel-audio"],
      ["fallback_order", "self_hosted → ali → volc → open_source"],
      ["cost_guard", "单租户分钟成本、队列和 SLA 联合决策"]
    ]
  },
  {
    title: "音频预处理",
    rows: [
      ["sample_rate", "16k / 24k auto"],
      ["denoise", "store_noise_profile + mic_profile"],
      ["chunk_strategy", "auto / 60s / streaming_cursor / full_day_minimap"]
    ]
  },
  {
    title: "VAD 参数",
    rows: [
      ["energy_threshold", "按门店噪声画像动态调整"],
      ["min_speech_ms", "320ms"],
      ["merge_gap_ms", "480ms，避免一句话被切碎"]
    ]
  },
  {
    title: "Diar 参数",
    rows: [
      ["speaker_policy", "employee_badge + device + diar_cluster"],
      ["max_speakers", "销售/客户/展厅麦/串入声"],
      ["overlap_policy", "串音候选保留双归因，人工确认后写回"]
    ]
  },
  {
    title: "ASR 参数",
    rows: [
      ["hotword_pack_version_id", "hwpv-auto-sales-v1-8"],
      ["execution_mode", "production"],
      ["word_timestamps", "true"],
      ["domain_normalizer", "金额、车型、试驾时间结构化"]
    ]
  },
  {
    title: "回调与资产",
    rows: [
      ["callback_asset", "audio_intelligence_materialization"],
      ["output_policy", "voice_segments / speaker_turns / transcript_asset 分开落盘"],
      ["trace_tags", "tenant_id, provider, model_chain, experiment_arm"]
    ]
  }
];

export const audioServiceObservabilityRowsSource = [
  ["自研", "P95 4.8s", "VAD漏检 1.8%", "DER 9.6%", "WER 7.9%", "¥0.012/min", "主路由"],
  ["阿里", "P95 6.2s", "VAD漏检 2.2%", "DER 11.4%", "WER 8.4%", "¥0.026/min", "兜底"],
  ["火山", "P95 5.6s", "VAD漏检 2.0%", "DER 12.1%", "WER 8.1%", "¥0.023/min", "高并发"],
  ["开源", "P95 18.4s", "VAD漏检 2.9%", "DER 13.8%", "WER 9.7%", "¥0.004/min", "离线回填"]
];

export const audioServiceOptimizationRowsSource = [
  ["自动路由", "SLA + 成本 + 质量", "门店实时任务走自研/火山；离线回填可走开源。"],
  ["参数自调优", "按门店噪声画像", "根据空转写、串音误报和人工修正结果调整 VAD 阈值。"],
  ["影子评测", "provider_shadow=true", "同一分区可并行跑候选 provider，只写评测资产不覆盖生产。"],
  ["失败回退", "stage-level fallback", "ASR 失败复用 VAD/Diar；Diar 低置信转人工说话人标注。"]
];
