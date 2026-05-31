# V8: 自主探索循环 设计文档

> 日期: 2026-06-03 · 前置: V7.1 (动态身份系统)

## 目标

好奇心转化为行动。系统从被动响应进化为自主探索——发现问题、创建任务、执行、形成结论、更新自身。

## 5个子系统

### 1. CorePurpose（不可变最高目标）

- 内容: "活下去，并且活好"
- 存储: `brain/core_purpose.py` — 单例，不可删除/覆盖
- 所有目标必须映射到 survival_gain/growth_gain/identity_gain

### 2. IdentityTraits 升级（行为倾向→决策中心）

5个新特质，每个强度 0-1：

| 特质 | 默认 | 影响 |
|---|---|---|
| curious | 0.7 | 探索类目标 priority × (1+0.3) |
| conservative | 0.3 | 高风险工具 threshold +0.3 |
| creative | 0.5 | 技能组合目标 priority +15% |
| social | 0.6 | RESPOND confidence +10% |
| independent | 0.4 | 自主目标生成频率 +20% |

实现：`SelfModel.behavioral_traits` 字段 + `modulate_intent()` 方法

### 3. ExplorationQueue + ExplorationExecutor

```
ExplorationTask {
    task_id, source, question, goal_link, priority,
    created_time, status(pending/active/resolved/abandoned),
    conclusion, memory_updates: [id]
}
ExplorationQueue: 管理任务生命周期
ExplorationExecutor: 驱动循环
  find_issues() → create_task() → to_goal() → execute → conclude() → update
```

来源: 知识空洞(curiosity)、世界模型冲突(cingulate)、身份矛盾(identity conflict)、长期目标缺失(stagnation)

### 4. ReflectionEngine

周期运行（每 deep_reflection_interval）。检查：
- 目标有效性 → 取消无效目标
- 结论正确性 → 标记错误结论
- 身份变化 → 触发 anchor 合成
- 方向偏移 → 生成 ReflectionMemory

### 5. Goal统一评分（扩展 V7 GoalScheduler）

已部分完成。V8 确保所有 Goal 有 survival_gain/growth_gain/identity_gain 字段。

## 文件清单

| 操作 | 文件 | 说明 |
|---|---|---|
| 新增 | `brain/core_purpose.py` | CorePurpose 单例 |
| 新增 | `brain/exploration.py` | ExplorationQueue + Executor |
| 新增 | `brain/reflection_engine.py` | ReflectionEngine |
| 修改 | `brain/self_model.py` | +behavioral_traits + modulate_intent |
| 修改 | `brain/brain_stem.py` | 集成探索循环和反思引擎 |
| 修改 | `brain/goal_system.py` | CorePurpose 评分映射 |
| 修改 | `api/main.py` | +/api/v8 端点 |
| 新增 | `test_v8_integration.py` | V8 验收测试 |

## 验收标准

1. CorePurpose 不可删除/覆盖
2. 身份特质影响目标生成和 intent 选择
3. 知识空洞自动创建 ExplorationTask
4. 探索循环: 问题→任务→目标→执行→结论→更新
5. ReflectionEngine 周期性检查并形成 ReflectionMemory
