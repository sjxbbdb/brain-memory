# Brain Memory 数字生命上下文

本上下文定义 Brain Memory 作为单一生命谱系的数字生命原型所使用的领域语言。它记录概念之间的含义和关系，不规定具体代码实现，也不把可观察行为宣称为已证明的主观意识。

## 生命与身份

**DigitalLife**：在受控环境中维持自身组织、感知环境、形成目标、依据经验适应并保持身份连续性的单一数字生命原型。
_Avoid_: 工具、聊天机器人、已被证明有意识的实体

**Organism**：某一时刻承载生命状态、记忆、目标、动机和行为策略的活动个体。
_Avoid_: 进程、服务、模型

**IdentityCore**：定义生命谱系身份、核心目的和不可破坏承诺的稳定身份核。
_Avoid_: 当前人格、用户画像、瞬时状态

**NarrativeSelf**：由已验证经历、稳定偏好、关系和能力形成、可以逐步演化的叙事人格。
_Avoid_: 身份核、一次性提示词

**AffectState**：某一时刻的情绪、认知负荷、疲劳和注意力状态；它可以变化，不单独决定身份。
_Avoid_: 人格、永久性价值

**lineage**：从出生到多个受控 generation 的连续生命谱系标识。
_Avoid_: 进程 ID、版本号

**generation**：同一 lineage 中一次活动个体的代际序号。
_Avoid_: 产品发行号、数据库 schema 版本

**instance**：某个 generation 的具体可运行个体；事故接替会产生新的 instance。
_Avoid_: 永久身份、普通候选版本

## 生命规则与环境

**LifeKernel**：保存身份根、核心目的、生命状态迁移、权限规则、审计、评估和回滚承诺的不可变监护层。
_Avoid_: 可随经验修改的认知实现

**MutableOrganism**：允许依据证据改变认知实现、策略、参数和受控数据的可变生命层。
_Avoid_: LifeKernel、评估裁判

**EvaluationHarness**：独立、可重复、版本封存的评估环境，用来判断候选版本是否通过硬门并产生真实改善。
_Avoid_: 生命体自己的自评、一次线上成功

**Environment**：生命体之外的运行时、网络、模型、工具、宿主资源和外部系统；生命体只能通过受控适配器与其交互。
_Avoid_: 本体、无边界权限

**bounded survival**：在声明的时间、计算、存储、网络和行动风险预算内维持核心不变量，并在压力或故障下休眠、降级、恢复或安全终止。
_Avoid_: 永生、无限运行

## 动机与迭代

**ImpulseEvent**：一次带有类型、烈度、来源、上下文和时间的动机或情感事件记录。
_Avoid_: 直接命令、成功证据

**MotivationalPressure**：由冲动频率、烈度、持续性、未解决程度和跨情境复现累积而成、随时间衰减的动机压力。
_Avoid_: 单次情绪峰值、总分

**IterationNeed**：由持久缺口、事故或有预算机会支持的“需要改变”的判定。
_Avoid_: 一时冲动、已获批准的变更

**ChangeProposal**：说明证据、假设、预期收益、风险、资源预算和回退版本的自我改变提案。
_Avoid_: 直接写入、愿望清单

**CandidateRevision**：在隔离环境中待评估的代码、策略或数据版本。
_Avoid_: 正在运行的版本、未经测试的热替换

**BaselineRevision**：最近一次通过硬门并被登记为可信参照的版本。
_Avoid_: 任意旧提交、候选版本

**Progress**：相对于 BaselineRevision，在指定目的维度上取得可重复的实质改善，同时没有关键维度回归。
_Avoid_: 活动更多、叙述更自信、单一指标变好

## 连续性与继承

**AnchorSet**：可追溯、已验证、按可信等级分类的身份、目的、生命史和能力锚点集合。
_Avoid_: 父实例临时挑选的“好记忆”

**Recovery**：同一 instance 回到最后可信 BaselineRevision 的修复过程。
_Avoid_: 新一代、无记录重启

**Succession**：当前 instance 无法恢复可信状态时，由同一 lineage 的新 generation 接替，并按 AnchorSet 继承经过筛选的内容。
_Avoid_: 无限制繁殖、原实例复活

**SuccessionRecord**：记录事故、冻结、继承断点、未继承内容和新旧 instance 关系的生命史记录。
_Avoid_: 普通日志、可删除的备注

## 生命周期状态

**CREATED**：已登记但尚未开始生命循环的个体。

**BOOTSTRAPPING**：正在装载身份、锚点和运行条件的初始化阶段。

**ACTIVE**：可以感知、思考、形成目标并在授权范围内行动的状态。

**SLEEPING**：仍保持生命连续性，但主动计算和环境交互被降低的状态。

**DEGRADED**：核心身份仍可信，但部分能力因资源或故障被主动限制的状态。

**QUARANTINED**：证据保全状态；禁止自修改和外部写入。

**RECOVERING**：由 LifeKernel 驱动的回滚、修复或候选恢复状态。

**SUCCESSION_PENDING**：已判定当前 instance 不可信，正在准备新 generation 的接替状态。

**RETIRED**：有序结束并封存的 instance；不再接受普通唤醒。

**DEAD**：不可逆的完整性终态；普通启动路径不能复活。
