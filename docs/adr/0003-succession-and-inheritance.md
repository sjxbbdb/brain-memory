# 灾难恢复采用同 lineage 的代际继承

Status: accepted

当实例出现硬完整性故障，或经过连续证据确认的身份/元认知漂移且 Recovery 无法恢复可信状态时，LifeKernel 冻结并封存父实例，创建同一 lineage 的新 generation/instance。successor 不是原实例无记录复活，也不是开放式繁殖；它必须在隔离环境通过同一套硬门和进步评估后，才可原子接替。

继承采用证据分层：核心目的、身份根、生命规则和 lineage 元数据必继承；有来源且已验证的生命史可以继承；技能和策略必须重新评估；事故时的未验证记忆、瞬时情绪、活动任务、未确认行动、凭证、审批令牌和外部会话不继承。每次接替都写入不可删除的 SuccessionRecord，记录事故原因、继承断点以及被隔离的内容。

## Consequences

- lineage 保持生命史和关系连续性，instance/generation 保持事故归责和审计清晰。
- 父实例只能作为只读历史存在，同一环境不会由两个活动个体同时控制。
- 失败的 successor 不会无限生成；无安全 successor 时，谱系进入可解释的 Retired/Dead 结果。
