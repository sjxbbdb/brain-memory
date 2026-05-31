# V7: Drive Engine 设计文档

> 日期: 2026-06-03 · 前置版本: V6.0 (ActivationField)

## 目标

行为开始来源于内部需求。驱动力不再是静态配置项，而是持续响应客观信号的动态需求系统。

## 架构

```
客观信号源                     V7 DriveEngine                    ActivationField
──────────────────────────────────────────────────────────────────────────────
知识缺口 ──→                                        ┌─→ survival_drive
长期停滞 ──→  drive.update() ──→ 逐tick评估 ──→   ├─→ curiosity_drive
身份冲突 ──→  (信号→增量→衰减)                     ├─→ coherence_drive
未知内容 ──→                                       ├─→ growth_drive
成功反馈 ──→                                       ├─→ exploration_drive
社交信号 ──→                                       ├─→ creation_drive
错误率  ──→                                       └─→ connection_drive
记忆衰减 ──→

Drives+Identity+Memory → GoalGenerator → [Goal,...]
                                          ↓
                                   GoalScheduler.score()
                                   (importance×urgency×drive_gain)
                                          ↓
                                   最高分Goal → CALL_TOOL intent
```

## 文件变更

| 操作 | 文件 | 量级 |
|---|---|---|
| 新增 | `brain/drive_engine.py` | ~350行 |
| 修改 | `brain/self_model.py` | -50行（删除drives代码） |
| 修改 | `brain/goal_system.py` | +30行（Goal加字段） |
| 修改 | `brain/brain_stem.py` | +30行（信号收集→驱动更新→目标生成） |
| 修改 | `brain/activation_field.py` | +10行（新增3个维度） |
| 修改 | `api/main.py` | +15行 |
| 新增 | `test_v7_integration.py` | ~200行 |

## 7个驱动力

| 驱动 | 默认 | 来源 | 信号源 |
|---|---|---|---|
| survival | 0.5 | 替换 self_preservation | 错误率↑、身份冲突、资源消耗 |
| curiosity | 0.5 | 保留 | 知识缺口、待解问题、新实体 |
| coherence | 0.6 | 保留 | 记忆冲突、元认知矛盾 |
| growth | 0.5 | 合并 | 知识缺口、长期停滞、技能掌握 |
| exploration | 0.4 | 新增 | 未知实体、盲区、好奇心 |
| creation | 0.3 | 新增 | 技能组合、成功经验 |
| connection | 0.4 | 合并 | 社交信号、长时间无交互 |

## 信号→驱动映射

```python
SIGNALS = {
    "知识缺口":  {"curiosity":+0.05, "growth":+0.03, "exploration":+0.04},
    "长期停滞":  {"growth":+0.08, "exploration":+0.05, "creation":+0.03},
    "身份冲突":  {"survival":+0.06, "coherence":+0.08, "growth":+0.05},
    "未知内容":  {"exploration":+0.06, "curiosity":+0.04},
    "成功反馈":  {"creation":+0.04, "growth":-0.02, "connection":+0.02},
    "社交信号":  {"connection":+0.05, "creation":+0.02},
    "错误率上升":{"survival":+0.07, "coherence":+0.04},
    "记忆衰减":  {"coherence":+0.03, "growth":+0.02},
}
```

## Goal新增字段

- `survival_gain` — 对生存的贡献
- `growth_gain` — 对成长的贡献
- `identity_gain` — 对身份稳定的贡献
- `source_drive` — 来源驱动力名

## GoalScheduler评分

```
score = priority×0.20 + urgency×arousal×0.15
      + survival_gain×survival_drive×0.20
      + growth_gain×growth_drive×0.20
      + identity_gain×(1-identity_stability)×0.15
      + progress×(-0.10)
```

## 验收标准

1. 无人输入30tick后自动产生≥1个目标
2. 目标进度随tick推进
3. 低优先级目标被自动取消
4. 驱动力变化后优先级重排
