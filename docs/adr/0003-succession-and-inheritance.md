# 灾难恢复采用同 lineage 的代际继承

Status: accepted

当实例出现硬完整性故障，或经过连续证据确认的身份/元认知漂移且 Recovery 无法恢复可信状态时，LifeKernel 进入有界接替流程，创建同一 lineage 的新 generation/instance。successor 不是原实例无记录复活，也不是开放式繁殖；它必须在隔离环境通过同一套硬门和进步评估后，才可接替。

继承采用证据分层：核心目的、身份根、生命规则和 lineage 元数据必继承；有来源且已验证、且递归敏感数据检查通过的生命史可以继承；技能和策略必须重新评估；事故时的未验证记忆、瞬时情绪、活动任务、未确认行动、凭证、审批令牌和外部会话不继承。任何 anchor 的 value、metadata、证据引用或来源只要包含凭据字段、token、会话、URL 或宿主绝对路径，就整体排除，决策记录只留下固定原因与内容哈希，不复制原文。技能/策略的裸 `reevaluated_anchor_ids` 不是证据；生产路径必须逐项提供可验证且哈希固定的 evaluator receipt，只有显式 legacy/offline 适配才可启用旧式 attestation。每次接替都写入不可删除的 SuccessionRecord，记录事故原因、继承断点以及被隔离的内容。

持久接替采用 `SUCCESSION_PENDING` 两阶段封口：父代先进入非终态 pending，随后依次持久化 child genesis、successor anchor set 和 SuccessionRecord；只有三者在同一 lineage/generation/plan 关系上全部核验通过，父代才写入 `succession_parent_sealed` 终态并释放 lease。child 在父代封口及独立激活证明之前不能 claim 控制权。跨 sink 目前不是单一数据库事务；中途失败会保留 pending 父代并阻止重启/激活，而不是伪造一个成功 successor。该 safe-stop 需要宿主告警与显式 repair/resume 运维流程，不能自动猜测完成。

因此，生产 `SuccessionCoordinator` 必须同时绑定 life-event、anchor-vault 和
`SuccessionRecord` 三类持久 sink；缺失任一 sink 直接拒绝构造。`append_life_event_handover`
也只接受仍处于 `SUCCESSION_PENDING` 且已绑定目标 child 的父代，历史终态行只能走读取/迁移
路径，不能被该写接口重新注入子代。无持久化的离线/单元演练必须显式使用 `profile="legacy"`。

## Consequences

- lineage 保持生命史和关系连续性，instance/generation 保持事故归责和审计清晰。
- 父实例只能作为只读历史存在，同一环境不会由两个活动个体同时控制。
- 失败的 successor 不会无限生成；无安全 successor 时，谱系进入可解释的 Retired/Dead 结果。
- 中途故障优先留下可审计的 pending 阻断态；可用性恢复不能绕过 anchor、record、父代封口和激活证明。
