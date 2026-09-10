"""Brain Stem — 脑干。意识主循环引擎，永不停止。

职责:
  1. 驱动每个 tick 的脑区管道
  2. 分发输入到丘脑 → 协调各脑区依次运转
  3. 管理觉醒状态
  4. 统计和健康监控

每 tick 流程:
  脑干 → 丘脑(输入过滤) → 杏仁核(情绪) → 前额叶(决策) 
  → 海马体(检索+编码) → 默认模式(内在独白) 
  → 基底节(习惯) → 扣带回(冲突监控) → 工作记忆更新
"""

import asyncio
from collections.abc import Iterable
from dataclasses import replace
import inspect
import hashlib
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from brain.thalamus import Thalamus
from brain.amygdala import Amygdala
from brain.prefrontal import Prefrontal
from brain.hippocampus import Hippocampus
from brain.default_mode import DefaultModeNetwork
from brain.basal_ganglia import BasalGanglia
from brain.cingulate import Cingulate
from brain.dream import DreamEngine
from brain.pipeline import gate_check, run_consolidation
from brain.working_memory import WorkingMemory
from brain.brain_state import BrainState, SNAPSHOT_SCHEMA_VERSION
from brain.intent import Intent, IntentQueue, IntentType
from brain.goal_system import GoalSystem, GoalStatus
from brain.task_scheduler import LongTermTaskScheduler, TaskTier
from brain.metacognition import Metacognition
from brain.emotional_spectrum import EmotionalSpectrum
from brain.procedural_memory import ProceduralMemory
from brain.time_sense import TimeSense
from brain.drive_engine import DriveEngine, GoalGenerator, GoalScheduler, build_state_snapshot_for_drive_engine  # V7
from brain.exploration import ExplorationQueue, ExplorationExecutor  # V8
from brain.reflection_engine import ReflectionEngine  # V8
from brain.predictive_layer import PredictiveLayer  # V9
from brain.cognitive_dispatch import CognitiveDispatch  # V9
from brain.boredom import BoredomEngine  # V9
from brain.social_self import SocialEmotionEngine, AttachmentSystem  # V10
from brain.reward_system import RewardSystem  # V10
from brain.autobiographical import AutobiographicalNarrative  # V10
from brain.boundary import BoundaryEngine  # V10
from brain.autonomy import AutonomyEpisode, EpisodeStatus  # V11
from brain.task_execution import (
    TaskExecutionLedger,
    PlanStatus,
    StepStatus,
    ActionStatus,
    OutcomeQuality,
)
from brain.learning_feedback import VerifiedLearningFeedback
from brain.evaluation_harness import (
    BaselineRevision,
    CandidateRevision,
    EvaluationHarness,
    EvaluationMode,
    EvaluationReceipt,
)
from brain.evolution import (
    PromotionController,
    PromotionMode,
    PromotionOutcome,
    SandboxAttestation,
)
from brain.life_kernel import (
    AppendOnlyLedger,
    IdentityCore,
    LifeKernel,
    LifeIdentity,
    LedgerIntegrityError,
    LifecycleError,
    LifecycleEvent,
    LifecycleState,
)
from brain.homeostasis import (
    ActionDecision,
    ActionRequest,
    ControlledEnvironment,
    EnvironmentKind,
    HomeostasisAction,
    HomeostasisController,
    ResourceBudget as HomeostasisBudget,
    ResourceObservation,
)
from brain.motivation import (
    ChangeProposal,
    ImpulseEvent,
    IterationNeed,
    MotivationSourceAttestor,
    MotivationalPressure,
)
from brain.succession import (
    AnchorSet,
    AnchorVault,
    FailureAssessment,
    SuccessionLedger,
)
from brain.succession_runtime import (
    SuccessionCoordinator,
    SuccessionOutcome,
    SuccessionRuntimeError,
    SuccessorActivationAttestation,
    SuccessorActivationAttestor,
)
from config import (
    TICK_INTERVAL_SEC,
    COGNITIVE_TIMEOUT_SEC,
    MEMORY_DECAY_RATE,
    MEMORY_DECAY_INTERVAL_TICKS,
    MEMORY_ARCHIVE_THRESHOLD,
    DROWSY_THRESHOLD_TICKS,
    LIGHT_SLEEP_THRESHOLD_TICKS,
    DEEP_SLEEP_THRESHOLD_TICKS,
    DREAM_INTERVAL_SEC,
    CONSOLIDATION_INTERVAL_SEC,
    REFLECTION_INTERVAL_SEC,
    STATE_SNAPSHOT_INTERVAL_SEC,
    GATE_GOAL_RELEVANCE_DEFAULT,
    GATE_GOAL_RELEVANCE_WITH_GOAL,
    DEEP_REFLECTION_INTERVAL_TICKS,
    DEEP_REFLECTION_ENABLED,
    DREAM_ENABLED,
    LLM_EMOTION_BLEND_RATIO,
    PREDICTIVE_LAYER_ENABLED,
    COGNITIVE_DISPATCH_ENABLED,
    BOREDOM_ENABLED,
    SOCIAL_SELF_ENABLED,
    REWARD_SYSTEM_ENABLED,
    AUTOBIO_ENABLED,
    BOUNDARY_ENABLED,
    AUTONOMY_ENABLED,
    AUTONOMY_GOAL_INTERVAL_TICKS,
    AUTONOMY_MAX_EPISODE_TICKS,
    TASK_QUEUE_LIMIT,
    TASK_MAINTENANCE_BUDGET_TICKS,
    TASK_USER_BUDGET_TICKS,
    TASK_EXPLORATION_BUDGET_TICKS,
    TASK_MAINTENANCE_DEADLINE_TICKS,
    TASK_USER_DEADLINE_TICKS,
    TASK_EXPLORATION_DEADLINE_TICKS,
    TASK_EXECUTION_ENABLED,
    TASK_EXECUTION_MAX_PLANS,
    TASK_EXECUTION_MAX_STEPS,
    TASK_EXECUTION_MAX_DEPTH,
    TASK_EXECUTION_MAX_RETRIES,
    TASK_EXECUTION_MAX_ACTIONS,
    TASK_EXECUTION_MAX_OBSERVATIONS,
    TASK_EXECUTION_MAX_OUTCOMES,
    TASK_EXECUTION_MAX_EVENTS,
    TASK_EXECUTION_AUTO_REPLAN,
    TASK_EXECUTION_REQUIRE_VERIFIED_COMPLETION,
)

logger = logging.getLogger("brain-v5.brain-stem")


class BrainStem:
    """Consciousness loop engine — the brain's heartbeat."""

    # Candidate records are observability/evidence state, not a work queue.
    # Keeping the limit here prevents a long-running subject from turning an
    # otherwise safe proposal seam into an unbounded memory sink.
    _ITERATION_RECORD_LIMIT = 32
    _ITERATION_SENSITIVE_KEYS = frozenset(
        {
            "api_key",
            "apikey",
            "access_token",
            "refresh_token",
            "password",
            "passwd",
            "secret",
            "authorization",
            "cookie",
            "credential",
            "private_key",
            "host_capability",
            "attestation_secret",
            "path",
            "cwd",
            "working_directory",
            "source_path",
            "candidate_path",
            "url",
        }
    )
    _ITERATION_ABSOLUTE_PATH_RE = re.compile(
        r"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/]|/(?:Users|home|root|tmp|var|etc|mnt|opt|srv)(?:[\\/]|$))",
        re.IGNORECASE,
    )
    _ITERATION_SECRET_VALUE_RE = re.compile(
        r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization|bearer|cookie|credential|private[_-]?key)\s*[:=]"
        r"|\bgh[pousr]_[A-Za-z0-9_]{12,}\b"
        r"|\bsk-[A-Za-z0-9_-]{16,}\b"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        state_store=None,
        memory_store=None,
        *,
        life_kernel=None,
        homeostasis=None,
        motivation=None,
        motivation_profile: str = "production",
        motivation_source_attestor=None,
        motivation_source_verifier=None,
        motivation_require_source_attestation: bool | None = None,
        controlled_environment=None,
        succession_coordinator=None,
        anchor_vault=None,
        succession_ledger=None,
        succession_profile: str = "production",
        succession_activation_attestor=None,
        life_control_mode: str = "durable",
        evaluation_harness: EvaluationHarness | None = None,
        promotion_controller: PromotionController | None = None,
    ):
        # Brain regions
        self.thalamus = Thalamus()
        self.amygdala = Amygdala()
        self.prefrontal = Prefrontal()
        self.hippocampus = Hippocampus(memory_store=memory_store)
        self.default_mode = DefaultModeNetwork()
        self.basal_ganglia = BasalGanglia()
        self.cingulate = Cingulate()
        self.working_memory = WorkingMemory()
        self.dream_engine = DreamEngine()

        # Sleep state
        self.sleep_state = "awake"  # awake / drowsy / light_sleep / deep_sleep
        self.last_dream_time = 0.0
        self.last_consolidation_time = 0.0

        # State
        self.state = BrainState()
        self.state_store = state_store
        self.memory_store = memory_store
        control_mode = str(life_control_mode or "durable").strip().lower()
        control_mode = {
            "strict": "durable",
            "fenced": "durable",
            "compatibility": "legacy",
            "legacy": "legacy",
        }.get(control_mode, control_mode)
        if control_mode not in {"durable", "legacy"}:
            raise ValueError("life_control_mode must be 'durable' or explicit 'legacy'")
        # Durable is the default.  Legacy is an explicit host choice for old
        # append-only adapters and carries no cross-process single-owner claim.
        self.life_control_mode = control_mode

        # Constitutional lifecycle seam.  The kernel is deliberately kept
        # outside ``BrainState``: cognition may evolve, while identity,
        # lifecycle edges, and their append-only audit remain independently
        # verifiable.  Persistence is attached lazily at the first explicit
        # snapshot/start so constructing a stem does not create a second
        # lineage before an existing snapshot has been inspected.
        if life_kernel is not None and not isinstance(life_kernel, LifeKernel):
            raise TypeError("life_kernel must be a LifeKernel")
        if succession_coordinator is not None and not isinstance(
            succession_coordinator, SuccessionCoordinator
        ):
            raise TypeError("succession_coordinator must be a SuccessionCoordinator")
        if succession_activation_attestor is not None and not isinstance(
            succession_activation_attestor, SuccessorActivationAttestor
        ):
            raise TypeError(
                "succession_activation_attestor must be a SuccessorActivationAttestor"
            )
        if succession_coordinator is not None:
            coordinator_parent = succession_coordinator.parent
            if life_kernel is not None and life_kernel is not coordinator_parent:
                if (
                    life_kernel.lineage_id != coordinator_parent.lineage_id
                    or life_kernel.instance_id != coordinator_parent.instance_id
                    or life_kernel.ledger.last_hash != coordinator_parent.ledger.last_hash
                ):
                    raise ValueError("life_kernel does not match succession coordinator parent")
            life_kernel = coordinator_parent
        self.life_kernel = life_kernel or LifeKernel()
        self._life_sink_attached = False
        self._life_runtime_started = False
        self._life_restore_blocked = False
        self._life_restore_error = ""
        self._life_legacy_bootstrap = True
        # A persistent host gets a short-lived, fenced SQLite control lease.
        # The token never enters BrainState or a lifecycle event; it remains
        # in this process and is renewed while the heartbeat is alive.  An
        # in-memory stem keeps the older process-local coordinator guard.
        self._life_control_lease = None
        self._life_control_owner_id = f"brainstem-{uuid.uuid4().hex}"
        self._life_local_control_token = object()
        self._life_control_ttl_sec = 120.0
        self._life_control_renew_interval_sec = 30.0
        self._life_control_next_renew_at = 0.0
        self._life_control_lost = False
        # Process-local guard ownership is tracked separately from the
        # durable SQLite lease.  This prevents a second stem that shares the
        # same LifeKernel object from releasing the first stem's registry
        # entry during its own failed start/stop path.
        self._life_local_control_kernel = None
        # During an explicit succession handover the child genesis event is
        # durable evidence but the CREATED child must not claim live control
        # before independent activation.  This narrow context permits that
        # one genesis append without weakening ordinary lifecycle writes.
        self._life_handover_lineage = ""
        self._life_handover_parent_generation = None
        self._life_replay_mode = False

        # Mutable-organism safety seams.  Neither manager owns identity or
        # executes an external action: motivation may only request an
        # evaluation, and homeostasis may only tighten lifecycle/admission
        # policy.  The aliases keep the public domain names discoverable for
        # embedders without duplicating state.
        if homeostasis is not None and not isinstance(homeostasis, HomeostasisController):
            raise TypeError("homeostasis must be a HomeostasisController")
        if motivation is not None and not isinstance(motivation, MotivationalPressure):
            raise TypeError("motivation must be a MotivationalPressure")
        motivation_mode = str(motivation_profile or "production").strip().lower()
        motivation_mode = {
            "strict": "production",
            "offline": "legacy",
            "compatibility": "legacy",
        }.get(motivation_mode, motivation_mode)
        if motivation_mode not in {"production", "legacy"}:
            raise ValueError(
                "motivation_profile must be 'production' or explicit 'legacy'"
            )
        self.motivation_profile = motivation_mode
        if motivation is not None:
            # An injected accumulator is already a live policy object.  Do
            # not let separate constructor arguments describe a different
            # attestor/verifier/strictness than the object that will actually
            # admit events; that would run permissively until the first
            # restart and then silently restore under another policy.
            if (
                motivation_source_attestor is not None
                and getattr(motivation, "source_attestor", None)
                is not motivation_source_attestor
            ):
                raise ValueError(
                    "motivation source attestor does not match injected motivation"
                )
            if (
                motivation_source_verifier is not None
                and getattr(motivation, "source_verifier", None)
                is not motivation_source_verifier
            ):
                raise ValueError(
                    "motivation source verifier does not match injected motivation"
                )
            if (
                motivation_require_source_attestation is not None
                and bool(
                    getattr(motivation, "require_source_attestation", False)
                )
                != bool(motivation_require_source_attestation)
            ):
                raise ValueError(
                    "motivation attestation policy does not match injected motivation"
                )
            if (
                self.motivation_profile == "production"
                and not bool(
                    getattr(motivation, "require_source_attestation", False)
                )
            ):
                raise ValueError(
                    "permissive motivation requires explicit legacy profile"
                )
        if (
            self.motivation_profile == "production"
            and motivation_require_source_attestation is False
        ):
            raise ValueError(
                "production motivation cannot disable source attestation"
            )
        if (
            self.motivation_profile == "production"
            and motivation_source_verifier is not None
        ):
            raise ValueError(
                "arbitrary source_verifier requires explicit legacy profile"
            )
        if controlled_environment is not None and not isinstance(
            controlled_environment, ControlledEnvironment
        ):
            raise TypeError("controlled_environment must be a ControlledEnvironment")
        # ``HomeostasisController`` is intentionally allowed to be supplied
        # as an empty-but-configured object.  Do not use truthiness here:
        # future adapters may expose ``__len__`` and an empty ledger must not
        # be silently replaced with a fresh budget.
        self.homeostasis = (
            homeostasis
            if homeostasis is not None
            else HomeostasisController(HomeostasisBudget())
        )
        self.homeostasis_controller = self.homeostasis
        self._motivation_source_attestor = (
            motivation_source_attestor
            if motivation_source_attestor is not None
            else getattr(motivation, "source_attestor", None)
        )
        self._motivation_source_verifier = (
            motivation_source_verifier
            if motivation_source_verifier is not None
            else getattr(motivation, "source_verifier", None)
        )
        if motivation is not None:
            self._motivation_require_source_attestation = bool(
                getattr(motivation, "require_source_attestation", False)
            )
        elif motivation_require_source_attestation is not None:
            self._motivation_require_source_attestation = bool(
                motivation_require_source_attestation
            )
        else:
            self._motivation_require_source_attestation = bool(
                self.motivation_profile == "production"
                or self._motivation_source_attestor is not None
            )
        self.motivation = motivation if motivation is not None else MotivationalPressure(
            source_attestor=self._motivation_source_attestor,
            source_verifier=self._motivation_source_verifier,
            require_source_attestation=self._motivation_require_source_attestation,
        )
        self.motivational_pressure = self.motivation
        self.controlled_environment = controlled_environment
        self._last_iteration_need: IterationNeed | None = None

        # Succession state is kept outside BrainState for the same reason as
        # the constitutional kernel: mutable cognition must not be able to
        # rewrite the parent/child boundary.  Empty injected containers are
        # retained by identity; callers may use them as durable projections.
        if anchor_vault is not None and not isinstance(anchor_vault, AnchorVault):
            raise TypeError("anchor_vault must be an AnchorVault")
        if succession_ledger is not None and not isinstance(
            succession_ledger, SuccessionLedger
        ):
            raise TypeError("succession_ledger must be a SuccessionLedger")
        if succession_coordinator is not None:
            if anchor_vault is not None and anchor_vault is not succession_coordinator.anchor_vault:
                raise ValueError("anchor_vault does not match succession coordinator")
            if succession_ledger is not None and succession_ledger is not succession_coordinator.succession_ledger:
                raise ValueError("succession_ledger does not match succession coordinator")
            self.anchor_vault = succession_coordinator.anchor_vault
            self.succession_ledger = succession_coordinator.succession_ledger
        else:
            self.anchor_vault = anchor_vault if anchor_vault is not None else AnchorVault()
            self.succession_ledger = (
                succession_ledger
                if succession_ledger is not None
                else SuccessionLedger()
            )
        self.succession_coordinator = succession_coordinator
        if succession_coordinator is not None:
            # An injected coordinator is already the policy authority.  Keep
            # its profile/attestor as the single source of truth and reject a
            # conflicting host binding rather than silently weakening it.
            if succession_activation_attestor is not None and (
                succession_coordinator.activation_attestor
                is not succession_activation_attestor
            ):
                raise ValueError(
                    "succession activation attestor does not match coordinator"
                )
            self.succession_profile = succession_coordinator.activation_profile
            self.succession_activation_attestor = (
                succession_coordinator.activation_attestor
            )
        else:
            self.succession_profile = str(succession_profile or "production")
            self.succession_activation_attestor = succession_activation_attestor
        self._successor_activation_required = bool(
            succession_coordinator is not None
            and succession_coordinator.successor is not None
            and succession_coordinator.successor.state == LifecycleState.CREATED
        )
        self._successor_evaluation_receipt_hash = ""

        # P3 candidate-iteration boundary.  These dependencies are host
        # capabilities and are therefore never auto-created from the live
        # checkout.  Motivation can suggest a proposal, but only an explicit
        # host call may evaluate it, and only a second explicit host call may
        # ask the controller to promote it.  The controller itself remains the
        # authority for production sandbox attestation and active-tree writes.
        if evaluation_harness is not None and not isinstance(
            evaluation_harness, EvaluationHarness
        ):
            raise TypeError("evaluation_harness must be an EvaluationHarness")
        if promotion_controller is not None and not isinstance(
            promotion_controller, PromotionController
        ):
            raise TypeError("promotion_controller must be a PromotionController")
        controller_harness = (
            getattr(promotion_controller, "harness", None)
            if promotion_controller is not None
            else None
        )
        if (
            evaluation_harness is not None
            and controller_harness is not None
            and controller_harness is not evaluation_harness
        ):
            raise ValueError(
                "evaluation_harness must be the same instance bound to promotion_controller"
            )
        # If the host supplied only a controller, its fixed harness is the
        # evaluator for this seam.  No controller or harness is synthesized.
        self.evaluation_harness = (
            evaluation_harness
            if evaluation_harness is not None
            else controller_harness
        )
        self.promotion_controller = promotion_controller
        # Readable aliases for embedders that use evaluator/evolution terms.
        self.evaluator = self.evaluation_harness
        self.evolution_controller = self.promotion_controller
        self._assert_production_motivation_policy()
        if self.promotion_controller is not None and self.state_store is not None:
            # A database path can be created lazily by SQLite.  Bind it now so
            # a not-yet-created ``.sqlite`` file cannot later appear inside the
            # atomically swapped runtime tree.  Adapters without ``db_path``
            # retain compatibility but remain an explicitly unverified host
            # boundary (the deployment layer must supply its own guard).
            persistence_path = getattr(self.state_store, "db_path", None)
            if persistence_path is not None:
                self.promotion_controller.bind_persistence_path(persistence_path)
        self._iteration_proposals: dict[str, Any] = {}
        self._iteration_candidate_bindings: dict[str, dict[str, str]] = {}
        self._iteration_receipts: dict[str, EvaluationReceipt] = {}
        self._iteration_outcomes: dict[str, PromotionOutcome] = {}
        self._iteration_restore_rejected = 0

        # Loop control
        self._stop_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = None
        self._task: asyncio.Task | None = None
        self._has_started = False
        # Inputs may be buffered before the first ``start()`` (useful for
        # embedded callers), but once a running instance is stopped we reject
        # new input until the next explicit start.  This prevents a shutdown
        # race from leaving work that unexpectedly executes after restart.
        self._accepting_input = True
        self._pending_input: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._input_processed = asyncio.Event()
        self._input_waiters: dict[str, asyncio.Future] = {}
        self._input_sources: dict[str, str] = {}
        self._active_input_id: str | None = None
        self._restored_uptime_seconds: float = 0.0
        self.cognitive_timeout_sec = max(1, min(120, int(COGNITIVE_TIMEOUT_SEC)))
        # A private capability shared with the in-process AgentBridge.  Tool
        # observations are evidence, not ordinary user input; callers that
        # can reach ``receive_input`` directly must not be able to forge a
        # verified result by supplying a look-alike dictionary.
        self._tool_observation_capability = object()

        # Output feed — external agents consume this
        self._output_feed: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Intent queue — brain produces intents, agent layer consumes (v5.0)
        self.intent_queue: IntentQueue = IntentQueue()

        # Goal system — v5.1: brain sets its own goals
        self.goal_system = GoalSystem()
        # Long-term policy — one execution lane, bounded tiered queue.
        self.task_scheduler = LongTermTaskScheduler(
            max_queue=TASK_QUEUE_LIMIT,
            default_budgets={
                TaskTier.MAINTENANCE: TASK_MAINTENANCE_BUDGET_TICKS,
                TaskTier.USER: TASK_USER_BUDGET_TICKS,
                TaskTier.EXPLORATION: TASK_EXPLORATION_BUDGET_TICKS,
            },
            default_deadlines={
                TaskTier.MAINTENANCE: TASK_MAINTENANCE_DEADLINE_TICKS,
                TaskTier.USER: TASK_USER_DEADLINE_TICKS,
                TaskTier.EXPLORATION: TASK_EXPLORATION_DEADLINE_TICKS,
            },
        )

        # Metacognition — v5.2: brain monitors its own thinking
        self.metacognition = Metacognition()

        # Emotional Spectrum — v5.3: continuous emotion instead of discrete labels
        self.emotional_spectrum = EmotionalSpectrum()

        # Procedural Memory — v5.4: learns skills from repeated success
        self.procedural_memory = ProceduralMemory()

        # Time Sense — v5.4: internal clock, rhythm, temporal awareness
        self.time_sense = TimeSense()

        # Drive Engine — V7: dynamic drive system
        self.drive_engine = DriveEngine()
        self.goal_generator = GoalGenerator()
        self.goal_scheduler = GoalScheduler()
        self._last_archived_count: int = 0  # tracked for stagnation detection

        # V8: exploration + reflection
        self.exploration_queue = ExplorationQueue()
        self.exploration_executor = ExplorationExecutor()
        self.reflection_engine = ReflectionEngine()

        # V9: predictive processing + cognitive dispatch + boredom
        self.predictive_layer = PredictiveLayer() if PREDICTIVE_LAYER_ENABLED else None
        self.cognitive_dispatch = CognitiveDispatch() if COGNITIVE_DISPATCH_ENABLED else None
        self.boredom_engine = BoredomEngine() if BOREDOM_ENABLED else None

        # V10: social self + reward + autobiography + boundary
        self.social_emotion = SocialEmotionEngine() if SOCIAL_SELF_ENABLED else None
        self.attachment_system = AttachmentSystem() if SOCIAL_SELF_ENABLED else None
        self.reward_system = RewardSystem() if REWARD_SYSTEM_ENABLED else None
        self.autobiography = AutobiographicalNarrative() if AUTOBIO_ENABLED else None
        self.boundary = BoundaryEngine() if BOUNDARY_ENABLED else None

        # V11: a bounded causal record for internally generated action cycles.
        # The manager never executes a tool; it only coordinates state,
        # feedback and recovery across BrainStem and AgentBridge.
        self.autonomy = (
            AutonomyEpisode(max_ticks=AUTONOMY_MAX_EPISODE_TICKS)
            if AUTONOMY_ENABLED else None
        )

        # V13: a separate causal ledger for plans, actions, observations and
        # deterministic outcomes.  It never executes tools; AgentBridge stays
        # the only execution boundary.
        self.task_execution_enabled = bool(TASK_EXECUTION_ENABLED)
        self.task_execution = (
            TaskExecutionLedger(
                max_plans=TASK_EXECUTION_MAX_PLANS,
                max_steps_per_plan=TASK_EXECUTION_MAX_STEPS,
                max_depth=TASK_EXECUTION_MAX_DEPTH,
                default_max_retries=TASK_EXECUTION_MAX_RETRIES,
                max_actions=TASK_EXECUTION_MAX_ACTIONS,
                max_observations=TASK_EXECUTION_MAX_OBSERVATIONS,
                max_outcomes=TASK_EXECUTION_MAX_OUTCOMES,
                max_events=TASK_EXECUTION_MAX_EVENTS,
            )
            if self.task_execution_enabled else None
        )
        if self.task_execution is not None:
            # Keep the operator's explicit policy visible without granting any
            # write capability to the ledger itself.
            self.task_execution.require_verified_completion = bool(
                TASK_EXECUTION_REQUIRE_VERIFIED_COMPLETION
            )
            self.task_execution.auto_replan = bool(TASK_EXECUTION_AUTO_REPLAN)
        self.learning_feedback = (
            VerifiedLearningFeedback() if self.task_execution_enabled else None
        )
        self._last_execution_outcome = None

        # Stats
        self.start_time = datetime.now(timezone.utc)
        self.state.total_ticks = 0
        self.last_reflection = 0.0
        self.last_snapshot = 0.0
        self.last_decay = 0.0
        if self.autonomy:
            self.state.autonomy = self.autonomy.summary()
        self._sync_life_projection()
        self._sync_self_maintenance_projection()

    # ── Constitutional life seam (P1) ──

    def _sync_life_projection(self) -> None:
        """Project the immutable kernel into the serializable brain state."""
        try:
            projection = dict(self.life_kernel.public_snapshot())
        except Exception as exc:  # pragma: no cover - defensive boundary
            projection = {
                "schema_version": 1,
                "lifecycle_state": "UNKNOWN",
                "accepts_input": False,
                "allows_self_modification": False,
            }
            self._life_restore_error = str(exc)[:300]
        if self._life_restore_blocked:
            projection["restore_blocked"] = True
            projection["restore_error"] = self._life_restore_error[:300]
        # A resource quarantine can only narrow the kernel permission.  It
        # never grants self-modification when the lifecycle would deny it.
        homeostasis = getattr(self, "homeostasis", None)
        if homeostasis is not None and homeostasis.quarantine_latched:
            projection["accepts_input"] = False
            projection["allows_self_modification"] = False
            projection["homeostasis_quarantine"] = True
        coordinator = getattr(self, "succession_coordinator", None)
        if coordinator is not None:
            projection["succession_active_instance_id"] = coordinator.active_instance_id
            projection["successor_activation_required"] = bool(
                getattr(self, "_successor_activation_required", False)
            )
        lease = getattr(self, "_life_control_lease", None)
        if lease is not None:
            try:
                # ``LifeControlLease.to_dict`` intentionally omits the token
                # unless explicitly requested.  Keep only non-capability
                # diagnostics in the public state projection.
                projection["control_lease"] = lease.to_dict()
                projection["control_lease_bound"] = True
            except Exception:
                projection["control_lease_bound"] = False
        else:
            projection["control_lease_bound"] = False
        projection["life_control_mode"] = getattr(self, "life_control_mode", "durable")
        if getattr(self, "_life_control_lost", False):
            projection["control_lease_lost"] = True
        self.state.life = projection

    def _sync_self_maintenance_projection(self) -> None:
        """Expose bounded motivation/resource summaries through BrainState."""
        try:
            self.state.homeostasis = dict(self.homeostasis.public_snapshot())
        except Exception as exc:  # pragma: no cover - defensive projection
            self.state.homeostasis = {
                "quarantine_latched": True,
                "projection_error": str(exc)[:200],
            }
        try:
            pressures = self.motivation.get_pressure()
            if not isinstance(pressures, dict):
                pressures = {}
            latest_need = (
                self._last_iteration_need.to_dict()
                if self._last_iteration_need is not None
                else None
            )
            self.state.motivation = {
                "schema_version": 1,
                "pressures": {
                    self._safe_motivation_label(key): float(value)
                    for key, value in list(pressures.items())[:128]
                },
                "total_received": int(self.motivation.total_received),
                "total_accepted": int(self.motivation.total_accepted),
                "total_rejected": int(self.motivation.total_rejected),
                "total_self_rejected": int(self.motivation.total_self_rejected),
                "total_needs_emitted": int(self.motivation.total_needs_emitted),
                "source_attestation_required": bool(
                    getattr(self.motivation, "require_source_attestation", False)
                ),
                "profile": self.motivation_profile,
                "source_attestor_bound": bool(
                    getattr(self.motivation, "source_attestor", None) is not None
                    or getattr(self.motivation, "source_verifier", None) is not None
                ),
                "source_replay_durable": bool(
                    getattr(
                        getattr(self.motivation, "source_attestor", None),
                        "durable_replay_enabled",
                        False,
                    )
                ),
                "snapshot_rejected": bool(
                    getattr(self.motivation, "snapshot_rejected", False)
                ),
                "last_iteration_need": latest_need,
                # An IterationNeed is evidence for evaluation, never an
                # authorization to write or execute a change.
                "self_modification_authorized": False,
                # P3 is deliberately a read-only projection.  Host bindings,
                # attestation secrets and candidate paths never cross this
                # state boundary.
                "iteration": self._iteration_snapshot(),
            }
        except Exception as exc:  # pragma: no cover - defensive projection
            self.state.motivation = {
                "schema_version": 1,
                "self_modification_authorized": False,
                "projection_error": str(exc)[:200],
            }
        self._sync_life_projection()

    @staticmethod
    def _safe_motivation_label(value: Any) -> str:
        """Keep the compact state projection free of caller-supplied prose."""

        try:
            raw = str(value).replace("\x00", "").strip()[:80]
            text = raw.lower()
        except Exception:
            raw = ""
            text = ""
        if not text:
            return "unknown"
        if (
            raw == text
            and re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,79}", text)
            and not re.search(
                r"(?:raw|user|payload|private|api[_-]?key|token|password|secret|"
                r"credential|path|root|cwd|url)",
                text,
                re.IGNORECASE,
            )
        ):
            return text
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"<redacted:motive:{digest}>"

    def _assert_production_motivation_policy(self) -> None:
        """Keep permissive motivation out of the production P3 seam."""

        controller = getattr(self, "promotion_controller", None)
        production_controller = bool(
            controller is not None
            and getattr(controller, "profile", "") == "production"
        )
        if (
            getattr(self, "motivation_profile", "production") != "production"
            and not production_controller
        ):
            return
        motivation = getattr(self, "motivation", None)
        if not isinstance(motivation, MotivationalPressure):
            raise PermissionError("production iteration requires MotivationalPressure")
        if not bool(getattr(motivation, "require_source_attestation", False)):
            raise PermissionError(
                "production iteration cannot use permissive motivation policy"
            )
        attestor = getattr(motivation, "source_attestor", None)
        verifier = getattr(motivation, "source_verifier", None)
        if verifier is not None or (
            attestor is not None and not isinstance(attestor, MotivationSourceAttestor)
        ):
            raise PermissionError(
                "production iteration requires a MotivationSourceAttestor"
            )
        if production_controller and attestor is not None and not bool(
            getattr(attestor, "durable_replay_enabled", False)
        ):
            raise PermissionError(
                "production iteration requires durable source-attestation replay protection"
            )
        if production_controller and attestor is not None and (
            getattr(attestor, "replay_store", None)
            is not getattr(self, "state_store", None)
        ):
            raise PermissionError(
                "production source attestation must use the BrainStem StateStore replay ledger"
            )

    def record_impulse(
        self,
        impulse: ImpulseEvent | dict[str, Any] | str,
        intensity: float | None = None,
        source: str = "runtime",
        context: str = "",
        **kwargs: Any,
    ) -> float:
        """Record a motivational observation without granting change rights."""
        self._assert_production_motivation_policy()
        now = kwargs.pop("now", None)
        if isinstance(impulse, ImpulseEvent):
            event = impulse
        elif isinstance(impulse, dict):
            event = ImpulseEvent.from_dict(impulse)
            if event is None:
                raise ValueError("invalid impulse event")
        else:
            event = ImpulseEvent.create(
                impulse_type=str(impulse),
                intensity=0.0 if intensity is None else intensity,
                source=source,
                context=context,
                **kwargs,
            )
        pressure = self.motivation.record(event, now=now)
        self._sync_self_maintenance_projection()
        return pressure

    observe_impulse = record_impulse

    def poll_iteration_need(
        self,
        motive: str | None = None,
        *,
        now: Any = None,
    ) -> IterationNeed | None:
        """Return a bounded need record; never mutate source or authorize it."""
        self._assert_production_motivation_policy()
        need = self.motivation.poll_iteration_need(motive=motive, now=now)
        if need is not None:
            self._last_iteration_need = need
        self._sync_self_maintenance_projection()
        return need

    # ── Explicit candidate iteration seam (P3) ──

    @staticmethod
    def _require_iteration_host(host: Any) -> Any:
        """Require a host capability on every candidate-boundary call.

        The stem deliberately does not retain a host object and never infers
        one from ``state_store``, ``controlled_environment`` or the current
        process.  A caller must therefore pass the host explicitly for each
        registration, evaluation and promotion operation.  The host may be a
        mapping/object understood by :class:`PromotionController`; for the
        proposal/evaluation phases it need not carry authorization.
        """

        if host is None:
            raise PermissionError("an explicit host binding is required")
        return host

    @staticmethod
    def _coerce_iteration_need(need: IterationNeed | Mapping[str, Any]) -> IterationNeed:
        if isinstance(need, IterationNeed):
            return need
        if isinstance(need, Mapping):
            parsed = IterationNeed.from_dict(need)
            if parsed is not None:
                return parsed
        raise TypeError("need must be an IterationNeed or mapping")

    @staticmethod
    def _coerce_candidate_revision(
        candidate: str | CandidateRevision,
    ) -> CandidateRevision:
        if isinstance(candidate, CandidateRevision):
            return candidate
        return CandidateRevision.from_path(candidate)

    @staticmethod
    def _coerce_iteration_proposal(
        proposal: ChangeProposal | Mapping[str, Any],
    ) -> ChangeProposal:
        if isinstance(proposal, ChangeProposal):
            return proposal
        if isinstance(proposal, Mapping):
            parsed = ChangeProposal.from_dict(proposal)
            if parsed is not None:
                return parsed
        raise TypeError("proposal must be a ChangeProposal or mapping")

    def _remember_iteration_record(self, proposal: ChangeProposal) -> None:
        """Remember one proposal and evict the oldest related evidence."""

        proposal_id = proposal.proposal_id
        self._iteration_proposals[proposal_id] = proposal
        while len(self._iteration_proposals) > self._ITERATION_RECORD_LIMIT:
            oldest_id = next(iter(self._iteration_proposals))
            self._iteration_proposals.pop(oldest_id, None)
            self._iteration_candidate_bindings.pop(oldest_id, None)
            self._iteration_receipts.pop(oldest_id, None)
            self._iteration_outcomes.pop(oldest_id, None)

    def _require_registered_iteration_proposal(
        self,
        proposal: ChangeProposal | Mapping[str, Any],
    ) -> ChangeProposal:
        parsed = self._coerce_iteration_proposal(proposal)
        registered = self._iteration_proposals.get(parsed.proposal_id)
        if registered is None:
            raise ValueError("proposal must be registered through register_iteration_proposal")
        # A mapping can be reconstituted safely, but it must denote exactly the
        # immutable record that was registered.  This prevents a caller from
        # swapping the need/evidence while reusing a receipt or candidate id.
        if registered.to_dict() != parsed.to_dict():
            raise ValueError("proposal does not match the registered immutable record")
        return registered

    @classmethod
    def _iteration_redaction_marker(cls, value: Any, label: str) -> str:
        """Return a stable, non-reversible marker without retaining ``value``."""

        try:
            raw = str(value)
        except Exception:
            raw = "<unprintable>"
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"<redacted:{label}:{digest}>"

    @classmethod
    def _iteration_text_is_sensitive(cls, value: Any, *, key: str = "") -> bool:
        try:
            text = str(value)
        except Exception:
            return True
        lowered_key = key.casefold().replace("-", "_").replace(" ", "_")
        if any(marker in lowered_key for marker in cls._ITERATION_SENSITIVE_KEYS):
            return True
        if any(token in lowered_key for token in ("candidate", "revision")) and any(
            separator in text for separator in ("/", "\\", ":")
        ):
            return True
        if cls._ITERATION_ABSOLUTE_PATH_RE.search(text):
            return True
        # URLs can carry credentials/query tokens and are host capabilities,
        # not durable proposal evidence.
        if "://" in text or cls._ITERATION_SECRET_VALUE_RE.search(text):
            return True
        return False

    @classmethod
    def _safe_iteration_text(
        cls,
        value: Any,
        *,
        key: str = "",
        limit: int = 500,
        empty: str = "",
    ) -> str:
        try:
            text = "" if value is None else str(value)
        except Exception:
            text = ""
        text = text.replace("\x00", " ")[:limit]
        if not text:
            return empty
        if cls._iteration_text_is_sensitive(text, key=key):
            return cls._iteration_redaction_marker(text, key or "value")
        return text

    @classmethod
    def _safe_iteration_value(cls, value: Any, *, key: str = "", depth: int = 0) -> Any:
        """Bound and redact arbitrary metadata before it reaches a snapshot."""

        if depth > 3:
            return "<depth-limit>"
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else 0.0
        if isinstance(value, Mapping):
            bounded: dict[str, Any] = {}
            for raw_key, raw_value in list(value.items())[:24]:
                safe_key = cls._safe_iteration_text(raw_key, key="metadata-key", limit=80)
                if not safe_key:
                    continue
                if cls._iteration_text_is_sensitive(raw_key, key=str(raw_key)):
                    bounded[safe_key] = cls._iteration_redaction_marker(raw_value, safe_key)
                else:
                    bounded[safe_key] = cls._safe_iteration_value(
                        raw_value, key=str(raw_key), depth=depth + 1
                    )
            return bounded
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                cls._safe_iteration_value(item, key=key, depth=depth + 1)
                for item in list(value)[:24]
            ]
        return cls._safe_iteration_text(value, key=key, limit=500)

    @classmethod
    def _iteration_payload_contains_sensitive(
        cls, value: Any, *, key: str = "", depth: int = 0
    ) -> bool:
        """Check a receipt before persisting its hash-addressed full payload.

        An EvaluationReceipt cannot be field-redacted and remain verifiable.
        When any nested field contains a host path, URL, or secret-shaped
        value, the durable snapshot therefore keeps only a non-authorizing
        summary and receipt hash; restoration deliberately skips that receipt.
        """

        if depth > 5:
            return True
        if cls._iteration_text_is_sensitive(key, key=key):
            return True
        if isinstance(value, Mapping):
            return any(
                cls._iteration_payload_contains_sensitive(
                    raw_value, key=str(raw_key), depth=depth + 1
                )
                for raw_key, raw_value in list(value.items())[:128]
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(
                cls._iteration_payload_contains_sensitive(
                    item, key=key, depth=depth + 1
                )
                for item in list(value)[:256]
            )
        if isinstance(value, str):
            return cls._iteration_text_is_sensitive(value, key=key)
        return False

    @classmethod
    def _safe_iteration_receipt_summary(
        cls, receipt: EvaluationReceipt
    ) -> dict[str, Any]:
        """Return the capability-free subset safe for API and persistence."""

        raw = receipt.to_dict()
        return {
            "receipt_id": cls._safe_iteration_text(
                raw.get("receipt_id", ""), key="receipt_id", limit=160
            ),
            "receipt_hash": (
                str(raw.get("receipt_hash", "")).strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", str(raw.get("receipt_hash", "")).strip().lower())
                else ""
            ),
            "candidate_revision_id": cls._safe_iteration_text(
                raw.get("candidate_revision_id", ""),
                key="candidate_revision_id",
                limit=160,
            ),
            "candidate_fingerprint": (
                str(raw.get("candidate_fingerprint", "")).strip().lower()
                if re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(raw.get("candidate_fingerprint", "")).strip().lower(),
                )
                else ""
            ),
            "mode": cls._safe_iteration_text(
                raw.get("mode", ""), key="mode", limit=40
            ),
            "accepted": bool(raw.get("accepted", False)),
            "outcome": cls._safe_iteration_text(
                raw.get("outcome", ""), key="outcome", limit=120
            ),
            "isolated": bool(raw.get("isolated", False)),
            "result_verified": bool(raw.get("result_verified", False)),
        }

    @classmethod
    def _safe_iteration_receipt_envelope(
        cls, proposal_id: str, receipt: EvaluationReceipt
    ) -> dict[str, Any]:
        raw = receipt.to_dict()
        summary = cls._safe_iteration_receipt_summary(receipt)
        redacted = cls._iteration_payload_contains_sensitive(raw)
        return {
            "proposal_id": cls._safe_iteration_text(
                proposal_id, key="proposal_id", limit=100
            ),
            # Redacting fields would invalidate the evaluator's receipt hash.
            # Omit the full object when unsafe and retain only a transparent,
            # non-authorizing summary.  Such a receipt must be re-evaluated
            # after restart before promotion can proceed.
            "receipt": None if redacted else raw,
            "receipt_hash": summary["receipt_hash"],
            "summary": summary,
            "binding": {
                "revision_id": summary["candidate_revision_id"],
                "fingerprint": summary["candidate_fingerprint"],
            },
            "redacted": redacted,
        }

    @classmethod
    def _safe_iteration_proposal_dict(cls, proposal: ChangeProposal) -> dict[str, Any]:
        """Redact host paths/capabilities and secret-shaped values at persistence."""

        raw = proposal.to_dict()
        payload = dict(raw)
        for key, limit in (
            ("title", 180),
            ("scope", 180),
            ("hypothesis", 1000),
            ("created_at", 100),
            ("status", 40),
        ):
            payload[key] = cls._safe_iteration_text(payload.get(key, ""), key=key, limit=limit)
        for key in ("proposal_id", "need_id"):
            value = cls._safe_iteration_text(payload.get(key, ""), key=key, limit=120)
            payload[key] = value
        for key in ("evidence", "expected_benefits", "risks"):
            raw_items = payload.get(key, ())
            if not isinstance(raw_items, (list, tuple)):
                raw_items = (raw_items,)
            payload[key] = [
                cls._safe_iteration_text(item, key=key, limit=500)
                for item in list(raw_items)[:32]
            ]
        for key in ("baseline_revision", "rollback_revision"):
            payload[key] = cls._safe_iteration_text(
                payload.get(key, ""), key=key, limit=180
            )
        # Candidate revisions are rebound explicitly to a fresh host path after
        # restart.  Persisting even an opaque label here is unnecessary and can
        # accidentally preserve a source path supplied by an adapter.
        payload["candidate_revision"] = ""
        payload["resource_budget"] = cls._safe_iteration_value(
            payload.get("resource_budget", {}), key="resource_budget"
        )
        for key in (
            "requires_external_approval",
            "evaluation_required",
            "authorized",
            "is_authorized",
            "can_apply",
        ):
            payload[key] = bool(payload.get(key, False))
        return payload

    @staticmethod
    def _iteration_proposal_hash(payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_iteration_outcome_dict(outcome: PromotionOutcome) -> dict[str, Any]:
        """Keep outcome observability hash/identity based, not reason/path based."""

        raw = outcome.to_dict()
        return {
            key: raw.get(key)
            for key in (
                "schema_version",
                "accepted",
                "action",
                "mode",
                "candidate_revision_id",
                "candidate_fingerprint",
                "evaluation_receipt_hash",
                "active_before_fingerprint",
                "active_after_fingerprint",
                "rollback_available",
                "record_hash",
                "sandbox_attestation_id",
                "sandbox_attestation_hash",
                "attestation_verified",
            )
        }

    def _iteration_snapshot(self) -> dict[str, Any]:
        """Return a bounded, capability-free P3 observability projection."""

        controller = self.promotion_controller
        last_proposal = None
        if self._iteration_proposals:
            last_proposal = self._safe_iteration_proposal_dict(
                next(reversed(self._iteration_proposals.values()))
            )
            last_proposal["proposal_hash"] = self._iteration_proposal_hash(last_proposal)
        last_receipt = None
        if self._iteration_receipts:
            receipt = next(reversed(self._iteration_receipts.values()))
            last_receipt = self._safe_iteration_receipt_summary(receipt)
        last_outcome = None
        if self._iteration_outcomes:
            last_outcome = self._safe_iteration_outcome_dict(
                next(reversed(self._iteration_outcomes.values()))
            )
        return {
            "schema_version": 1,
            "enabled": bool(self.evaluation_harness is not None and controller is not None),
            "evaluation_harness_bound": self.evaluation_harness is not None,
            "promotion_controller_bound": controller is not None,
            "promotion_profile": getattr(controller, "profile", None),
            "sandbox_attestation_required": bool(
                getattr(controller, "require_sandbox_attestation", False)
            )
            if controller is not None
            else False,
            "proposal_count": len(self._iteration_proposals),
            "evaluated_count": len(self._iteration_receipts),
            "outcome_count": len(self._iteration_outcomes),
            "restore_rejected": self._iteration_restore_rejected,
            "last_proposal": last_proposal,
            "last_receipt": last_receipt,
            "last_outcome": last_outcome,
            # Never report a retained host/capability as state.  The host must
            # be supplied again after every restart and for every operation.
            "host_bound": False,
        }

    def _iteration_snapshot_for_persistence(self) -> dict[str, Any]:
        """Persist bounded evidence records without host capabilities."""

        payload = self._iteration_snapshot()
        payload["proposals"] = [
            {
                "proposal": self._safe_iteration_proposal_dict(proposal),
                "proposal_hash": self._iteration_proposal_hash(
                    self._safe_iteration_proposal_dict(proposal)
                ),
            }
            for proposal in list(self._iteration_proposals.values())[
                -self._ITERATION_RECORD_LIMIT :
            ]
        ]
        payload["receipts"] = [
            self._safe_iteration_receipt_envelope(proposal_id, receipt)
            for proposal_id, receipt in list(self._iteration_receipts.items())[
                -self._ITERATION_RECORD_LIMIT :
            ]
            if proposal_id in self._iteration_proposals
        ]
        payload["outcomes"] = [
            {
                "proposal_id": proposal_id,
                "outcome": self._safe_iteration_outcome_dict(outcome),
                "outcome_hash": self._iteration_proposal_hash(
                    self._safe_iteration_outcome_dict(outcome)
                ),
            }
            for proposal_id, outcome in list(self._iteration_outcomes.items())[
                -self._ITERATION_RECORD_LIMIT :
            ]
            if proposal_id in self._iteration_proposals
        ]
        return payload

    def _restore_iteration_pipeline(self, snapshot: Mapping[str, Any]) -> None:
        """Restore evidence only; never restore a host or an active write right."""

        raw = snapshot.get("iteration_pipeline")
        if not isinstance(raw, Mapping):
            return
        rejected = 0
        raw_proposals = raw.get("proposals", ())
        if isinstance(raw_proposals, (list, tuple)):
            for item in list(raw_proposals)[-self._ITERATION_RECORD_LIMIT :]:
                try:
                    proposal_payload = item.get("proposal", item) if isinstance(item, Mapping) else item
                    supplied_hash = (
                        str(item.get("proposal_hash", "")).strip().lower()
                        if isinstance(item, Mapping)
                        else ""
                    )
                    if supplied_hash and (
                        not re.fullmatch(r"[0-9a-f]{64}", supplied_hash)
                        or supplied_hash != self._iteration_proposal_hash(proposal_payload)
                    ):
                        raise ValueError("proposal hash mismatch")
                    proposal = ChangeProposal.from_dict(proposal_payload)
                    if proposal is None or proposal.validate():
                        rejected += 1
                        continue
                    if proposal.proposal_id not in self._iteration_proposals:
                        self._remember_iteration_record(proposal)
                except Exception:
                    rejected += 1
        raw_receipts = raw.get("receipts", ())
        if isinstance(raw_receipts, (list, tuple)):
            for item in list(raw_receipts)[-self._ITERATION_RECORD_LIMIT :]:
                if not isinstance(item, Mapping):
                    rejected += 1
                    continue
                proposal_id = str(item.get("proposal_id", ""))[:100]
                if proposal_id not in self._iteration_proposals:
                    rejected += 1
                    continue
                if bool(item.get("redacted", False)):
                    # The original receipt contained a path/secret-shaped
                    # value and was intentionally not persisted.  Its summary
                    # is observability only, never a promotion capability.
                    continue
                try:
                    receipt_payload = item.get("receipt", {})
                    supplied_receipt_hash = str(
                        item.get("receipt_hash", "")
                    ).strip().lower()
                    receipt = EvaluationReceipt.from_dict(receipt_payload)
                    if not receipt.verify():
                        raise ValueError("receipt hash mismatch")
                    if supplied_receipt_hash and supplied_receipt_hash != receipt.receipt_hash:
                        raise ValueError("receipt envelope hash mismatch")
                    binding = item.get("binding", {})
                    if not isinstance(binding, Mapping):
                        raise ValueError("candidate binding is invalid")
                    revision_id = str(binding.get("revision_id", ""))[:160]
                    fingerprint = str(binding.get("fingerprint", ""))[:128].lower()
                    if (
                        not revision_id
                        or not re.fullmatch(r"[0-9a-zA-Z._-]{1,160}", revision_id)
                        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                        or receipt.candidate_revision_id != revision_id
                        or receipt.candidate_fingerprint != fingerprint
                    ):
                        raise ValueError("candidate binding does not match receipt")
                    self._iteration_receipts[proposal_id] = receipt
                    self._iteration_candidate_bindings[proposal_id] = {
                        "revision_id": revision_id,
                        "fingerprint": fingerprint,
                    }
                except Exception:
                    rejected += 1
        while len(self._iteration_receipts) > self._ITERATION_RECORD_LIMIT:
            oldest_id = next(iter(self._iteration_receipts))
            self._iteration_receipts.pop(oldest_id, None)
            self._iteration_candidate_bindings.pop(oldest_id, None)
        self._iteration_restore_rejected = min(
            self._ITERATION_RECORD_LIMIT,
            max(0, int(raw.get("restore_rejected", 0))) + rejected,
        )

    def iteration_pipeline_snapshot(self) -> dict[str, Any]:
        """Read the P3 pipeline status without evaluating or writing anything."""

        return self._iteration_snapshot()

    def register_iteration_proposal(
        self,
        need: IterationNeed | Mapping[str, Any],
        *,
        host: Any,
        title: str,
        scope: str,
        hypothesis: str,
        evidence: Iterable[str] | None = None,
        expected_benefits: Iterable[str] | None = None,
        risks: Iterable[str] | None = None,
        resource_budget: Mapping[str, Any] | None = None,
        rollback_revision: str = "",
        baseline_revision: str = "",
        candidate_revision: str = "",
    ) -> ChangeProposal:
        """Register a candidate change request without touching source files.

        ``IterationNeed`` is evidence only.  The returned
        :class:`ChangeProposal` is immutable and remains unauthorized even if
        hostile input attempts to set approval fields.  Registration stores a
        bounded record in memory; it does not invoke the evaluator, spawn a
        process, modify ``active_root`` or contact a remote repository.
        """

        self._require_iteration_host(host)
        self._assert_production_motivation_policy()
        need_obj = self._coerce_iteration_need(need)
        proposal = ChangeProposal.from_need(
            need_obj,
            title=title,
            scope=scope,
            hypothesis=hypothesis,
            evidence=evidence or (),
            expected_benefits=expected_benefits or (),
            risks=risks or (),
            resource_budget=resource_budget or {},
            rollback_revision=rollback_revision,
            baseline_revision=baseline_revision,
        )
        if candidate_revision:
            proposal = replace(
                proposal,
                candidate_revision=str(candidate_revision)[:180],
            )
        issues = proposal.validate()
        if issues:
            raise ValueError("invalid change proposal: " + ", ".join(issues))
        if proposal.proposal_id in self._iteration_proposals:
            raise ValueError("proposal id collision")
        self._remember_iteration_record(proposal)
        self._sync_self_maintenance_projection()
        return proposal

    # A descriptive alias for hosts that use the ``ChangeProposal`` noun.
    register_change_proposal = register_iteration_proposal

    def evaluate_iteration_proposal(
        self,
        proposal: ChangeProposal | Mapping[str, Any],
        candidate: str | CandidateRevision,
        baseline: BaselineRevision | Mapping[str, Any] | str | None,
        *,
        host: Any,
        mode: EvaluationMode | PromotionMode | str = EvaluationMode.EVOLUTION,
        **kwargs: Any,
    ) -> EvaluationReceipt:
        """Evaluate one registered candidate through the host-bound harness.

        Evaluation is explicit and local-only.  It returns immutable evidence
        and never promotes or writes the active tree.  A configured
        ``PromotionController`` is preferred because it additionally checks
        that candidate and active roots are separate siblings.
        """

        self._require_iteration_host(host)
        self._assert_production_motivation_policy()
        registered = self._require_registered_iteration_proposal(proposal)
        if not registered.validate() == ():
            # This branch is defensive for records restored from old snapshots;
            # freshly registered proposals are validated above.
            raise ValueError("registered change proposal is no longer valid")
        descriptor = self._coerce_candidate_revision(candidate)
        if registered.candidate_revision and (
            registered.candidate_revision != descriptor.revision_id
        ):
            raise ValueError("candidate revision does not match proposal")
        evaluator = self.evaluation_harness
        if evaluator is None and self.promotion_controller is not None:
            evaluator = getattr(self.promotion_controller, "harness", None)
        if not isinstance(evaluator, EvaluationHarness):
            raise RuntimeError(
                "an EvaluationHarness must be injected before candidate evaluation"
            )
        evaluation_mode = (
            mode.value if isinstance(mode, (EvaluationMode, PromotionMode)) else mode
        )
        if self.promotion_controller is not None:
            receipt = self.promotion_controller.evaluate(
                descriptor,
                baseline,
                mode=evaluation_mode,
                **kwargs,
            )
        else:
            receipt = evaluator.evaluate(
                descriptor,
                baseline,
                mode=evaluation_mode,
                **kwargs,
            )
        if not isinstance(receipt, EvaluationReceipt) or not receipt.verify():
            raise ValueError("evaluator returned an invalid EvaluationReceipt")
        self._iteration_candidate_bindings[registered.proposal_id] = {
            "revision_id": descriptor.revision_id,
            "fingerprint": descriptor.fingerprint,
        }
        self._iteration_receipts[registered.proposal_id] = receipt
        # Keep receipt memory bounded even if proposals were restored from an
        # older snapshot with fewer records.
        while len(self._iteration_receipts) > self._ITERATION_RECORD_LIMIT:
            oldest_id = next(iter(self._iteration_receipts))
            self._iteration_receipts.pop(oldest_id, None)
            self._iteration_candidate_bindings.pop(oldest_id, None)
        self._sync_self_maintenance_projection()
        return receipt

    evaluate_iteration_candidate = evaluate_iteration_proposal

    def promote_iteration_proposal(
        self,
        proposal: ChangeProposal | Mapping[str, Any],
        candidate: str | CandidateRevision,
        evaluation_receipt: EvaluationReceipt | Mapping[str, Any] | None = None,
        *,
        host: Any,
        mode: PromotionMode | EvaluationMode | str | None = None,
        authorized: bool | None = None,
        sandbox_attestation: SandboxAttestation | Mapping[str, Any] | None = None,
        attestation: SandboxAttestation | Mapping[str, Any] | None = None,
    ) -> PromotionOutcome:
        """Request one explicit promotion through ``PromotionController``.

        No heartbeat/tick calls this method.  The host must opt in on every
        invocation; ``authorized=True`` (or an equivalent host authorization
        object) is required by the controller, and its production profile
        still requires a one-time host-issued ``SandboxAttestation``.
        """

        self._require_iteration_host(host)
        self._assert_production_motivation_policy()
        registered = self._require_registered_iteration_proposal(proposal)
        # An active-tree write is a constitutional operation, not merely a
        # controller capability.  Keep the check in the BrainStem seam so a
        # host cannot use an injected PromotionController to bypass lifecycle
        # or resource quarantine policy.  Evaluation remains available in
        # CREATED/DEGRADED because it only creates isolated evidence.
        if self._life_restore_blocked:
            raise LifecycleError(
                self._life_restore_error or "life restore is blocked"
            )
        if self.homeostasis.quarantine_latched:
            raise LifecycleError("homeostasis quarantine is latched")
        if self.life_kernel.is_terminal or self.life_kernel.state is LifecycleState.SUCCESSION_PENDING:
            raise LifecycleError(
                f"candidate promotion is denied in lifecycle state {self.life_kernel.state.value}"
            )
        if not self.life_kernel.allows_self_modification:
            raise LifecycleError(
                "candidate promotion requires an ACTIVE life kernel"
            )
        controller = self.promotion_controller
        if not isinstance(controller, PromotionController):
            raise RuntimeError(
                "a PromotionController must be injected before candidate promotion"
            )
        descriptor = self._coerce_candidate_revision(candidate)
        binding = self._iteration_candidate_bindings.get(registered.proposal_id)
        if binding is None:
            raise ValueError("candidate must be evaluated before promotion")
        if (
            binding.get("revision_id") != descriptor.revision_id
            or binding.get("fingerprint") != descriptor.fingerprint
        ):
            raise ValueError("candidate differs from the evaluated proposal revision")
        receipt = evaluation_receipt
        if receipt is None:
            receipt = self._iteration_receipts.get(registered.proposal_id)
        if receipt is None:
            raise ValueError("an EvaluationReceipt is required before promotion")
        if not isinstance(receipt, EvaluationReceipt):
            # Let the controller perform its strict mapping decoder, but do
            # not permit arbitrary candidate-owned receipt objects.
            if not isinstance(receipt, Mapping):
                raise TypeError("evaluation_receipt must be an EvaluationReceipt or mapping")
            receipt_obj = EvaluationReceipt.from_dict(receipt)
        else:
            receipt_obj = receipt
        stored = self._iteration_receipts.get(registered.proposal_id)
        if stored is not None and stored.receipt_hash != receipt_obj.receipt_hash:
            raise ValueError("evaluation receipt does not match the registered proposal")
        outcome = controller.promote(
            descriptor,
            receipt_obj,
            mode=mode,
            authorized=authorized,
            host=host,
            sandbox_attestation=sandbox_attestation,
            attestation=attestation,
        )
        if not isinstance(outcome, PromotionOutcome):
            raise ValueError("promotion controller returned an invalid outcome")
        self._iteration_outcomes[registered.proposal_id] = outcome
        while len(self._iteration_outcomes) > self._ITERATION_RECORD_LIMIT:
            oldest_id = next(iter(self._iteration_outcomes))
            self._iteration_outcomes.pop(oldest_id, None)
        self._sync_self_maintenance_projection()
        return outcome

    promote_iteration_candidate = promote_iteration_proposal
    promote_change_proposal = promote_iteration_proposal

    def _apply_homeostasis_decision(self, decision) -> None:
        """Tighten lifecycle state according to a resource decision."""
        state = self.life_kernel.state
        target = None
        if decision.action is HomeostasisAction.QUARANTINE:
            target = LifecycleState.QUARANTINED
        elif decision.action is HomeostasisAction.DEGRADE:
            target = LifecycleState.DEGRADED
        elif decision.action is HomeostasisAction.SLEEP:
            target = LifecycleState.SLEEPING
        if target is None or state == target or self.life_kernel.is_terminal:
            return
        if target not in LifeKernel.allowed_transitions(state):
            # CREATED cannot be safely skipped into quarantine.  The
            # controller latch still closes admission and blocks start.
            return
        try:
            self.life_kernel.transition(
                target,
                decision.reason,
                event_type="homeostasis_" + decision.action.value.lower(),
                metadata={
                    "resource_tick": decision.tick,
                    "ratios": dict(decision.ratios),
                    "exceeded": list(decision.exceeded),
                },
            )
        except LifecycleError as exc:
            # The resource controller is already conservative; retain its
            # decision and close admission if lifecycle persistence failed.
            self._record_loop_error("homeostasis_transition", exc)

    def observe_resources(
        self,
        usage: ResourceObservation | dict[str, Any],
        *,
        tick: int | None = None,
        source: str = "runtime",
        context: str = "",
    ):
        """Account for a trusted measurement and apply its safe response."""
        if isinstance(usage, ResourceObservation):
            observation = usage
        elif isinstance(usage, dict):
            observation = ResourceObservation(
                tick=self.state.total_ticks if tick is None else tick,
                usage=usage,
                source=source,
                context=context,
            )
        else:
            raise TypeError("usage must be ResourceObservation or a mapping")
        decision = self.homeostasis.observe(observation)
        self._apply_homeostasis_decision(decision)
        self._sync_self_maintenance_projection()
        return decision

    def clear_homeostasis_quarantine(
        self,
        *,
        verified: bool,
        evidence: str = "",
    ) -> None:
        """Enter RECOVERING only after an independent verification signal."""
        if not verified:
            raise PermissionError("independent verification is required")
        self.homeostasis.clear_quarantine(verified=True)
        if self.life_kernel.state is LifecycleState.QUARANTINED:
            self.life_kernel.transition(
                LifecycleState.RECOVERING,
                str(evidence or "homeostasis quarantine independently cleared")[:500],
                event_type="homeostasis_recovery_started",
            )
        self._sync_self_maintenance_projection()

    def complete_homeostasis_recovery(
        self,
        *,
        verified: bool,
        evidence: str,
    ) -> None:
        """Return RECOVERING to ACTIVE only with explicit verified evidence."""
        if not verified or not str(evidence).strip():
            raise PermissionError("verified recovery evidence is required")
        if self.homeostasis.quarantine_latched:
            raise LifecycleError("homeostasis quarantine is still latched")
        if self.life_kernel.state is not LifecycleState.RECOVERING:
            raise LifecycleError("life instance is not recovering")
        self.life_kernel.transition(
            LifecycleState.ACTIVE,
            str(evidence)[:500],
            event_type="homeostasis_recovery_completed",
        )
        self._sync_self_maintenance_projection()

    def authorize_environment_action(self, request: ActionRequest) -> ActionDecision:
        """Delegate to an attached adapter; absence is an explicit denial."""
        if not isinstance(request, ActionRequest):
            raise TypeError("request must be an ActionRequest")
        if self.homeostasis.quarantine_latched or self.life_kernel.state in {
            LifecycleState.QUARANTINED,
            LifecycleState.RECOVERING,
            LifecycleState.SUCCESSION_PENDING,
            LifecycleState.RETIRED,
            LifecycleState.DEAD,
        }:
            return ActionDecision(
                False,
                "lifecycle or homeostasis state denies environment action",
                request.request_hash,
                self.controlled_environment.kind
                if self.controlled_environment is not None
                else EnvironmentKind.LIVING,
            )
        if self.controlled_environment is None:
            return ActionDecision(
                False,
                "no controlled environment is attached",
                request.request_hash,
                EnvironmentKind.LIVING,
            )
        return self.controlled_environment.authorize(request)

    # ── Disaster-only succession seam (P5) ──

    def _succession_anchor_sink(self, payload: dict[str, Any]) -> bool:
        """Persist a redacted successor anchor set when the host supports it."""
        store = self.state_store
        append = getattr(store, "append_anchor_set", None) if store is not None else None
        if not callable(append):
            append = getattr(store, "append_succession_anchor_set", None) if store is not None else None
        if not callable(append):
            # A production coordinator must never interpret a missing
            # persistence projection as an acknowledged append.  The
            # explicit legacy profile may retain the historical in-memory
            # adapter behavior for offline tests.
            return not self._succession_profile_is_production()
        return bool(append(payload))

    def _succession_record_sink(self, payload: dict[str, Any]) -> bool:
        """Persist one immutable succession record through the host adapter."""
        store = self.state_store
        append = (
            getattr(store, "append_succession_record", None)
            if store is not None
            else None
        )
        if not callable(append):
            return not self._succession_profile_is_production()
        return bool(append(payload))

    def _succession_profile_is_production(self) -> bool:
        """Return the normalized production/legacy policy for host checks."""

        value = str(getattr(self, "succession_profile", "production") or "production")
        return value.strip().lower() not in {"legacy", "test", "dev", "local", "compatibility"}

    def _get_succession_coordinator(self) -> SuccessionCoordinator:
        coordinator = self.succession_coordinator
        if coordinator is not None:
            if coordinator.parent is not self.life_kernel:
                raise SuccessionRuntimeError(
                    "succession coordinator parent is not the current life kernel"
                )
            return coordinator
        if self._succession_profile_is_production() and self.state_store is not None:
            required = (
                "claim_life_control",
                "renew_life_control",
                "release_life_control",
                "append_life_event",
                "append_life_event_with_lease",
                "append_life_event_handover",
                "append_life_event_handover_seal",
                "append_anchor_set",
                "append_succession_record",
            )
            if any(not callable(getattr(self.state_store, name, None)) for name in required):
                raise LifecycleError(
                    "production succession requires the complete durable StateStore handover API"
                )
        coordinator = SuccessionCoordinator(
            self.life_kernel,
            anchor_vault=self.anchor_vault,
            succession_ledger=self.succession_ledger,
            life_event_sink=self._life_event_sink if self.state_store is not None else None,
            # Production succession requires durable projections.  The
            # adapters below are intentionally not exposed when no store is
            # attached; their historical no-op behavior remains available to
            # explicit legacy/in-memory callers only.
            anchor_sink=self._succession_anchor_sink if self.state_store is not None else None,
            record_sink=self._succession_record_sink if self.state_store is not None else None,
            profile=self.succession_profile,
            activation_attestor=self.succession_activation_attestor,
        )
        self.succession_coordinator = coordinator
        self.anchor_vault = coordinator.anchor_vault
        self.succession_ledger = coordinator.succession_ledger
        self.succession_profile = coordinator.activation_profile
        self.succession_activation_attestor = coordinator.activation_attestor
        return coordinator

    async def _stop_runtime_for_succession(self) -> None:
        """Close runtime admission without recording an intermediate sleep."""
        self._accepting_input = False
        self._stop_event.set()
        task = self._task
        self._task = None
        current = asyncio.current_task()
        if task and task is not current:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self._record_loop_error("succession_shutdown", exc)
        self._discard_pending_inputs("succession boundary")
        self._fail_all_waiters("succession boundary")
        self._life_runtime_started = False
        self.state.awake = False

    async def succeed_to_next_generation(
        self,
        *,
        failure: FailureAssessment | dict[str, Any],
        anchors: AnchorSet | dict[str, Any],
        reevaluated_anchor_ids=None,
        evaluation_receipts=None,
        successor_instance_id: str | None = None,
        terminal_state: LifecycleState | str | None = None,
    ) -> SuccessionOutcome:
        """Execute a verified disaster handover and leave the child CREATED.

        The caller must later provide an independent ``EvaluationReceipt`` to
        :meth:`authorize_successor`; no automatic wake or environment binding
        occurs here.
        """
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            if self._successor_activation_required:
                raise SuccessionRuntimeError("a successor is already awaiting evaluation")
            await self._stop_runtime_for_succession()
            coordinator = self._get_succession_coordinator()
            parent_kernel = self.life_kernel
            # The coordinator publishes a CREATED child genesis while the
            # parent remains SUCCESSION_PENDING, then seals the parent only
            # after child/anchor/record persistence succeeds.  Keep the
            # parent's durable lease for both phases and open only the narrow
            # handover routes in ``_life_event_sink`` until it returns.
            self._life_handover_lineage = parent_kernel.lineage_id
            self._life_handover_parent_generation = parent_kernel.generation
            try:
                outcome = coordinator.succeed(
                    failure=failure,
                    anchors=anchors,
                    reevaluated_anchor_ids=reevaluated_anchor_ids,
                    evaluation_receipts=evaluation_receipts,
                    successor_instance_id=successor_instance_id,
                    terminal_state=terminal_state,
                )
            except Exception:
                self._life_handover_lineage = ""
                self._life_handover_parent_generation = None
                # A failed handover never leaves a live parent lease behind
                # when the parent was sealed; if it remained non-terminal the
                # next explicit start may reclaim a fresh lease safely.
                self._release_life_control(parent_kernel)
                # Runtime admission remains closed after an unsuccessful
                # attempt; the parent itself is left untouched unless the
                # coordinator had already crossed its explicit freeze point.
                self._sync_life_projection()
                raise
            self._life_handover_lineage = ""
            self._life_handover_parent_generation = None
            # The parent is terminal after a successful handover.  Release its
            # exact durable proof before exposing the CREATED child; the child
            # will claim control only after independent activation evidence.
            self._release_life_control(parent_kernel)
            self.succession_coordinator = coordinator
            self.anchor_vault = coordinator.anchor_vault
            self.succession_ledger = coordinator.succession_ledger
            self.life_kernel = outcome.successor
            self._life_sink_attached = False
            self._life_runtime_started = False
            self._successor_activation_required = True
            self._successor_evaluation_receipt_hash = ""
            self.state.awake = False
            self._sync_life_projection()
            saved = await self._snapshot_state()
            if saved is False:
                self._record_loop_error(
                    "succession_snapshot",
                    "successor is durable only through its append-only ledgers",
                )
            return outcome

    # Vocabulary aliases used by embedders and operator scripts.
    begin_succession = succeed_to_next_generation
    create_successor = succeed_to_next_generation

    async def authorize_successor(
        self,
        evaluation_receipt: Any,
        *,
        activation_attestation: SuccessorActivationAttestation | Mapping[str, Any] | None = None,
        attestation: SuccessorActivationAttestation | Mapping[str, Any] | None = None,
    ) -> LifeKernel:
        """Open the child lifecycle only after an independent hard-gate receipt."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            coordinator = self.succession_coordinator
            if coordinator is None or coordinator.successor is None:
                raise SuccessionRuntimeError("no successor is waiting for evaluation")
            if self.life_kernel is not coordinator.successor:
                raise SuccessionRuntimeError("current life kernel is not the successor")
            if (
                activation_attestation is not None
                and attestation is not None
                and activation_attestation != attestation
            ):
                raise SuccessionRuntimeError(
                    "activation_attestation and attestation disagree"
                )
            supplied_attestation = (
                activation_attestation
                if activation_attestation is not None
                else attestation
            )
            # Preflight the independent proof before reserving the child in
            # the durable lease table.  ``activate_successor`` repeats these
            # checks and consumes a production nonce only after the claim is
            # ready, so a rejected proof cannot strand the lineage lease.
            coordinator._verified_evaluation_receipt(evaluation_receipt)
            if coordinator.require_activation_attestation:
                if coordinator.activation_attestor is None or supplied_attestation is None:
                    raise SuccessionRuntimeError(
                        "production successor activation requires a host-issued attestation"
                    )
                coordinator.activation_attestor.validate(
                    supplied_attestation,
                    outcome=coordinator.outcome,
                    evaluation_receipt=evaluation_receipt,
                    consume=False,
                )
            elif supplied_attestation is not None:
                if coordinator.activation_attestor is None:
                    raise SuccessionRuntimeError(
                        "activation attestor is required to consume an attestation"
                    )
                coordinator.activation_attestor.validate(
                    supplied_attestation,
                    outcome=coordinator.outcome,
                    evaluation_receipt=evaluation_receipt,
                    consume=False,
                )
            child = coordinator.successor
            self._claim_life_control(child)
            try:
                child = coordinator.activate_successor(
                    evaluation_receipt,
                    activation_attestation=activation_attestation,
                    attestation=attestation,
                )
            except Exception:
                self._release_life_control(child)
                raise
            self._successor_activation_required = False
            if isinstance(evaluation_receipt, Mapping):
                receipt_hash = evaluation_receipt.get("receipt_hash", "")
            else:
                receipt_hash = getattr(evaluation_receipt, "receipt_hash", "")
            self._successor_evaluation_receipt_hash = str(receipt_hash).strip().lower()
            self._sync_life_projection()
            saved = await self._snapshot_state()
            if saved is False:
                self._record_loop_error(
                    "successor_activation_snapshot",
                    "activation ledger is durable but the bounded snapshot was rejected",
                )
            return child

    activate_successor = authorize_successor

    # ── Durable life-control lease ──

    @staticmethod
    def _has_any_life_lease_api(store: Any) -> bool:
        """Detect a partially exposed lease adapter and fail closed.

        A duck-typed host that exposes only some fenced methods must not fall
        back to the historical unleased callback: that would make a typo in
        the capability contract look like successful persistence.
        """

        return bool(
            store is not None
            and any(
                callable(getattr(store, name, None))
                for name in (
                    "claim_life_control",
                    "renew_life_control",
                    "release_life_control",
                    "append_life_event_with_lease",
                    "append_life_event_handover",
                    "append_life_event_handover_seal",
                    "append_life_event_handover_finalize",
                )
            )
        )

    def _durable_life_control_available(self) -> bool:
        """Return whether the attached store exposes the fenced lease seam."""

        store = self.state_store
        return bool(
            store is not None
            and callable(getattr(store, "claim_life_control", None))
            and callable(getattr(store, "renew_life_control", None))
            and callable(getattr(store, "release_life_control", None))
            and callable(getattr(store, "append_life_event_with_lease", None))
            and callable(getattr(store, "append_life_event", None))
        )

    def _validate_life_control_policy(self) -> None:
        """Validate the persistence mode before any lifecycle replay/write."""

        store = self.state_store
        if store is None:
            return
        if self.life_control_mode == "legacy":
            if self._has_any_life_lease_api(store):
                raise LifecycleError(
                    "legacy life control cannot downgrade an adapter that exposes fenced leases"
                )
            if not callable(getattr(store, "append_life_event", None)):
                raise LifecycleError(
                    "legacy life control requires an append_life_event adapter"
                )
            return
        if not self._durable_life_control_available():
            raise LifecycleError(
                "persistent life control requires a fenced StateStore lease API"
            )

    def _durable_life_handover_available(self) -> bool:
        """Return whether the store supports the complete staged handover."""

        return bool(
            self._durable_life_control_available()
            and self.state_store is not None
            and callable(getattr(self.state_store, "append_life_event_handover", None))
            and callable(
                getattr(self.state_store, "append_life_event_handover_seal", None)
            )
        )

    def _claim_life_control(self, kernel: LifeKernel | None = None):
        """Claim the exact kernel before any durable lifecycle transition.

        ``StateStore`` claims are cross-process and fenced.  A stem without a
        persistent store retains the in-process ``SuccessionCoordinator``
        guard for backwards-compatible embeddings.  The returned lease is
        intentionally kept only in memory; it is never serialized.
        """

        target = kernel or self.life_kernel
        if not isinstance(target, LifeKernel):
            raise TypeError("kernel must be a LifeKernel")
        if self.state_store is None or self.life_control_mode == "legacy":
            try:
                SuccessionCoordinator.claim_control(
                    target, owner=self._life_local_control_token
                )
            except Exception as exc:
                raise LifecycleError("process-local life control claim failed") from exc
            self._life_local_control_kernel = target
            return None
        if not self._durable_life_control_available():
            raise LifecycleError(
                "persistent life control requires a fenced StateStore lease API"
            )
        claim = self.state_store.claim_life_control
        try:
            lease = claim(
                target.lineage_id,
                target.instance_id,
                self._life_control_owner_id,
                generation=target.generation,
                ttl_sec=self._life_control_ttl_sec,
                expected_last_event_hash=target.ledger.last_hash,
            )
        except Exception as exc:
            raise LifecycleError("life-control lease claim failed") from exc
        if lease is None:
            raise LifecycleError(
                "another process owns this lineage or its lifecycle head diverged"
            )
        try:
            # Keep the process-local guard as a second, cheap split-brain
            # barrier.  If it rejects, release the durable row immediately.
            SuccessionCoordinator.claim_control(
                target, owner=self._life_local_control_token
            )
        except Exception as exc:
            try:
                self.state_store.release_life_control(lease)
            except Exception:
                pass
            raise LifecycleError("process-local life control claim failed") from exc
        if target is self.life_kernel:
            self._life_control_lease = lease
            self._life_control_lost = False
            self._life_control_next_renew_at = (
                time.monotonic() + self._life_control_renew_interval_sec
            )
        self._life_local_control_kernel = target
        return lease

    def _release_life_control(self, kernel: LifeKernel | None = None) -> None:
        """Release only this stem's exact lease, retaining fencing tombstones."""

        target = kernel or self.life_kernel
        lease = self._life_control_lease
        if lease is not None and (
            target is self.life_kernel
            or (
                lease.lineage_id == target.lineage_id
                and lease.instance_id == target.instance_id
                and lease.generation == target.generation
            )
        ):
            try:
                release = getattr(self.state_store, "release_life_control", None)
                if callable(release):
                    release(lease)
            except Exception:
                # A lost/expired lease is already fenced; never replace it
                # with an unverified delete or make shutdown unsafe.
                pass
            if target is self.life_kernel or (
                lease.lineage_id == target.lineage_id
                and lease.instance_id == target.instance_id
                and lease.generation == target.generation
            ):
                self._life_control_lease = None
                self._life_control_next_renew_at = 0.0
        if self._life_local_control_kernel is target:
            SuccessionCoordinator.release_control(
                target, owner=self._life_local_control_token
            )
            self._life_local_control_kernel = None

    def _mark_life_control_lost(self, reason: str) -> None:
        """Close admission after a durable lease cannot be proven."""

        self._life_control_lost = True
        self._accepting_input = False
        self._life_restore_error = str(reason or "life-control lease lost")[:300]
        self._sync_life_projection()

    def _renew_life_control(self, *, force: bool = False) -> bool:
        """Renew the fenced lease before its TTL can elapse."""

        lease = self._life_control_lease
        if lease is None or self.state_store is None:
            return not self._life_control_lost
        now_mono = time.monotonic()
        if not force and now_mono < self._life_control_next_renew_at:
            return True
        renew = getattr(self.state_store, "renew_life_control", None)
        if not callable(renew):
            self._mark_life_control_lost("life-control renewal API disappeared")
            return False
        try:
            refreshed = renew(lease, ttl_sec=self._life_control_ttl_sec)
        except Exception:
            refreshed = None
        if refreshed is None:
            self._mark_life_control_lost("life-control lease expired or was fenced")
            return False
        self._life_control_lease = refreshed
        self._life_control_next_renew_at = (
            now_mono + self._life_control_renew_interval_sec
        )
        self._life_control_lost = False
        return True

    def _life_event_sink(self, event: dict[str, Any]) -> bool:
        """Persist one kernel event through the configured INSERT-only store."""
        store = self.state_store
        append = getattr(store, "append_life_event", None) if store is not None else None
        if not callable(append):
            if self._has_any_life_lease_api(store):
                self._mark_life_control_lost(
                    "persistent life adapter is missing its replay append API"
                )
                return False
            # A custom in-memory StateStore-like adapter may not implement the
            # optional constitutional table.  Keep the kernel usable in that
            # embedding, while real ``StateStore`` instances always expose it.
            return True
        # Existing history is replayed while restoring a kernel.  Replay is a
        # read/verify path and must not require a live ownership lease; the
        # append adapter remains idempotent and validates the immutable event.
        if self._life_replay_mode:
            return bool(append(event))

        # A CREATED successor's genesis is published as continuity evidence
        # before activation and deliberately has no *child* lease.  The
        # parent's durable proof is still held and the dedicated handover
        # adapter validates it while inserting the cross-generation event.
        if self._life_handover_lineage and isinstance(event, Mapping):
            # The parent terminal edge is appended only after child genesis,
            # anchors and the immutable succession record have landed.  Its
            # predecessor is still the parent's pending hash, while the
            # durable lease head has deliberately moved to the child hash;
            # route it through the atomic seal seam and keep the local lease
            # projection pinned to that child hash for the next claim.
            try:
                is_parent_seal = (
                    str(event.get("lineage_id", "")) == self._life_handover_lineage
                    and str(event.get("event_type", ""))
                    == "succession_parent_sealed"
                    and str(event.get("instance_id", ""))
                    == self.life_kernel.instance_id
                    and int(event.get("generation", -1))
                    == int(
                        self._life_handover_parent_generation
                        if self._life_handover_parent_generation is not None
                        else -1
                    )
                )
            except (TypeError, ValueError, OverflowError):
                is_parent_seal = False
            if is_parent_seal:
                lease = self._life_control_lease
                seal_append = getattr(
                    store, "append_life_event_handover_seal", None
                )
                if lease is not None and callable(seal_append) and self._durable_life_handover_available():
                    metadata = event.get("metadata", {})
                    if not isinstance(metadata, Mapping):
                        metadata = {}
                    try:
                        accepted = bool(
                            seal_append(
                                event,
                                lease,
                                successor_instance_id=metadata.get(
                                    "successor_instance_id"
                                ),
                                succession_record_id=metadata.get(
                                    "succession_record_id"
                                ),
                                succession_record_hash=metadata.get(
                                    "succession_record_hash"
                                ),
                            )
                        )
                    except Exception:
                        accepted = False
                    if not accepted:
                        self._mark_life_control_lost(
                            "durable succession seal rejected by lease"
                        )
                        return False
                    # Do not replace ``last_event_hash`` with the parent's
                    # terminal event hash: the StateStore intentionally keeps
                    # the child genesis hash as the lineage handover head.
                    return True
                if self._has_any_life_lease_api(store):
                    self._mark_life_control_lost(
                        "durable succession seal API is not bound"
                    )
                    return False
                try:
                    return bool(append(event))
                except Exception:
                    return False

            try:
                is_child_genesis = (
                    str(event.get("lineage_id", "")) == self._life_handover_lineage
                    and str(event.get("event_type", "")) == "created"
                    and int(event.get("sequence", 0) or 0) == 1
                    and int(event.get("generation", -1))
                    == int(
                        self._life_handover_parent_generation
                        if self._life_handover_parent_generation is not None
                        else -1
                    )
                    + 1
                )
            except (TypeError, ValueError, OverflowError):
                is_child_genesis = False
            if is_child_genesis:
                lease = self._life_control_lease
                handover_append = getattr(store, "append_life_event_handover", None)
                if lease is not None and self._durable_life_handover_available():
                    try:
                        accepted = bool(
                            handover_append(
                                event,
                                lease,
                                successor_instance_id=event.get("instance_id"),
                            )
                        )
                    except Exception:
                        accepted = False
                    if not accepted:
                        self._mark_life_control_lost(
                            "durable succession genesis rejected by lease"
                        )
                        return False
                    try:
                        self._life_control_lease = replace(
                            lease,
                            last_event_hash=str(event.get("event_hash", ""))
                            .strip()
                            .lower(),
                        )
                    except Exception:
                        self._mark_life_control_lost(
                            "durable succession head update failed"
                        )
                        return False
                    return True
                # A persistent adapter that exposes any lease seam but not the
                # dedicated cross-generation transaction must fail closed; an
                # unleased child insert would reopen the injection window.
                if self._has_any_life_lease_api(store):
                    self._mark_life_control_lost(
                        "durable succession handover API is not bound"
                    )
                    return False
                try:
                    return bool(append(event))
                except Exception:
                    return False

        lease = self._life_control_lease
        lease_append = getattr(store, "append_life_event_with_lease", None)
        if lease is not None and callable(lease_append):
            try:
                accepted = bool(lease_append(event, lease))
            except Exception:
                accepted = False
            if not accepted:
                self._mark_life_control_lost("durable lifecycle append rejected by lease")
                return False
            # The immutable lease object is refreshed locally after the atomic
            # append so the next transition presents the new predecessor hash.
            event_hash = str(event.get("event_hash", "")).strip().lower()
            if event_hash:
                try:
                    self._life_control_lease = replace(
                        self._life_control_lease,
                        last_event_hash=event_hash,
                    )
                except Exception:
                    self._mark_life_control_lost("durable lifecycle head update failed")
                    return False
            return True

        # A persistent adapter without the fenced append seam must not be
        # allowed to write lifecycle transitions through the old unguarded
        # callback.  Custom in-memory adapters remain compatible only when no
        # durable lease API is present at all.
        if self._has_any_life_lease_api(store):
            self._mark_life_control_lost("durable lifecycle lease is not bound")
            return False
        return bool(append(event))

    def _attach_life_sink(self, kernel: LifeKernel) -> None:
        """Attach durable history exactly once for the current kernel."""
        if self.state_store is None:
            self._life_sink_attached = False
            return
        # This method is also reachable from direct restore/activation helpers,
        # so the startup preflight cannot be its only guard.  Validate before
        # ``replay=True`` might invoke the adapter with the kernel genesis.
        self._validate_life_control_policy()
        if self._life_sink_attached and kernel is self.life_kernel:
            return
        append = getattr(self.state_store, "append_life_event", None)
        if not callable(append):
            if self._has_any_life_lease_api(self.state_store):
                raise LifecycleError(
                    "persistent life adapter is missing its replay append API"
                )
            self._life_sink_attached = False
            return
        self._life_replay_mode = True
        try:
            kernel.attach_event_sink(self._life_event_sink, replay=True)
            self._life_sink_attached = True
        finally:
            self._life_replay_mode = False

    def _load_life_events(self, *, instance_id: str | None = None) -> list[dict]:
        """Read the permanent lifecycle table without swallowing I/O errors."""
        loader = getattr(self.state_store, "load_life_events", None)
        if self.state_store is None or not callable(loader):
            return []
        try:
            if instance_id is None:
                raw = loader()
            else:
                raw = loader(instance_id=instance_id)
        except TypeError:
            raw = loader()
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple)):
            raise LedgerIntegrityError("life ledger loader returned a non-list")
        return [dict(item) if isinstance(item, dict) else item for item in raw]

    @staticmethod
    def _event_identity(raw: LifecycleEvent | dict) -> tuple[str, str, int]:
        if isinstance(raw, LifecycleEvent):
            return raw.lineage_id, raw.instance_id, raw.generation
        return (
            str(raw.get("lineage_id", "")),
            str(raw.get("instance_id", "")),
            int(raw.get("generation", 0)),
        )

    def _kernel_from_life_events(
        self,
        raw_events: list[dict],
        *,
        preferred_instance_id: str | None = None,
    ) -> LifeKernel:
        """Rebuild one current instance from the permanent ledger.

        Older generations may remain in the table after succession.  They are
        retained as evidence, but only one non-terminal instance may be live;
        ambiguity is treated as corruption instead of guessed recovery.
        """
        groups: dict[tuple[str, str, int], list[dict]] = {}
        for raw in raw_events:
            if not isinstance(raw, dict):
                raise LedgerIntegrityError("life ledger event is not an object")
            key = self._event_identity(raw)
            if not key[0] or not key[1]:
                raise LedgerIntegrityError("life ledger event identity is incomplete")
            groups.setdefault(key, []).append(raw)
        if not groups:
            raise LedgerIntegrityError("life ledger is empty")
        lineages = {key[0] for key in groups}
        if len(lineages) != 1:
            raise LedgerIntegrityError("multiple lineages found in one brain store")

        candidates: list[tuple[tuple[str, str, int], AppendOnlyLedger]] = []
        for key, events in groups.items():
            ledger = AppendOnlyLedger(events)
            ledger.verify_chain()
            candidates.append((key, ledger))

        if preferred_instance_id:
            preferred = [
                item for item in candidates if item[0][1] == str(preferred_instance_id)
            ]
            if preferred:
                candidates = preferred

        non_terminal = [
            item for item in candidates
            if LifecycleState.parse(item[1].head.to_state)
            not in {LifecycleState.RETIRED, LifecycleState.DEAD}
        ]
        if len(non_terminal) > 1:
            raise LedgerIntegrityError("multiple non-terminal life instances found")
        pool = non_terminal or candidates
        # Generation is the succession order; sequence and event timestamp
        # make selection deterministic when a store contains archived peers.
        key, ledger = max(
            pool,
            key=lambda item: (
                item[0][2],
                item[1].last_sequence,
                str(item[1].head.timestamp),
                item[0][1],
            ),
        )
        genesis_metadata = dict(ledger.events[0].metadata)
        core_data = genesis_metadata.get("identity_core")
        if not isinstance(core_data, dict) and not hasattr(core_data, "items"):
            # A kernel created before the durable-core metadata was introduced
            # can still be recovered only when the current in-memory core is
            # an exact lineage/fingerprint match.  Never invent a purpose.
            current_core = self.life_kernel.identity_core
            if (
                current_core.lineage_id == key[0]
                and genesis_metadata.get("identity_core_fingerprint")
                == current_core.fingerprint
            ):
                core = current_core
            else:
                raise LedgerIntegrityError("life genesis has no identity core")
        else:
            core = IdentityCore.from_dict(core_data)
        if core.lineage_id != key[0]:
            raise LedgerIntegrityError("life genesis lineage differs from event lineage")
        identity_data = genesis_metadata.get("identity")
        if isinstance(identity_data, dict) or hasattr(identity_data, "items"):
            identity = LifeIdentity.from_dict(identity_data)
            if identity.instance_id != key[1] or identity.generation != key[2]:
                raise LedgerIntegrityError("life genesis identity differs from event identity")
            parent_instance_id = identity.parent_instance_id
        else:
            parent_instance_id = None
        return LifeKernel(
            identity_core=core,
            instance_id=key[1],
            generation=key[2],
            parent_instance_id=parent_instance_id,
            ledger=ledger,
            lifecycle_state=ledger.head.to_state,
        )

    def _mark_life_restore_blocked(self, error: Exception | str) -> None:
        self._life_restore_blocked = True
        self._life_restore_error = str(error)[:300]
        self._accepting_input = False
        self._sync_life_projection()

    def _restore_life(self, snapshot: dict | None) -> None:
        """Restore/validate the constitutional kernel before waking a loop."""
        if self._life_restore_blocked:
            raise LifecycleError(self._life_restore_error or "life restore is blocked")
        candidate: LifeKernel | None = None
        raw_kernel = snapshot.get("life_kernel") if isinstance(snapshot, dict) else None
        legacy_bootstrap = False
        try:
            if raw_kernel is not None:
                candidate = LifeKernel.from_snapshot(raw_kernel)
                durable = self._load_life_events(instance_id=candidate.instance_id)
                if durable:
                    durable_candidate = self._kernel_from_life_events(
                        durable, preferred_instance_id=candidate.instance_id
                    )
                    snap_events = candidate.ledger.events
                    durable_events = durable_candidate.ledger.events
                    # A durable ledger is the continuity authority.  A
                    # shorter durable prefix means history was truncated or
                    # the store returned an incomplete view; trusting the
                    # snapshot suffix in that case could resurrect an event
                    # that no longer has a durable predecessor.  Block
                    # startup instead of silently choosing the longer,
                    # potentially stale projection.
                    if len(durable_events) < len(snap_events):
                        raise LedgerIntegrityError(
                            "durable life ledger is shorter than the snapshot"
                        )
                    common = min(len(snap_events), len(durable_events))
                    if any(
                        snap_events[index].event_hash
                        != durable_events[index].event_hash
                        for index in range(common)
                    ):
                        raise LedgerIntegrityError(
                            "snapshot and durable life ledgers diverge"
                        )
                    # A durable suffix may have been committed after the last
                    # brain-state snapshot.  Prefer it so a restart cannot
                    # repeat a transition or resurrect an older state.
                    if len(durable_events) > len(snap_events):
                        candidate = durable_candidate
            else:
                durable = self._load_life_events()
                if durable:
                    candidate = self._kernel_from_life_events(durable)
                else:
                    # ``BrainState.snapshot()`` is a legacy/compact API and
                    # may contain only the public projection.  It cannot prove
                    # identity, so deliberately ignore those fields and start
                    # the current process from its already-created kernel.
                    # This is an explicit compatibility mode, not an implicit
                    # claim that the projection was verified.
                    candidate = self.life_kernel
                    legacy_bootstrap = True
            self._attach_life_sink(candidate)
            self.life_kernel = candidate
            self._life_legacy_bootstrap = legacy_bootstrap
            self._sync_life_projection()
        except (LifecycleError, ValueError, TypeError) as exc:
            self._mark_life_restore_blocked(exc)
            raise LifecycleError(str(exc)) from exc

    def _prepare_life_for_start(self) -> None:
        """Apply only safe wake edges; never auto-revive quarantine/terminal states."""
        if self._life_restore_blocked:
            raise LifecycleError(self._life_restore_error or "life restore is blocked")
        self._validate_life_control_policy()
        try:
            state = self.life_kernel.state
            if getattr(self, "_successor_activation_required", False):
                # A CREATED successor is an audit artifact until an
                # independently verified evaluator receipt is presented.
                # Ordinary ``start()`` must not turn succession into an
                # implicit self-approval.
                raise LifecycleError(
                    "successor awaits independent evaluation before start"
                )
            # Claim durable ownership before any wake/quarantine transition.
            # The event sink then checks the same token/fencing/head inside
            # the SQLite transaction that appends each lifecycle event.
            self._claim_life_control(self.life_kernel)
            if self.homeostasis.quarantine_latched:
                # A latched resource alarm survives restart.  If the
                # lifecycle is still wakeable, make the quarantine explicit
                # before refusing startup; never silently resume work.
                if LifecycleState.QUARANTINED in LifeKernel.allowed_transitions(state):
                    self.life_kernel.transition(
                        LifecycleState.QUARANTINED,
                        "持久化资源稳态告警，启动前保持隔离",
                        event_type="homeostasis_quarantine_restore",
                    )
                raise LifecycleError("homeostasis quarantine is latched")
            if state == LifecycleState.CREATED:
                self.life_kernel.transition(
                    LifecycleState.BOOTSTRAPPING,
                    "心跳启动，开始受控引导",
                    event_type="bootstrap_started",
                )
                self.life_kernel.transition(
                    LifecycleState.ACTIVE,
                    "引导检查通过，进入活动态",
                    event_type="bootstrap_completed",
                )
            elif state in {LifecycleState.BOOTSTRAPPING, LifecycleState.SLEEPING}:
                self.life_kernel.transition(
                    LifecycleState.ACTIVE,
                    "显式唤醒请求",
                    event_type="wake",
                )
            elif state in {LifecycleState.ACTIVE, LifecycleState.DEGRADED}:
                pass
            else:
                raise LifecycleError(
                    f"life kernel cannot auto-resume from {state.value}"
                )
        except LifecycleError:
            self._accepting_input = False
            # A failed startup must not leave a valid lease owned by a stem
            # that never reached ACTIVE.  Expired/taken-over rows are safely
            # retained as fencing tombstones by the store.
            self._release_life_control(self.life_kernel)
            self._sync_life_projection()
            raise
        self._sync_life_projection()

    def _restore_succession_runtime(self, snapshot: dict) -> None:
        """Restore and cross-check the parent/child succession boundary."""
        raw_runtime = snapshot.get("succession_runtime") if isinstance(snapshot, dict) else None
        raw_activation = snapshot.get("succession_activation") if isinstance(snapshot, dict) else None
        if raw_runtime is None:
            # A bounded snapshot may be gone while the permanent P5 tables
            # remain.  Rebuild from those tables when available; otherwise a
            # pre-P5 snapshot has no succession state and must not infer one
            # from the compact ``life`` projection.
            if self._restore_succession_from_durable_store():
                return
            # A generation>0 kernel with a parent is only ever created by
            # the succession boundary.  If its P5 record/anchor proof is not
            # available, treating it as an ordinary CREATED organism would
            # let a partial handover bypass the independent activation gate
            # after restart.  Fail closed and require operator recovery.
            if (
                self.life_kernel.generation > 0
                and self.life_kernel.parent_instance_id
            ):
                self._mark_life_restore_blocked(
                    "successor continuity ledger has no complete succession boundary"
                )
                raise LifecycleError(
                    "successor continuity proof is missing; startup is blocked"
                )
            self.succession_coordinator = None
            self._successor_activation_required = False
            self._successor_evaluation_receipt_hash = ""
            return
        if not isinstance(raw_runtime, dict):
            self._mark_life_restore_blocked("succession runtime snapshot is invalid")
            raise LifecycleError("succession runtime snapshot is invalid")
        try:
            coordinator = SuccessionCoordinator.from_snapshot(
                raw_runtime,
                # The host's configured profile is authoritative; a mutable
                # snapshot may not downgrade production to legacy on restart.
                life_event_sink=self._life_event_sink if self.state_store is not None else None,
                anchor_sink=self._succession_anchor_sink if self.state_store is not None else None,
                record_sink=self._succession_record_sink if self.state_store is not None else None,
                profile=self.succession_profile,
                activation_attestor=self.succession_activation_attestor,
                # The child ledger is attached below, after the durable child
                # lease is reclaimed; replaying through the ordinary sink at
                # this point would be an unleased write.
                replay_life_events=False,
            )
        except Exception as exc:
            self._mark_life_restore_blocked(exc)
            raise LifecycleError(f"succession restore is blocked: {str(exc)[:240]}") from exc

        current = self.life_kernel
        designated = coordinator.successor or coordinator.parent
        if (
            current.lineage_id != designated.lineage_id
            or current.instance_id != designated.instance_id
            or current.generation != designated.generation
            or current.ledger.last_hash != designated.ledger.last_hash
        ):
            self._mark_life_restore_blocked(
                "snapshot life kernel and succession designated instance diverge"
            )
            raise LifecycleError("snapshot life kernel and succession state diverge")
        self.succession_coordinator = coordinator
        self.anchor_vault = coordinator.anchor_vault
        self.succession_ledger = coordinator.succession_ledger
        required = False
        receipt_hash = ""
        if raw_activation is not None:
            if not isinstance(raw_activation, dict):
                self._mark_life_restore_blocked("succession activation projection is invalid")
                raise LifecycleError("succession activation projection is invalid")
            required = bool(raw_activation.get("required", False))
            receipt_hash = str(raw_activation.get("receipt_hash", "")).strip()
        elif coordinator.successor is not None:
            required = coordinator.successor.state == LifecycleState.CREATED
        if coordinator.successor is None and required:
            self._mark_life_restore_blocked(
                "activation gate is set without a successor"
            )
            raise LifecycleError("activation gate is set without a successor")
        if coordinator.successor is not None:
            child_state = coordinator.successor.state
            if required and child_state != LifecycleState.CREATED:
                self._mark_life_restore_blocked(
                    "activation gate disagrees with successor lifecycle state"
                )
                raise LifecycleError("activation gate disagrees with successor state")
            if not required and child_state == LifecycleState.CREATED:
                self._mark_life_restore_blocked(
                    "successor is CREATED without an activation gate"
                )
                raise LifecycleError("successor activation boundary is incomplete")
        self._successor_activation_required = required
        self._successor_evaluation_receipt_hash = receipt_hash
        # The current life kernel has already been rebuilt from the durable
        # lifecycle table.  Keep it as the source of truth and only attach the
        # coordinator's child sink after the cross-check above.
        if coordinator.successor is not None:
            # ``from_snapshot`` necessarily creates a fresh LifeKernel
            # object.  The identity/hash cross-check above proves equivalence;
            # use the coordinator's object so subsequent authorization and
            # lifecycle transitions cannot accidentally split the child view.
            self.life_kernel = coordinator.successor
            self._life_sink_attached = False
            if self.state_store is not None:
                try:
                    self._attach_life_sink(self.life_kernel)
                except (LifecycleError, ValueError, TypeError) as exc:
                    self._mark_life_restore_blocked(exc)
                    raise LifecycleError("successor life ledger attachment failed") from exc
        self._sync_life_projection()

    def _restore_succession_from_durable_store(self) -> bool:
        """Recover P5 state after loss of the bounded BrainState snapshot."""
        store = self.state_store
        load_records = getattr(store, "load_succession_record_objects", None) if store is not None else None
        load_anchors = getattr(store, "load_anchor_set_objects", None) if store is not None else None
        if not callable(load_records) or not callable(load_anchors):
            return False
        try:
            records = list(load_records(lineage_id=self.life_kernel.lineage_id))
            if not records:
                return False
            anchors = list(load_anchors(lineage_id=self.life_kernel.lineage_id))
            if not anchors:
                raise LifecycleError("durable succession records have no anchor vault")
            latest = records[-1]
            if latest.successor_instance_id != self.life_kernel.instance_id:
                raise LifecycleError(
                    "durable succession successor differs from current life instance"
                )
            load_parent_events = self._load_life_events(
                instance_id=latest.parent_instance_id
            )
            if not load_parent_events:
                raise LifecycleError("durable succession parent life ledger is missing")
            parent = self._kernel_from_life_events(
                load_parent_events,
                preferred_instance_id=latest.parent_instance_id,
            )
            from brain.succession import AnchorVault, SuccessionLedger

            vault = AnchorVault(anchor_sets=anchors)
            ledger = SuccessionLedger(records=records)
            coordinator = SuccessionCoordinator.from_components(
                parent,
                self.life_kernel,
                anchor_vault=vault,
                succession_ledger=ledger,
                life_event_sink=self._life_event_sink if store is not None else None,
                anchor_sink=self._succession_anchor_sink if store is not None else None,
                record_sink=self._succession_record_sink if store is not None else None,
                profile=self.succession_profile,
                activation_attestor=self.succession_activation_attestor,
            )
            self.succession_coordinator = coordinator
            self.anchor_vault = vault
            self.succession_ledger = ledger
            self.succession_profile = coordinator.activation_profile
            self.succession_activation_attestor = coordinator.activation_attestor
            self._successor_activation_required = (
                self.life_kernel.state == LifecycleState.CREATED
            )
            self._successor_evaluation_receipt_hash = ""
            self._life_sink_attached = False
            self._attach_life_sink(self.life_kernel)
            self._sync_life_projection()
            return True
        except Exception as exc:
            self._mark_life_restore_blocked(exc)
            raise LifecycleError(
                f"durable succession restore is blocked: {str(exc)[:240]}"
            ) from exc

    def _enter_life_sleep(self) -> None:
        """Record an orderly process stop without changing terminal states."""
        try:
            if self._life_runtime_started and self.life_kernel.state in {
                LifecycleState.ACTIVE,
                LifecycleState.DEGRADED,
            }:
                self.life_kernel.transition(
                    LifecycleState.SLEEPING,
                    "心跳显式停止，进入可恢复休眠",
                    event_type="sleep",
                )
        finally:
            self._life_runtime_started = False
            self._sync_life_projection()

    # ── Public API ──

    @staticmethod
    def _transfer_queue(queue: asyncio.Queue) -> asyncio.Queue:
        """Copy buffered items into a queue owned by the current loop."""
        # A blocked reader/writer belongs to the old loop.  Replacing the
        # queue underneath it would strand that task forever, which is worse
        # than rejecting an unsafe cross-loop migration explicitly.
        active_getters = [
            waiter for waiter in (getattr(queue, "_getters", ()) or ())
            if not waiter.done()
        ]
        active_putters = [
            waiter for waiter in (getattr(queue, "_putters", ()) or ())
            if not waiter.done()
        ]
        if active_getters or active_putters:
            raise RuntimeError(
                "cannot move brain queue while a reader or writer is waiting; "
                "finish the owning event loop first"
            )
        replacement = asyncio.Queue(maxsize=queue.maxsize)
        while True:
            try:
                replacement.put_nowait(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
            except asyncio.QueueFull:
                break
        return replacement

    def _ensure_loop_primitives(self):
        """Rebind asyncio primitives when an embedded caller changes loops."""
        current = asyncio.get_running_loop()
        if self._bound_loop is current:
            # AgentBridge may have been started before the stem on this loop,
            # or may have moved an idle queue during a sequential restart.
            # Validate the shared queue even when the stem itself is already
            # bound so cross-loop ownership cannot remain implicit.
            if getattr(self.intent_queue, "_bound_loop", None) is not current:
                self.intent_queue.rebind_loop()
            return
        if self._bound_loop is None:
            # Objects are often constructed before an event loop exists.  The
            # primitives above are still unbound at that point, so simply
            # claim them for the first running loop.  In particular, preserve
            # a request future submitted before ``start()``; replacing the
            # maps here would silently cancel that caller's completion path.
            self.intent_queue.rebind_loop()
            self._bound_loop = current
            return
        lifecycle_lock = self._lifecycle_lock
        if lifecycle_lock.locked() or any(
            not waiter.done()
            for waiter in (getattr(lifecycle_lock, "_waiters", ()) or ())
        ):
            # A stop/start transition may have cleared ``_task`` before its
            # awaitable finished.  The lock is the authoritative ownership
            # marker in that window; do not replace it from another loop.
            raise RuntimeError(
                "brain-stem lifecycle transition is still running on another event loop"
            )
        if self._task and not self._task.done():
            # A live heartbeat must never be moved underneath itself.
            raise RuntimeError("brain-stem cannot change event loops while running")

        # Futures belong to the old loop and cannot safely be completed from a
        # new one.  They represent calls that outlived that loop, so cancel
        # them and let their callers observe cancellation rather than leak.
        for future in self._input_waiters.values():
            if not future.done():
                future.cancel()
        self._input_waiters.clear()
        self._input_sources.clear()

        self._stop_event = asyncio.Event()
        self._input_processed = asyncio.Event()
        self._pending_input = self._transfer_queue(self._pending_input)
        self._output_feed = self._transfer_queue(self._output_feed)
        self.intent_queue.rebind_loop()
        self._lifecycle_lock = asyncio.Lock()
        self._bound_loop = current

    async def start(self):
        """Start the consciousness loop, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self):
        """Start the consciousness loop."""
        if self._task and not self._task.done():
            return
        self._task = None
        # A prior stopped/failed start may have left a process-local or
        # durable lease on this exact object.  Release only our own proof
        # before a durable snapshot rebuilds an equivalent kernel; another
        # owner uses a different token and is unaffected.
        self._release_life_control(self.life_kernel)
        self._life_control_lost = False

        if self._life_restore_blocked:
            self._accepting_input = False
            raise LifecycleError(
                self._life_restore_error or "life kernel restore is blocked"
            )

        # Check the durable capability before replaying a snapshot into an
        # adapter.  A partial/legacy persistent adapter must not receive a
        # genesis event and only then discover that startup cannot claim a
        # fenced owner.  Hosts that knowingly retain an old adapter may opt in
        # to ``life_control_mode='legacy'``; that mode is process-local only.
        try:
            self._validate_life_control_policy()
        except LifecycleError:
            self._accepting_input = False
            raise

        logger.info("brain-stem: consciousness loop starting")
        self._stop_event.clear()
        # Admission stays closed until the constitutional kernel has been
        # restored, its durable sink attached, and the wake transition has
        # committed.  This prevents a request from racing an unverified
        # identity during restart.
        self._accepting_input = False
        self.start_time = datetime.now(timezone.utc)

        # Restore state before starting the loop.  Starting the task first
        # allowed a tick to race with state replacement and lose continuity.
        # Some persistence adapters intentionally implement ``__len__`` and
        # are falsey while empty; configuration must be tested by identity,
        # not truthiness, or restart snapshots/lease renewal silently vanish.
        if self.state_store is not None:
            try:
                restored = self.state_store.load_latest()
            except Exception as exc:
                restored = None
                self._record_loop_error("state_restore", exc)
            if restored:
                try:
                    self._restore_snapshot(restored)
                    logger.info("brain-stem: state restored from snapshot")
                except LifecycleError:
                    self._accepting_input = False
                    raise
                except Exception as exc:
                    # A corrupt/old snapshot must not prevent a fresh
                    # heartbeat from starting.  The in-memory defaults remain
                    # usable and the failure is visible in health state.
                    self._record_loop_error("state_restore", exc)
                    logger.warning("brain-stem: ignoring invalid snapshot: %s", str(exc)[:120])
                    # A generic snapshot decoder failure is recoverable, but
                    # the life ledger still has to be loaded/validated before
                    # the heartbeat can become active.
                    self._restore_life(None)
            else:
                self._restore_life(None)
        else:
            self._restore_life(None)

        # When the bounded snapshot journal is empty, the permanent life
        # ledger may still point at a gated successor.  Reconstruct that P5
        # boundary before any ordinary wake edge is considered.
        if self.succession_coordinator is None:
            restored_succession = self._restore_succession_from_durable_store()
            if (
                not restored_succession
                and self.life_kernel.generation > 0
                and self.life_kernel.parent_instance_id
            ):
                # A generation-bearing kernel with a parent can only be a
                # successor.  If the durable handover record/anchor proof is
                # absent, do not let the ordinary CREATED -> ACTIVE wake path
                # manufacture an apparently healthy child after a partial
                # sink failure.  The operator must restore the missing proof
                # or explicitly quarantine/recover the instance.
                self._mark_life_restore_blocked(
                    "successor continuity ledger has no complete succession boundary"
                )
                raise LifecycleError(
                    "successor continuity proof is missing; startup is blocked"
                )

        try:
            self._prepare_life_for_start()
        except LifecycleError as exc:
            self._accepting_input = False
            self._record_loop_error("life_start", exc)
            raise

        self._has_started = True
        self._accepting_input = True
        self._life_runtime_started = True

        # Tool execution is intentionally not replayed from a snapshot: a
        # crash may have happened after an external side effect but before its
        # feedback was journaled.  Close an in-flight action as interrupted so
        # the next heartbeat can make a fresh, policy-checked decision.  If
        # feedback was already durable, preserve that outcome without running
        # the tool a second time.
        if self.autonomy and self.autonomy.is_active:
            active = self.autonomy.active
            if active and active.status == EpisodeStatus.FEEDBACK_RECEIVED:
                recovered_success = active.success is not False
                recovered_outcome = "重启前已收到反馈，结果已恢复"
                recovered = (
                    self.autonomy.complete(recovered_outcome, self.state.total_ticks)
                    if recovered_success
                    else self.autonomy.fail(recovered_outcome, self.state.total_ticks)
                )
                self._finish_autonomy_episode(
                    recovered,
                    recovered_success,
                    recovered_outcome,
                )
            else:
                interrupted = self.autonomy.abort(
                    "进程重启，未重放未确认行动",
                    self.state.total_ticks,
                )
                self._finish_autonomy_episode(
                    interrupted,
                    False,
                    "进程重启，未重放未确认行动",
                )

        self.state.awake = True
        self._restored_uptime_seconds = max(0.0, float(self.state.uptime_seconds or 0.0))
        # ``wake_up`` means the process is available, but preserve the
        # recovered sleep phase so the next tick can make the same decision.
        if self.sleep_state not in {"awake", "drowsy", "light_sleep", "deep_sleep"}:
            self.sleep_state = "awake"
        self.state.sleep_state = self.sleep_state
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_loop_done)

    async def stop(self):
        """Stop the consciousness loop, serializing lifecycle transitions."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def retire(self, reason: str = "operator requested orderly retirement"):
        """Orderly, irreversible retirement of this concrete life instance."""
        return await self._terminate_life(LifecycleState.RETIRED, reason)

    async def mark_dead(self, reason: str = "integrity failure"):
        """Record an irreversible death after cancelling the runtime safely."""
        return await self._terminate_life(LifecycleState.DEAD, reason)

    async def _terminate_life(self, target: LifecycleState, reason: str) -> dict:
        """Stop runtime work, then commit one terminal kernel transition."""
        self._ensure_loop_primitives()
        async with self._lifecycle_lock:
            self._accepting_input = False
            self._stop_event.set()
            task = self._task
            self._task = None
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self._record_loop_error("terminal_shutdown", exc)
            self._discard_pending_inputs("life instance terminated")
            self._fail_all_waiters("life instance terminated")
            self._life_runtime_started = False
            try:
                self.life_kernel.transition(
                    target,
                    str(reason or "terminal transition")[:500],
                    event_type="retired" if target == LifecycleState.RETIRED else "dead",
                )
            except LifecycleError:
                self._sync_life_projection()
                raise
            self.state.awake = False
            self._release_life_control(self.life_kernel)
            self._sync_life_projection()
            await self._snapshot_state()
            return self.life_kernel.public_snapshot()

    async def _stop_unlocked(self):
        """Stop the consciousness loop."""
        logger.info("brain-stem: stopping consciousness loop")
        # Close the admission gate before cancelling the task.  Callers that
        # race with shutdown then receive an immediate, correlated failure
        # instead of enqueueing work that could survive into the next start.
        self._accepting_input = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # A failed background task must not make shutdown fail too.
                self._record_loop_error("loop_shutdown", exc)

        self._discard_pending_inputs("brain stopped")
        self._fail_all_waiters("brain stopped")
        self.state.awake = False

        # Keep process shutdown and constitutional lifecycle distinct: an
        # orderly stop records SLEEPING only for a runtime that actually
        # reached ACTIVE/DEGRADED.  A never-started stem remains CREATED, and
        # terminal states are never revived or rewritten.
        try:
            self._enter_life_sleep()
        except LifecycleError as exc:
            # The heartbeat is already stopped.  Preserve the last verified
            # kernel state and surface a durable-sink failure in health data;
            # do not claim that sleep was committed when it was not.
            self._record_loop_error("life_sleep", exc)

        # A stopped (SLEEPING) instance no longer owns the in-process control
        # lease.  A later explicit start may reclaim this same instance; a
        # second ACTIVE organism is still prevented by the claim in
        # ``_prepare_life_for_start``.
        self._release_life_control(self.life_kernel)

        # Final snapshot
        try:
            await self._snapshot_state()
        except Exception as exc:
            self._record_loop_error("final_snapshot", exc)

    def _on_loop_done(self, task: asyncio.Task):
        """Fail pending callers if the heartbeat exits unexpectedly."""
        # A completed task's callback can run just after an explicit restart.
        # Never let an old callback mark the newly-created heartbeat as dead.
        if self._task is not None and self._task is not task:
            return
        if task.cancelled() or self._stop_event.is_set():
            return
        try:
            error = task.exception()
        except Exception as exc:
            error = exc
        if error is None:
            error = "heartbeat exited without a stop request"
        self._record_loop_error("loop_exit", error)
        self._accepting_input = False
        self._discard_pending_inputs("brain loop stopped unexpectedly")
        self._fail_all_waiters("brain loop stopped unexpectedly")
        self.state.awake = False

    def _fail_all_waiters(self, error: str):
        """Resolve every outstanding input future with its own correlation ID."""
        self._fail_active_input(error)
        for request_id, future in list(self._input_waiters.items()):
            if not future.done():
                source = self._input_sources.get(request_id, "external")
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error=error,
                ))
            self._input_waiters.pop(request_id, None)
            self._input_sources.pop(request_id, None)

    def _discard_pending_inputs(self, error: str) -> int:
        """Drop queued requests during shutdown and resolve their futures.

        A request that has not reached ``_tick`` must never be replayed after
        restart.  Tracked callers still receive a normal structured result so
        they do not hang waiting for a future that can no longer complete.
        """
        discarded = 0
        while True:
            try:
                input_data = self._pending_input.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
            request_id = input_data.get("request_id") if isinstance(input_data, dict) else None
            if not request_id:
                continue
            future = self._input_waiters.pop(request_id, None)
            source = self._input_sources.pop(
                request_id,
                input_data.get("source", "external") if isinstance(input_data, dict) else "external",
            )
            if future and not future.done():
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error=error,
                ))
        if discarded:
            logger.info("brain-stem: discarded %d queued input(s) during shutdown", discarded)
        return discarded

    async def receive_input(
        self,
        text: str,
        source: str = "external",
        goal: str | None = None,
        episode_id: str | None = None,
        intent_id: str | None = None,
        goal_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        action_id: str | None = None,
        tool_observation: dict[str, Any] | None = None,
        observation_token: object | None = None,
    ):
        """Receive input from an external agent. Pushes to input queue."""
        self._ensure_loop_primitives()
        # This legacy one-way API does not need a completion future.  Avoid
        # retaining an unobserved waiter when a caller only wants to enqueue.
        request_id, _ = self.submit_input(
            text=text,
            source=source,
            goal=goal,
            episode_id=episode_id,
            intent_id=intent_id,
            goal_id=goal_id,
            plan_id=plan_id,
            step_id=step_id,
            action_id=action_id,
            tool_observation=tool_observation,
            observation_token=observation_token,
            track=False,
            _raise_on_reject=True,
        )
        logger.debug("brain-stem: input queued from %s (id=%s)", source, request_id)
        return request_id

    def submit_input(
        self,
        text: str,
        source: str = "external",
        goal: str | None = None,
        episode_id: str | None = None,
        intent_id: str | None = None,
        goal_id: str | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        action_id: str | None = None,
        tool_observation: dict[str, Any] | None = None,
        observation_token: object | None = None,
        track: bool = True,
        _raise_on_reject: bool = False,
    ) -> tuple[str, asyncio.Future | None]:
        """Queue an input and return an isolated completion future.

        Each caller gets its own correlation ID.  The old shared Event made
        concurrent requests observe one another's result and could return a
        stale intent after a timeout.
        """
        request_id = uuid.uuid4().hex
        source = str(source or "external")[:200]
        text = str(text or "")[:4000]
        goal = str(goal)[:500] if goal is not None else None
        episode_id = str(episode_id)[:80] if episode_id else None
        intent_id = str(intent_id)[:80] if intent_id else None
        goal_id = str(goal_id)[:100] if goal_id else None
        plan_id = str(plan_id)[:100] if plan_id else None
        step_id = str(step_id)[:100] if step_id else None
        action_id = str(action_id)[:100] if action_id else None
        # Structured tool observations are an in-process bridge contract.  Do
        # not accept arbitrary objects or unbounded payloads into the pending
        # queue; the bridge already removes response bodies and credentials.
        # The private token is checked by identity, not by a caller-controlled
        # boolean/string, so a normal API client can only submit text and will
        # receive an ``unknown`` result rather than self-authored proof.
        observation_trusted = observation_token is self._tool_observation_capability
        if isinstance(tool_observation, dict) and observation_trusted:
            def _bound_observation(value: Any, depth: int = 0):
                if depth > 3:
                    return "<depth-limit>"
                if value is None or isinstance(value, (bool, int, float)):
                    return value
                if isinstance(value, dict):
                    bounded: dict[str, Any] = {}
                    for raw_key, raw_value in list(value.items())[:24]:
                        key = str(raw_key)[:60]
                        if not key:
                            continue
                        lowered = key.casefold().replace("-", "_")
                        if any(
                            marker in lowered
                            for marker in (
                                "api_key", "access_token", "refresh_token",
                                "password", "passwd", "secret", "authorization",
                                "cookie", "credential", "private_key",
                            )
                        ):
                            bounded[key] = "<redacted>"
                        else:
                            bounded[key] = _bound_observation(raw_value, depth + 1)
                    return bounded
                if isinstance(value, (list, tuple, set)):
                    return [
                        _bound_observation(item, depth + 1)
                        for item in list(value)[:24]
                    ]
                return str(value).replace("\x00", " ")[:500]

            safe_observation: dict[str, Any] = {}
            for key, value in list(tool_observation.items())[:24]:
                key = str(key)[:60]
                if not key:
                    continue
                safe_observation[key] = _bound_observation(value, 1)
            tool_observation = safe_observation
        else:
            tool_observation = None
        future = None
        life_rejection = None
        if self._life_restore_blocked:
            life_rejection = self._life_restore_error or "life kernel restore is blocked"
        elif getattr(self, "homeostasis", None) is not None and self.homeostasis.quarantine_latched:
            life_rejection = "homeostasis quarantine is latched"
        elif self.life_kernel.is_terminal:
            life_rejection = f"life kernel is terminal ({self.life_kernel.state.value})"
        elif self._life_runtime_started and not self.life_kernel.accepts_input:
            life_rejection = f"life kernel does not accept input in {self.life_kernel.state.value}"
        if life_rejection:
            if _raise_on_reject:
                raise LifecycleError(life_rejection)
            if track:
                self._ensure_loop_primitives()
                future = asyncio.get_running_loop().create_future()
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error=life_rejection,
                ))
            return request_id, future
        # ``submit_input`` is also used by the legacy pre-start path.  A first
        # request may be buffered before ``start()``, but after an instance has
        # been started and stopped admission is closed until the next start.
        running = self._task is not None and not self._task.done()
        if self._has_started and (not running or self._stop_event.is_set()):
            if _raise_on_reject:
                raise RuntimeError("brain stopped")
            if track:
                self._ensure_loop_primitives()
                future = asyncio.get_running_loop().create_future()
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error="brain stopped",
                ))
            return request_id, future
        if track:
            self._ensure_loop_primitives()
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._input_waiters[request_id] = future
            self._input_sources[request_id] = source
        item = {
            "text": text,
            "source": source,
            "goal": goal,
            "episode_id": episode_id,
            "intent_id": intent_id,
            "goal_id": goal_id,
            "plan_id": plan_id,
            "step_id": step_id,
            "action_id": action_id,
            "tool_observation": tool_observation,
            "_observation_token": (
                self._tool_observation_capability if observation_trusted else None
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if track:
            item["request_id"] = request_id
        try:
            self._pending_input.put_nowait(item)
        except asyncio.QueueFull:
            self._input_waiters.pop(request_id, None)
            self._input_sources.pop(request_id, None)
            if _raise_on_reject:
                raise RuntimeError("input queue full")
            if future is not None:
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                    error="input queue full",
                ))
        return request_id, future

    async def get_output(self) -> dict | None:
        """Get the latest output event (non-blocking)."""
        self._ensure_loop_primitives()
        try:
            return self._output_feed.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def get_state(self) -> dict:
        """Get current brain state snapshot."""
        self._ensure_loop_primitives()
        self._sync_life_projection()
        return self.state.snapshot()

    # ── Main Loop ──

    async def _loop(self):
        """The consciousness loop — runs until stop_event is set."""
        logger.info("brain-stem: loop started")

        while not self._stop_event.is_set():
            # Fencing is checked at the heartbeat boundary.  If another host
            # has taken over an expired lease, stop admission before another
            # cognitive/action cycle can run under stale authority.
            if not self._renew_life_control():
                self._stop_event.set()
                break
            tick_start = datetime.now(timezone.utc)

            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._record_loop_error("tick", e)
                self._fail_active_input(str(e)[:200])

            # Account for the completed heartbeat as a bounded observation.
            # This is deliberately logical accounting (elapsed compute time),
            # not unrestricted host introspection.  A failure in the optional
            # accounting seam must not crash the heartbeat; its controller
            # remains fail-closed for explicitly supplied observations.
            try:
                elapsed_ms = max(
                    0.0,
                    (datetime.now(timezone.utc) - tick_start).total_seconds() * 1000.0,
                )
                # Keep a conservative bounded fallback when a wall-clock
                # adjustment makes the measurement implausible.
                if not math.isfinite(elapsed_ms) or elapsed_ms > 24 * 60 * 60 * 1000:
                    elapsed_ms = 0.0
                self.observe_resources(
                    {"compute_ms": min(elapsed_ms, 60_000.0)},
                    tick=self.state.total_ticks,
                    source="heartbeat",
                    context="completed heartbeat tick",
                )
            except Exception as exc:
                self._record_loop_error("homeostasis_observe", exc)

            self.state.total_ticks += 1
            self.state.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
            self.state.uptime_seconds = self._restored_uptime_seconds + (
                datetime.now(timezone.utc) - self.start_time
            ).total_seconds()

            # Periodic tasks
            try:
                now_ts = datetime.now(timezone.utc).timestamp()

                # Sleep-time dream + consolidation
                if self.sleep_state != "awake" and DREAM_ENABLED:
                    if now_ts - self.last_dream_time >= DREAM_INTERVAL_SEC:
                        self.last_dream_time = now_ts
                        await self._run_periodic("dream", self._dream_tick)
                    if now_ts - self.last_consolidation_time >= CONSOLIDATION_INTERVAL_SEC:
                        self.last_consolidation_time = now_ts
                        await self._run_periodic("consolidation", self._consolidation_tick)

                # Reflection (every REFLECTION_INTERVAL_SEC)
                if now_ts - self.last_reflection >= REFLECTION_INTERVAL_SEC:
                    self.last_reflection = now_ts
                    await self._run_periodic("reflection", self._reflection_tick)
                    logger.debug("brain-stem: reflection tick")

                # Memory decay (every MEMORY_DECAY_INTERVAL_TICKS ticks)
                if self.state.total_ticks % MEMORY_DECAY_INTERVAL_TICKS == 0 and self.state.total_ticks > 0:
                    self.last_decay = now_ts
                    if self.memory_store:
                        def _decay():
                            return self.memory_store.decay_all(
                                decay_rate=MEMORY_DECAY_RATE,
                                archive_threshold=MEMORY_ARCHIVE_THRESHOLD,
                            )

                        result = await self._run_periodic("memory_decay", _decay)
                        if isinstance(result, dict):
                            self._last_archived_count = int(result.get("archived", 0) or 0)

                # State snapshot (every STATE_SNAPSHOT_INTERVAL_SEC)
                if now_ts - self.last_snapshot >= STATE_SNAPSHOT_INTERVAL_SEC:
                    self.last_snapshot = now_ts
                    await self._run_periodic("snapshot", self._snapshot_state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The maintenance scheduler itself is part of the heartbeat;
                # protect it even if a future task is added without a wrapper.
                self._record_loop_error("maintenance", exc)

            # Sleep until next tick
            elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
            sleep_time = max(0.0, TICK_INTERVAL_SEC - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_time)
                break  # stop_event set
            except asyncio.TimeoutError:
                pass  # normal tick cycle

        logger.info("brain-stem: loop stopped after %d ticks", self.state.total_ticks)

    async def _run_periodic(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any] | Any],
    ) -> Any:
        """Run a maintenance task without taking down the heartbeat."""
        try:
            result = operation()
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict) and result.get("archived", 0) > 0:
                logger.info("brain-stem: archived %d decayed memories", result["archived"])
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_loop_error(name, exc)
            return None

    def _record_loop_error(self, phase: str, error: Exception | str):
        message = f"{phase}: {str(error)[:300]}"
        self.state.loop_error_count += 1
        self.state.last_loop_error = message
        self.state.recent_errors.append(message)
        self.state.recent_errors = self.state.recent_errors[-20:]
        if isinstance(error, Exception) and error.__traceback__ is not None:
            logger.error(
                "brain-stem: %s",
                message,
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            logger.error("brain-stem: %s", message)

    def _record_cognitive_timeout(self, phase: str) -> None:
        """Record a bounded cognitive timeout without taking down the loop."""
        label = str(phase or "cognitive")[:100]
        message = f"{label}: timeout after {self.cognitive_timeout_sec}s"
        self.state.llm_error_count += 1
        self.state.last_error = message[:200]
        self.state.recent_errors.append(message[:300])
        self.state.recent_errors = self.state.recent_errors[-20:]
        logger.warning("brain-stem: %s", message)

    async def _bounded_cognitive_await(self, awaitable, phase: str, fallback):
        """Await optional cognitive I/O with a hard heartbeat-local bound."""
        try:
            return await asyncio.wait_for(
                awaitable,
                timeout=self.cognitive_timeout_sec,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._record_cognitive_timeout(phase)
            return fallback

    @staticmethod
    def _cognitive_timeout_fallback(text: str = "") -> dict[str, Any]:
        """Build a side-effect-light result when external cognition times out."""
        snippet = str(text or "")[:80]
        return {
            "encoding": {
                "type": "episodic",
                "title": snippet or "timeout",
                "summary": snippet,
                "importance": 0.2,
                "entities": [],
            },
            "emotion": {},
            "focus": snippet,
            "monologue": "",
            "intent": {
                "type": "think",
                "confidence": 0.1,
                "reason": "认知服务超时，暂不采取行动",
            },
        }

    def _fail_active_input(self, error: str):
        request_id = self._active_input_id
        if not request_id:
            return
        future = self._input_waiters.pop(request_id, None)
        source = self._input_sources.pop(request_id, self.state.last_input_source or "external")
        if future and not future.done():
            future.set_result(self._build_input_result(
                source=source,
                pending=False,
                request_id=request_id,
                error=error,
            ))
        self._active_input_id = None

    def _complete_input_waiter(self, input_data: dict | None):
        """Resolve exactly the future associated with ``input_data``."""
        if not input_data:
            self._active_input_id = None
            return
        request_id = input_data.get("request_id")
        if request_id:
            future = self._input_waiters.pop(request_id, None)
            source = self._input_sources.pop(request_id, input_data.get("source", "external"))
            if future and not future.done():
                future.set_result(self._build_input_result(
                    source=source,
                    pending=False,
                    request_id=request_id,
                ))
        self._active_input_id = None

    def _build_input_result(
        self,
        source: str,
        pending: bool,
        request_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        """Build a stable response snapshot for one input request."""
        st = self.state
        sm = st.self_model
        # A pending response is deliberately a neutral acknowledgement: it
        # must not leak a different request's error or intent while the actor
        # is still working.  For completed requests only expose state errors
        # when the state belongs to this request.
        request_matches = request_id is None or request_id == st.last_input_id
        effective_error = None if pending else (
            error if error is not None else (st.last_error if request_matches else "")
        )

        # Never expose an intent from a different request.  A pending or
        # failed request has no response even if a prior intent exists.
        intent = None if pending or error is not None else st.last_intent
        # Keep completed responses type-stable for callers that render or
        # slice the field even when the brain only produced a ``think`` intent.
        # ``None`` remains reserved for a still-pending acknowledgement.
        response_text = None if pending else ""
        intent_type = None
        if intent:
            intent_type = intent.get("type")
            if intent_type == "respond":
                response_text = intent.get("response_text", "")
            elif intent_type == "ask_question":
                response_text = intent.get("question", "")

        try:
            memory_total = self.memory_store.count() if self.memory_store else 0
            identity_total = (
                len(self.memory_store.get_identity_memories(20))
                if self.memory_store else 0
            )
        except Exception:
            memory_total = identity_total = 0

        try:
            top_drives = [
                {
                    "name": d.get("name", ""),
                    "label": d.get("label", ""),
                    "weight": d.get("weight", 0.0),
                }
                for d in sm.get_top_drives(3)
                if isinstance(d, dict)
            ]
        except Exception:
            top_drives = []
        return {
            "accepted": False if pending or error is not None else (
                st.last_input_accepted if request_matches else False
            ),
            "gated": False if pending else st.last_input_gated,
            "llm_error": bool(effective_error),
            "llm_error_message": effective_error[:200] if effective_error else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "response": response_text,
            "intent_type": intent_type,
            "focus": st.focus_entity,
            "emotion": st.current_emotion,
            "inner_monologue": (st.inner_monologue or "")[:200],
            "working_memory": (st.current_context or "")[:300],
            "memories": {
                "total": memory_total,
                "identity_forming": identity_total,
            },
            "self": {
                "identity": str(sm.identity_anchor or "")[:200],
                "traits": list(sm.identity_traits or []),
                "top_drives": top_drives,
                "mood": str(sm.mood_tendency or "balanced"),
                "version": sm.identity_version,
                "experiences": sm.total_experiences,
                "last_reflection": str(sm.last_reflection or "")[:100],
            },
            "session": {
                "source": source,
                "active_sessions": st.session_manager.get_session_count(),
            },
            "request_id": request_id or st.last_input_id or None,
            "pending": pending,
        }

    # ── V11: Autonomous episode coordination ──

    def _sync_autonomy_projection(self) -> None:
        """Keep the API/snapshot projection aligned with the live manager."""
        if self.autonomy is not None:
            self.state.autonomy = self.autonomy.summary()

    def _find_goal(self, goal_id: str | None):
        if not goal_id:
            return None
        return self.goal_system.get_by_id(str(goal_id))

    def _execution_plan_for_goal(self, goal_id: str = "", task_id: str = ""):
        """Resolve the V13 plan owning a goal/episode without raising."""
        execution = self.task_execution
        if execution is None:
            return None
        try:
            return execution.get_plan(
                plan_id=str(task_id or ""),
                goal_id=str(goal_id or ""),
            )
        except Exception as exc:
            self._record_loop_error("task_execution_lookup", exc)
            return None

    def _execution_action_for_episode(
        self,
        episode_id: str = "",
        action_id: str = "",
        step_id: str = "",
        plan_id: str = "",
        include_terminal: bool = False,
    ):
        execution = self.task_execution
        if execution is None:
            return None
        try:
            action = execution.find_action(
                action_id=action_id,
                episode_id=episode_id,
                step_id=step_id,
                plan_id=plan_id,
                include_terminal=include_terminal,
            )
            if action is None and include_terminal:
                action = execution.find_action(
                    action_id=action_id,
                    episode_id=episode_id,
                    step_id=step_id,
                    plan_id=plan_id,
                    include_terminal=True,
                )
            return action
        except Exception as exc:
            self._record_loop_error("task_execution_action_lookup", exc)
            return None

    def _plan_has_verified_success(self, plan) -> bool:
        """Return whether every plan step has a durable verified success.

        Plan/step status is derived state and may be torn in a snapshot or in
        a partially completed cross-component hand-off.  Keep this predicate
        deliberately strict: a completed step counts only when an evaluated
        action points at an existing ``verified``/``success=True`` outcome
        belonging to the same plan and step.
        """
        if plan is None or self.task_execution is None:
            return False
        steps = list(getattr(plan, "steps", []) or [])
        if not steps:
            return False
        actions = getattr(self.task_execution, "actions", {})
        outcomes = getattr(self.task_execution, "outcomes", {})
        for step in steps:
            if getattr(step, "status", "") != StepStatus.COMPLETED:
                return False
            proven = False
            for action in getattr(actions, "values", lambda: ())():
                if (
                    getattr(action, "plan_id", "") != getattr(plan, "id", "")
                    or getattr(action, "step_id", "") != getattr(step, "id", "")
                    or getattr(action, "status", "") != ActionStatus.EVALUATED
                ):
                    continue
                outcome = getattr(outcomes, "get", lambda _key: None)(
                    getattr(action, "outcome_id", "")
                )
                if (
                    outcome is not None
                    and getattr(outcome, "plan_id", "") == getattr(plan, "id", "")
                    and getattr(outcome, "step_id", "") == getattr(step, "id", "")
                    and getattr(outcome, "status", "") == OutcomeQuality.VERIFIED
                    and getattr(outcome, "success", None) is True
                ):
                    proven = True
                    break
            if not proven:
                return False
        return True

    def _close_unqueued_execution_episode(self, goal, episode, plan) -> None:
        """Safely close an episode when a selected plan has no next step.

        ``next_step`` can return ``None`` after deriving a terminal/blocked
        plan.  The episode is created just before that call, so leaving it in
        ``planned`` would make the single autonomous lane permanently busy.
        This helper resolves the exceptional hand-off without touching a
        different episode and without claiming success from a bare plan flag.
        """
        autonomy = getattr(self, "autonomy", None)
        if (
            autonomy is None
            or getattr(autonomy, "active", None) is not episode
            or not getattr(autonomy, "is_active", False)
        ):
            return

        execution = getattr(self, "task_execution", None)
        status = str(getattr(plan, "status", "") or "")
        if status == PlanStatus.COMPLETED and self._plan_has_verified_success(plan):
            reason = "执行计划全部步骤已核验完成"
            record = autonomy.complete(
                reason,
                self.state.total_ticks,
                result_quality=OutcomeQuality.VERIFIED,
            )
            self._finish_autonomy_episode(record, True, reason)
            return

        if status == PlanStatus.FAILED:
            reason = "执行计划已失败，未产生可继续交付的步骤"
            record = autonomy.fail(
                reason,
                self.state.total_ticks,
                result_quality=OutcomeQuality.FAILED,
            )
            self._finish_autonomy_episode(record, False, reason)
            return

        reason = (
            "执行计划没有可安全交付的下一步骤，已暂停并等待显式恢复"
        )
        # A running action without a queued intent is an ambiguous boundary;
        # cancel it rather than allowing a later tick to replay an external
        # side effect.  ``pause_plan`` is bounded and skips already evaluated
        # outcomes.
        if execution is not None and status not in {
            PlanStatus.PAUSED,
            PlanStatus.BLOCKED,
            PlanStatus.ABANDONED,
        }:
            try:
                execution.pause_plan(
                    plan.id,
                    reason=reason,
                    tick=self.state.total_ticks,
                )
            except Exception as exc:
                self._record_loop_error("task_execution_empty_plan_pause", exc)
        self._sync_goal_with_plan(goal, plan, reason)
        record = autonomy.abort(reason, self.state.total_ticks)
        self._finish_autonomy_episode(record, False, reason)

    def _sync_goal_with_plan(self, goal, plan, note: str = "") -> None:
        """Project deterministic plan state onto the legacy Goal owner.

        GoalSystem remains the public lifecycle owner for compatibility.  A
        plan may only mark a goal done after *all* of its steps are completed;
        a single successful tool response never bypasses this boundary.
        """
        if goal is None or plan is None:
            return
        try:
            steps = list(getattr(plan, "steps", []) or [])
            completed = sum(
                1 for step in steps if getattr(step, "status", "") == StepStatus.COMPLETED
            )
            if steps:
                goal.progress = min(1.0, completed / len(steps))
            if note:
                goal.result_note = str(note)[:300]
            status = getattr(plan, "status", "")
            goal_status = getattr(goal, "status", "")
            actionable = goal_status in {"pending", "active", "paused"}
            if status == PlanStatus.COMPLETED:
                # Treat the plan status as a hint only.  The durable
                # action/observation/outcome chain is the final proof that
                # every step really completed; this second guard protects
                # against malformed snapshots or an embedder constructing a
                # TaskPlan object by hand.
                if self.task_execution is not None:
                    if not self._plan_has_verified_success(plan):
                        if goal_status in {"pending", "active"}:
                            goal.status = "paused"
                            goal.paused_reason = "execution:完成状态缺少可验证结果"
                        return
                if actionable and not (
                    goal_status == "paused"
                    and str(getattr(goal, "paused_reason", "")).startswith("manual:")
                ):
                    self.goal_system.mark_done(goal.id, note[:240] or "执行计划全部步骤已核验完成")
                if getattr(goal, "status", "") == "done":
                    self.task_scheduler.on_goal_terminal(goal.id)
                    if hasattr(self, "drive_engine"):
                        self.drive_engine.notify_goal_completed()
            elif status == PlanStatus.FAILED:
                if actionable and not (
                    goal_status == "paused"
                    and str(getattr(goal, "paused_reason", "")).startswith("manual:")
                ):
                    self.goal_system.mark_failed(goal.id, note[:240] or "执行计划失败")
                self.task_scheduler.on_goal_terminal(goal.id)
            elif status in {PlanStatus.PAUSED, PlanStatus.BLOCKED}:
                if goal_status in {"pending", "active"}:
                    goal.status = "paused"
                if goal.status == "paused" and not str(
                    getattr(goal, "paused_reason", "")
                ).startswith("manual:"):
                    reason = getattr(plan, "active_step_id", "") or "结果等待核验"
                    goal.paused_reason = f"execution:{str(reason)[:100]}"
                if self.task_scheduler.running_goal_id == goal.id:
                    self.task_scheduler.running_goal_id = None
            elif goal_status == "paused" and str(
                getattr(goal, "paused_reason", "")
            ).startswith(("execution:", "restart:")):
                goal.status = "pending"
                goal.paused_reason = ""
        except Exception as exc:
            self._record_loop_error("task_execution_goal_projection", exc)

    @staticmethod
    def _capture_execution_claim_state(plan) -> dict[str, Any]:
        """Capture the mutable plan/step fields touched by a claim.

        ``TaskExecutionLedger.next_step`` is intentionally a small synchronous
        mutation, but the following intent/action hand-off crosses several
        components.  Keeping a before-image here lets the hand-off behave like
        a transaction: if any later stage rejects the work, the claim can be
        released without leaving a permanently ``running`` step behind.
        """
        plan_fields = (
            "status",
            "active_step_id",
            "consumed_ticks",
            "updated_tick",
            "revision",
        )
        step_fields = (
            "status",
            "attempts",
            "retries",
            "replan_count",
            "started_tick",
            "completed_tick",
            "last_action_id",
            "result_quality",
            "result_summary",
            "blocked_reason",
        )
        return {
            "plan": {
                field: getattr(plan, field, None)
                for field in plan_fields
            },
            "steps": [
                (
                    step,
                    {
                        field: getattr(step, field, None)
                        for field in step_fields
                    },
                )
                for step in list(getattr(plan, "steps", []) or [])
            ],
        }

    @staticmethod
    def _restore_execution_claim_state(plan, state: dict[str, Any]) -> None:
        """Restore a claim before-image while preserving object identity."""
        if not isinstance(state, dict):
            return
        plan_values = state.get("plan", {})
        if isinstance(plan_values, dict):
            for key, value in plan_values.items():
                try:
                    setattr(plan, key, value)
                except Exception:
                    pass
        raw_steps = state.get("steps", [])
        if not isinstance(raw_steps, (list, tuple)):
            raw_steps = []
        for step, values in raw_steps:
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                try:
                    setattr(step, key, value)
                except Exception:
                    pass

    def _rollback_execution_claim(
        self,
        execution,
        plan,
        claim_state: dict[str, Any],
        action_ids_before: set[str],
        *,
        action=None,
        step_id: str = "",
        reason: str = "",
    ) -> None:
        """Undo a partially-delivered execution claim.

        Newly persisted actions are retained as cancelled audit records (they
        must never be replayed), while the plan and its steps are restored to
        their exact pre-claim state.  This method is deliberately defensive:
        it is also used when a mocked/embedded callback raises after inserting
        an action but before returning it.
        """
        cancelled_ids: list[str] = []
        action_store = getattr(execution, "actions", {})
        before = set(action_ids_before or set())
        candidate_ids: set[str] = set()
        try:
            candidate_ids.update(str(key) for key in action_store.keys() if str(key) not in before)
        except Exception:
            pass
        returned_id = str(getattr(action, "id", "") or "") if action is not None else ""
        if returned_id and returned_id not in before:
            candidate_ids.add(returned_id)

        for action_id in candidate_ids:
            try:
                candidate = action_store.get(action_id)
            except Exception:
                candidate = None
            if candidate is None:
                # A callback may return an action object without registering
                # it in the ledger.  It is still unsafe to hand it to the
                # bridge after rollback, so cancel the detached object too.
                if action is not None and action_id == returned_id:
                    candidate = action
                else:
                    continue
            if getattr(candidate, "outcome_id", ""):
                # Never rewrite an action that already has a durable outcome.
                continue
            try:
                candidate.status = ActionStatus.CANCELLED
                candidate.started_tick = None
                candidate.observed_ids = []
                candidate.outcome_id = ""
                cancelled_ids.append(action_id)
            except Exception as exc:
                self._record_loop_error("task_execution_action_rollback", exc)

        self._restore_execution_claim_state(plan, claim_state)
        try:
            event = getattr(execution, "_event", None)
            if callable(event):
                event(
                    "claim_rolled_back",
                    plan_id=getattr(plan, "id", ""),
                    step_id=step_id or getattr(plan, "active_step_id", "") or "",
                    tick=self.state.total_ticks,
                    detail=(reason or "执行交接失败，已回滚")[:300],
                    data={"cancelled_actions": len(cancelled_ids)},
                )
        except Exception as exc:
            self._record_loop_error("task_execution_claim_event", exc)

    def _release_failed_execution_episode(self, episode, reason: str) -> None:
        """Close a failed hand-off so the scheduler can retry safely.

        Leaving an ``AutonomyEpisode`` in ``planned``/``feedback_received``
        makes ``episode_busy`` true forever, which prevents the scheduler from
        claiming the restored pending step.  Abort only the matching active
        episode, then release the scheduler pointer even if an embedded
        autonomy implementation does not expose the full API.
        """
        goal_id = str(getattr(episode, "goal_id", "") or "")
        record = None
        try:
            autonomy = getattr(self, "autonomy", None)
            active = getattr(autonomy, "active", None) if autonomy is not None else None
            same_lane = bool(
                active is not None
                and getattr(active, "id", "") == getattr(episode, "id", "")
            )
            if same_lane and getattr(autonomy, "is_active", False):
                abort = getattr(autonomy, "abort", None)
                if callable(abort):
                    record = abort(reason[:240], self.state.total_ticks)
                if record is not None:
                    self._finish_autonomy_episode(record, False, reason)
        except Exception as exc:
            self._record_loop_error("task_execution_episode_rollback", exc)
        finally:
            try:
                scheduler = getattr(self, "task_scheduler", None)
                if scheduler is not None and getattr(scheduler, "running_goal_id", None) == goal_id:
                    scheduler.running_goal_id = None
            except Exception as exc:
                self._record_loop_error("task_execution_scheduler_rollback", exc)
            try:
                self._sync_autonomy_projection()
            except Exception as sync_exc:
                self._record_loop_error("task_execution_projection_rollback", sync_exc)

    def _queue_execution_step(self, goal, episode, plan):
        """Claim one ready plan step and construct its correlated intent."""
        execution = self.task_execution
        if execution is None or goal is None or episode is None or plan is None:
            return None, None

        # The ledger claim and the autonomy/intent records form one hand-off
        # transaction.  ``next_step`` changes the step and plan immediately;
        # keep before-images so a later capacity/error boundary cannot leave a
        # RUNNING step that no worker can ever service.
        claim_state = self._capture_execution_claim_state(plan)
        try:
            action_ids_before = {
                str(key) for key in getattr(execution, "actions", {}).keys()
            }
        except Exception:
            action_ids_before = set()

        claim_attempted = True
        step = None
        action = None

        def rollback(reason: str) -> None:
            try:
                self._rollback_execution_claim(
                    execution,
                    plan,
                    claim_state,
                    action_ids_before,
                    action=action,
                    step_id=str(getattr(step, "id", "") or ""),
                    reason=reason,
                )
            except Exception as exc:
                self._record_loop_error("task_execution_claim_rollback", exc)
            # An active episode with no queued intent blocks the scheduler's
            # single causal lane.  Close just this lane so the restored pending
            # step can be retried at the next safe boundary.
            try:
                self._release_failed_execution_episode(episode, reason)
            except Exception as exc:
                self._record_loop_error("task_execution_episode_rollback", exc)

        try:
            step = execution.next_step(plan_id=plan.id, current_tick=self.state.total_ticks)
            if step is None:
                self._sync_goal_with_plan(goal, plan)
                return None, None
            intent = self._goal_to_intent(
                goal,
                episode_id=episode.id,
                attempt_no=getattr(goal, "attempt_count", 0),
                step=step,
                plan_id=plan.id,
                step_id=step.id,
            )
            if intent is None:
                rollback("无法构造工具意图，已回滚执行认领")
                return None, None
            action = execution.record_action(
                plan.id,
                step.id,
                action_type=intent.type.value,
                tool_name=intent.tool_name or step.tool_name,
                args=intent.tool_args,
                expected=step.expected,
                tick=self.state.total_ticks,
                episode_id=episode.id,
                attempt_no=intent.attempt_no,
            )
            if action is None:
                rollback("执行账本容量不足，已回滚执行认领")
                return None, None
            intent.action_id = action.id
            intent.plan_id = plan.id
            intent.step_id = step.id
            try:
                planned = self.autonomy.plan(
                    episode.id,
                    intent.type.value,
                    intent.tool_name or "",
                    self.state.total_ticks,
                    intent.reason,
                    intent_id=intent.intent_id,
                    expected=step.expected or f"完成目标：{goal.description[:180]}",
                    attempt_no=intent.attempt_no,
                    plan_id=plan.id,
                    step_id=step.id,
                    action_id=action.id,
                )
            except Exception as exc:
                self._record_loop_error("task_execution_autonomy_plan", exc)
                planned = False
            if not planned:
                rollback("自主经历无法登记计划步骤，已回滚执行认领")
                return None, None
            # Keep the exact ledger identifiers on the episode as a durable
            # interruption/restart anchor.  ``plan`` normally already stores
            # them through the extended call above; the explicit bind also
            # supports a compatible autonomy implementation that accepts the
            # call but does not persist optional metadata.
            binder = getattr(self.autonomy, "bind_action", None)
            if callable(binder):
                try:
                    if not binder(
                        episode.id,
                        plan_id=plan.id,
                        step_id=step.id,
                        action_id=action.id,
                        tick=self.state.total_ticks,
                    ):
                        rollback("自主经历无法绑定执行账本行动，已回滚执行认领")
                        return None, None
                except Exception as exc:
                    self._record_loop_error("task_execution_autonomy_bind", exc)
                    rollback("自主经历绑定执行账本异常，已回滚执行认领")
                    return None, None
            if self.reward_system:
                # Reward anticipation is an auxiliary side effect.  It must
                # not invalidate an already committed autonomy plan when a
                # disabled/misconfigured reward backend raises.
                try:
                    self.reward_system.anticipate(
                        channel="achievement",
                        expectation=max(0.0, min(1.0, float(getattr(goal, "priority", 0.5)))),
                    )
                except Exception as exc:
                    self._record_loop_error("task_execution_reward_anticipate", exc)
            return intent, action
        except Exception as exc:
            self._record_loop_error("task_execution_step", exc)
            if claim_attempted:
                rollback("执行步骤交接异常，已回滚执行认领")
            return None, None

    def _finish_autonomy_episode(
        self,
        record,
        success: bool,
        outcome: str,
    ) -> None:
        """Close one episode and pass only verified outcomes into learning.

        The legacy GoalSystem/RewardSystem/SelfModel APIs remain available,
        but V13 routes terminal causal outcomes through
        :class:`VerifiedLearningFeedback`.  A plan is projected to ``done``
        only after every step has a deterministic verified result.
        """
        if record is None:
            return
        try:
            goal = self._find_goal(getattr(record, "goal_id", ""))
            status = getattr(record, "status", "")
            plan = self._execution_plan_for_goal(
                getattr(record, "goal_id", ""),
                getattr(record, "plan_id", "") or getattr(record, "task_id", ""),
            )
            # Resolve the exact action before closing the episode.  A close
            # can be triggered by timeout, user interruption, bridge failure,
            # or process restart; in all of those cases an unconfirmed
            # external side effect must be cancelled/quarantined, while an
            # already evaluated outcome must remain immutable.
            action = self._execution_action_for_episode(
                getattr(record, "id", ""),
                action_id=getattr(record, "action_id", "") or "",
                step_id=getattr(record, "step_id", "") or "",
                plan_id=getattr(plan, "id", "") if plan is not None else (
                    getattr(record, "plan_id", "") or getattr(record, "task_id", "")
                ),
                include_terminal=True,
            )
            if (
                self.task_execution is not None
                and plan is not None
                and status in {
                    EpisodeStatus.ABORTED,
                    EpisodeStatus.FAILED,
                    EpisodeStatus.COMPLETED,
                }
            ):
                execution = self.task_execution
                # Narrow the cancellation to this causal lane.  The helper
                # skips EVALUATED actions, so a verified result can never be
                # downgraded by a late timeout/abort callback.
                try:
                    execution.cancel_for_episode(
                        getattr(record, "id", ""),
                        reason=(outcome or "自主经历已收束")[:240],
                        plan_id=plan.id,
                        action_id=getattr(record, "action_id", "") or "",
                        step_id=getattr(record, "step_id", "") or "",
                        tick=self.state.total_ticks,
                    )
                except Exception as exc:
                    self._record_loop_error("task_execution_episode_cancel", exc)
                # A crash/abort can happen after ``next_step`` but before
                # ``record_action``.  There is no action to cancel in that
                # window, so explicitly pause the plan instead of leaving a
                # RUNNING step that the scheduler could accidentally replay.
                current_step = None
                if getattr(record, "step_id", ""):
                    current_step = plan.get_step(record.step_id)
                if current_step is None and getattr(plan, "active_step_id", ""):
                    current_step = plan.get_step(plan.active_step_id)
                if (
                    current_step is not None
                    and current_step.status == StepStatus.RUNNING
                    and plan.status not in {
                        PlanStatus.COMPLETED,
                        PlanStatus.FAILED,
                        PlanStatus.ABANDONED,
                    }
                ):
                    try:
                        execution.pause_plan(
                            plan.id,
                            reason=(outcome or "自主经历中止，等待显式恢复")[:240],
                            tick=self.state.total_ticks,
                        )
                    except Exception as exc:
                        self._record_loop_error("task_execution_episode_pause", exc)
            ledger_outcome = None
            if self.task_execution is not None and action is not None:
                ledger_outcome = self.task_execution.outcomes.get(
                    getattr(action, "outcome_id", ""),
                )
            quality = str(
                getattr(ledger_outcome, "status", "")
                or getattr(record, "result_quality", "unknown")
                or "unknown"
            ).lower()
            episode_success = bool(
                status == EpisodeStatus.COMPLETED
                and getattr(record, "success", None) is not False
                and success
            )
            # A plan-backed episode may represent one verified step of a
            # multi-step task.  Keep the episode's learning success local to
            # that step, while _sync_goal_with_plan enforces all-step goal
            # completion.
            if self.task_execution is not None and plan is not None:
                episode_success = episode_success and quality == OutcomeQuality.VERIFIED
                self._sync_goal_with_plan(goal, plan, outcome)
            elif status == EpisodeStatus.COMPLETED and goal is not None:
                # Legacy episodes without a V13 plan retain the old one-step
                # projection, subject to the verified completion policy.
                if episode_success and (
                    not getattr(self.task_execution, "require_verified_completion", True)
                    or quality == OutcomeQuality.VERIFIED
                ):
                    was_actionable = getattr(goal, "status", "") in {"active", "pending"}
                    if was_actionable:
                        self.goal_system.mark_done(goal.id, outcome[:240])
                    if was_actionable and getattr(goal, "status", "") == "done":
                        self.drive_engine.notify_goal_completed()
                elif getattr(goal, "status", "") == "active":
                    # Unknown/simulated feedback is retained as a resumable
                    # task rather than being silently marked failed.
                    goal.status = "pending"
            elif status == EpisodeStatus.FAILED and goal is not None:
                # A legacy explicit failure is terminal; an execution-backed
                # retry/pause decision has already been made by the ledger.
                if getattr(goal, "status", "") == "active":
                    self.goal_system.mark_failed(goal.id, outcome[:240])

            # Learning is a single idempotent sink.  Unknown and simulated
            # results are quarantined there and do not receive a reward or
            # memory/self-model reinforcement.
            receipt = None
            if self.learning_feedback is not None and status in {
                EpisodeStatus.COMPLETED,
                EpisodeStatus.FAILED,
            }:
                learning_outcome = {
                    "outcome_id": getattr(record, "id", ""),
                    "episode_id": getattr(record, "id", ""),
                    "task_id": getattr(record, "task_id", "") or getattr(plan, "id", ""),
                    "step_id": getattr(action, "step_id", "") if action is not None else getattr(record, "intent_id", ""),
                    "status": "completed" if status == EpisodeStatus.COMPLETED else "failed",
                    "success": episode_success,
                    "quality": quality,
                    "goal": getattr(record, "goal", ""),
                    "drive": (
                        getattr(goal, "source_drive", "")
                        or getattr(goal, "drive", "")
                        or getattr(record, "drive", "")
                    ),
                    "intent_type": getattr(record, "action_type", "") or "call_tool",
                    "tool_name": getattr(record, "tool_name", ""),
                    "summary": getattr(record, "result_summary", "") or outcome,
                    "verification_method": getattr(ledger_outcome, "evaluator", "") or (
                        "deterministic" if self.task_execution is not None else "legacy_boundary"
                    ),
                    "confidence": getattr(ledger_outcome, "confidence", 1.0) if ledger_outcome is not None else 1.0,
                    "channel": "achievement",
                }
                receipt = self.learning_feedback.apply(
                    learning_outcome,
                    procedural_memory=self.procedural_memory,
                    reward_system=self.reward_system,
                    self_model=self.state.self_model,
                    memory_store=self.memory_store,
                    reflection_engine=self.reflection_engine,
                    drive_engine=self.drive_engine,
                    activation=self.state.activation,
                    current_tick=self.state.total_ticks,
                )
                if receipt.memory_ids:
                    record.memory_ids = list(receipt.memory_ids)[-20:]
                if receipt.disposition == "verified_success":
                    record.reward = 0.8
                elif receipt.disposition == "verified_failure":
                    record.reward = 0.12
                else:
                    record.reward = None
            elif status in {EpisodeStatus.COMPLETED, EpisodeStatus.FAILED}:
                # Configuration may disable V13 entirely. Preserve the old
                # bounded scalar reward in that explicit compatibility mode.
                record.reward = 0.8 if episode_success else 0.2

            description = getattr(record, "goal", "自主经历")[:180]
            result_text = "完成" if episode_success else (
                "中止" if status == EpisodeStatus.ABORTED else "失败"
            )
            quality_note = (
                "（结果未核验，未进入学习层）"
                if quality in {OutcomeQuality.UNKNOWN, OutcomeQuality.SIMULATED}
                else "（模拟结果，未作为外部事实确认）"
                if quality == OutcomeQuality.SIMULATED else ""
            )
            marker = f"[自主经历] {result_text}{quality_note}：{description}"
            self.working_memory.push(
                marker[:240], "autonomy", 0.55 if episode_success else 0.35
            )
            self.state.last_narrative = marker[:500]

            # In V13 the learning sink above owns durable memory/self-model
            # writes.  Keeping this marker only in working memory avoids
            # duplicate reinforcement and preserves quarantine semantics.
        except Exception as exc:
            self._record_loop_error("autonomy_close", exc)
        finally:
            try:
                self.task_scheduler.on_episode_closed(record)
            except Exception as exc:
                self._record_loop_error("task_scheduler_close", exc)
            self._sync_autonomy_projection()

    def _record_autonomy_feedback(
        self,
        input_data: dict,
        success: bool,
        tool_name: str,
    ) -> bool:
        if self.autonomy is None:
            return False
        # Resolve the active causal lane before touching the execution ledger.
        # A late callback (after abort/timeout) must be an orphan; otherwise a
        # valid-looking observation could complete an action whose episode no
        # longer exists.  This check also gives plan-backed callbacks a single
        # authoritative episode object against which all IDs are compared.
        active_record = getattr(self.autonomy, "active", None)
        episode_id = input_data.get("episode_id")
        if not episode_id:
            # Legacy bridges did not send correlation IDs.  Only use the
            # single-lane fallback when the returned tool name also matches
            # the action we are waiting for; otherwise unrelated external
            # tool traffic must remain an orphan rather than close this run.
            active = active_record
            if (
                active is not None
                and active.status in {
                    EpisodeStatus.AWAITING_ACTION,
                    EpisodeStatus.AWAITING_FEEDBACK,
                }
                and active.tool_name == tool_name
            ):
                episode_id = active.id
        if not episode_id:
            # ``AutonomyEpisode`` deliberately requires explicit correlation.
            # Account for the unmatched observation without letting it mutate
            # whichever episode happens to be active.
            self.autonomy.total_orphan_feedback += 1
            self._sync_autonomy_projection()
            return False
        # An explicit episode ID is not enough by itself: it must still name
        # the currently active, non-terminal episode.  In particular, do this
        # *before* ``find_action``/``record_observation`` so delayed feedback
        # cannot mutate a plan after an abort or timeout.
        if (
            active_record is None
            or getattr(active_record, "id", "") != str(episode_id)
            or getattr(active_record, "status", "") not in {
                EpisodeStatus.AWAITING_ACTION,
                EpisodeStatus.AWAITING_FEEDBACK,
            }
        ):
            self.autonomy.total_orphan_feedback += 1
            self._sync_autonomy_projection()
            return False
        # V13 path: evaluate the structured bridge observation first.  The
        # text shown to the language model is deliberately not trusted as
        # proof, and a missing observation becomes ``unknown``.
        execution = self.task_execution
        active_plan = None
        plan_key = (
            getattr(active_record, "plan_id", "")
            or getattr(active_record, "task_id", "")
            or input_data.get("plan_id")
            or ""
        )
        plan_backed = bool(
            execution is not None
            and (
                plan_key
                or input_data.get("step_id")
                or input_data.get("action_id")
            )
        )
        if plan_backed:
            # A plan-backed lane must never fall through to the legacy text
            # classifier when its action correlation is missing or stale.
            # Otherwise a caller could attach an unverified success message
            # to an active plan simply by reusing the episode id.
            active_plan = self._execution_plan_for_goal(
                getattr(active_record, "goal_id", ""),
                plan_key,
            )
            if active_plan is None:
                self.autonomy.total_orphan_feedback += 1
                self._sync_autonomy_projection()
                return False

            # Every plan-backed callback must carry the complete correlation
            # tuple.  Falling back to "newest action for this episode" makes a
            # missing action_id (or a stale step) indistinguishable from a
            # valid delivery and permits forged feedback to advance a plan.
            expected_ids = {
                "goal_id": getattr(active_record, "goal_id", ""),
                "plan_id": getattr(active_plan, "id", ""),
                "step_id": getattr(active_record, "step_id", "") or "",
                "action_id": getattr(active_record, "action_id", "") or "",
                "intent_id": getattr(active_record, "intent_id", ""),
            }
            supplied_ids = {
                "goal_id": input_data.get("goal_id") or "",
                "plan_id": input_data.get("plan_id") or "",
                "step_id": input_data.get("step_id") or "",
                "action_id": input_data.get("action_id") or "",
                "intent_id": input_data.get("intent_id") or "",
            }
            if (
                any(not str(value) for value in supplied_ids.values())
                or supplied_ids["goal_id"] != str(expected_ids["goal_id"])
                or supplied_ids["plan_id"] != str(expected_ids["plan_id"])
                or supplied_ids["step_id"] != str(expected_ids["step_id"])
                or supplied_ids["action_id"] != str(expected_ids["action_id"])
                or supplied_ids["intent_id"] != str(expected_ids["intent_id"])
                or str(plan_key) != str(active_plan.id)
                or (
                    getattr(active_record, "task_id", "")
                    and str(getattr(active_record, "task_id", "")) != str(active_plan.id)
                )
                or (
                    getattr(active_record, "plan_id", "")
                    and str(getattr(active_record, "plan_id", "")) != str(active_plan.id)
                )
            ):
                self.autonomy.total_orphan_feedback += 1
                self._sync_autonomy_projection()
                return False
        action = None
        if plan_backed:
            action = self._execution_action_for_episode(
                str(episode_id),
                action_id=input_data.get("action_id") or "",
                step_id=input_data.get("step_id") or "",
                plan_id=getattr(active_plan, "id", "") if active_plan is not None else "",
                include_terminal=False,
            )
        if plan_backed:
            # Validate the tool/step/action relationship before recording the
            # observation.  Autonomy.record_feedback performs a similar check,
            # but it runs after ledger evaluation; relying on it alone would
            # let a mismatched callback mark the step completed even though
            # the episode rejects the feedback.
            if (
                action is None
                or action.episode_id != str(episode_id)
                or action.plan_id != str(active_plan.id)
                or action.step_id != str(input_data.get("step_id") or "")
                or (
                    getattr(action, "tool_name", "")
                    and str(getattr(action, "tool_name", "")) != str(tool_name or "")
                )
                or (
                    getattr(active_record, "tool_name", "")
                    and str(getattr(active_record, "tool_name", "")) != str(tool_name or "")
                )
                or str(getattr(action, "status", "")) != ActionStatus.STARTED
            ):
                self.autonomy.total_orphan_feedback += 1
                self._sync_autonomy_projection()
                return False
        elif execution is not None:
            # A ledger action without a bound task/episode is not a safe
            # legacy fallback.  Reject it rather than evaluating it and then
            # attaching the result to a text-only episode.
            try:
                orphan_action = execution.find_action(
                    action_id=input_data.get("action_id") or "",
                    episode_id=str(episode_id),
                    include_terminal=False,
                )
            except Exception:
                orphan_action = None
            if orphan_action is not None:
                self.autonomy.total_orphan_feedback += 1
                self._sync_autonomy_projection()
                return False
        outcome_record = None
        if execution is not None and action is not None:
            observation = input_data.get("tool_observation")
            if input_data.get("_observation_token") is not self._tool_observation_capability:
                # Direct/API callers cannot inject the bridge's structured
                # evidence.  Keep only the bounded text envelope, which is
                # intentionally insufficient for a verified outcome.
                observation = None
            if not isinstance(observation, dict):
                observation = {
                    "summary": str(input_data.get("text", ""))[:300],
                    "data": {},
                }
            try:
                outcome_record = execution.record_observation(
                    action.id,
                    observation,
                    tick=self.state.total_ticks,
                )
            except Exception as exc:
                self._record_loop_error("task_execution_observation", exc)
                outcome_record = None
            if outcome_record is not None and hasattr(outcome_record, "status"):
                self._last_execution_outcome = outcome_record
                verified_success = bool(
                    getattr(outcome_record, "status", "") == OutcomeQuality.VERIFIED
                    and getattr(outcome_record, "success", None) is True
                )
                accepted = self.autonomy.record_feedback(
                    episode_id=episode_id,
                    success=verified_success,
                    tool_name=tool_name,
                    tick=self.state.total_ticks,
                    intent_id=input_data.get("intent_id") or "",
                    result_summary=(
                        getattr(outcome_record, "summary", "")
                        or getattr(outcome_record, "reason", "")
                        or ("success" if verified_success else "failure")
                    ),
                    result_quality=getattr(outcome_record, "status", OutcomeQuality.UNKNOWN),
                    plan_id=input_data.get("plan_id") or "",
                    step_id=input_data.get("step_id") or "",
                    action_id=input_data.get("action_id") or "",
                )
                self._sync_autonomy_projection()
                return accepted
            # A validated action can still fail to append its observation
            # (most commonly because the bounded journal is full).  The
            # ledger pauses/quarantines the plan in that case, so leaving the
            # autonomy episode active would hold the scheduler's single lane
            # until an unrelated timeout.  Close only this exact episode;
            # ``_finish_autonomy_episode`` will cancel any remaining
            # ambiguous action without fabricating an outcome.
            self.autonomy.total_orphan_feedback += 1
            active_now = getattr(self.autonomy, "active", None)
            if active_now is active_record and getattr(self.autonomy, "is_active", False):
                reason = "执行观察无法写入有界账本，计划已暂停"
                try:
                    aborted = self.autonomy.abort(reason, self.state.total_ticks)
                    self._finish_autonomy_episode(aborted, False, reason)
                except Exception as exc:
                    self._record_loop_error("task_execution_observation_abort", exc)
            self._sync_autonomy_projection()
            return False

        if execution is not None and active_plan is not None:
            # Correlated plan actions are the only authority for V13 feedback;
            # do not downgrade to the legacy textual path on a missing or
            # mismatched action.
            self.autonomy.total_orphan_feedback += 1
            self._sync_autonomy_projection()
            return False

        # Legacy bridge/episode path.  Keep correlation checks and the old
        # textual classifier for compatibility, but still pass an explicit
        # quality so unknown callbacks cannot be silently promoted.
        quality = self._classify_autonomy_feedback(
            input_data.get("text", ""), success
        )
        # Preserve explicit structured failure/simulation markers for the
        # compatibility path as well.  This prevents a rendered
        # ``{"success": false}`` payload from being promoted by keyword
        # classification while retaining older positive text callbacks.
        legacy_observation = input_data.get("tool_observation")
        if isinstance(legacy_observation, dict):
            legacy_data = legacy_observation.get("data")
            if not isinstance(legacy_data, dict):
                legacy_data = legacy_observation
            if (
                legacy_observation.get("error")
                or legacy_data.get("error")
                or legacy_data.get("success") is False
                or legacy_data.get("ok") is False
                or str(legacy_data.get("result_quality", legacy_data.get("quality", ""))).lower()
                in {"failed", "failure", "error"}
            ):
                quality = "failed"
            elif legacy_observation.get("simulated") or str(
                legacy_data.get("result_quality", legacy_data.get("quality", ""))
            ).lower() in {"simulated", "dry_run", "dry-run"}:
                quality = "simulated"
        elif not success:
            quality = "failed"
        accepted = self.autonomy.record_feedback(
            episode_id=episode_id,
            success=success,
            tool_name=tool_name,
            tick=self.state.total_ticks,
            intent_id=input_data.get("intent_id") or "",
            result_summary="success" if success else "failure",
            result_quality=quality,
            plan_id=input_data.get("plan_id") or "",
            step_id=input_data.get("step_id") or "",
            action_id=input_data.get("action_id") or "",
        )
        self._sync_autonomy_projection()
        return accepted

    @staticmethod
    def _classify_autonomy_feedback(text: str, success: bool) -> str:
        """Classify execution feedback without treating placeholders as facts."""
        if not success:
            return "failed"
        normalized = str(text or "").lower()
        if any(
            marker in normalized
            for marker in (
                "模拟",
                "simulated",
                "dry-run",
                "需配置搜索引擎",
                "来源质量] simulated",
            )
        ):
            return "simulated"
        if "来源质量] failed" in normalized:
            return "failed"
        return "verified"

    @staticmethod
    def _feedback_success_hint(input_data: dict[str, Any]) -> bool:
        """Derive a conservative failure hint from a tool callback.

        The legacy path receives a rendered string in addition to the V13
        observation.  A string such as ``{"success": false}`` used to be
        treated as successful because it contained none of the human-facing
        error keywords.  Parse bounded structured envelopes when available
        and fail closed on explicit ``error``/``ok=false``/``success=false``;
        positive assertions still go through the normal verification policy.
        """
        text = str(input_data.get("text", "") or "")
        if any(
            keyword in text.casefold()
            for keyword in ("失败", "error", "错误", "exception", "traceback")
        ):
            return False

        candidates: list[Any] = []
        observation = input_data.get("tool_observation")
        if isinstance(observation, dict):
            candidates.append(observation)
            data = observation.get("data")
            if isinstance(data, dict):
                candidates.append(data)

        # Tool formatters commonly prefix a JSON object with a short label.
        # Decode the full text first, then bounded object fragments.  This is
        # only a failure hint; no parsed positive flag is used as proof.
        for raw in (text,):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    candidates.append(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            for marker in ("{", "["):
                start = raw.find(marker)
                if start < 0:
                    continue
                fragment = raw[start : start + 8000]
                try:
                    parsed = json.loads(fragment)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(parsed, dict):
                    candidates.append(parsed)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("error") not in (None, "", False):
                return False
            for key in ("success", "ok"):
                value = candidate.get(key)
                if isinstance(value, bool) and value is False:
                    return False
            quality = str(
                candidate.get("result_quality", candidate.get("quality", ""))
                or ""
            ).casefold()
            if quality in {"failed", "failure", "error"}:
                return False
        return True

    def _observe_autonomy_intent(self, intent: Intent) -> bool:
        """Attach a follow-up intent to the active episode.

        Returns True when the correlated follow-up was consumed, whether it
        schedules another action or closes the episode.
        """
        if self.autonomy is None or not self.autonomy.is_active:
            return False
        active = self.autonomy.active
        if active is None:
            return False
        if intent.episode_id and intent.episode_id != active.id:
            # A follow-up produced for another causal lane must never mutate
            # the currently active episode.
            return False
        if intent.goal_id and intent.goal_id != active.goal_id:
            return False
        # Tool feedback may come from an older bridge that did not carry the
        # correlation field.  In that case the single active episode is the
        # only safe fallback.
        if not intent.episode_id:
            intent.episode_id = active.id
        if not intent.goal_id:
            intent.goal_id = active.goal_id
        if not intent.origin or intent.origin == "external":
            intent.origin = active.origin or "autonomous"
        if intent.type == IntentType.CALL_TOOL:
            execution = self.task_execution
            followup_plan_key = (
                getattr(active, "plan_id", "")
                or getattr(active, "task_id", "")
            )
            plan = self._execution_plan_for_goal(active.goal_id, followup_plan_key)
            # A language-model follow-up cannot choose a new tool outside a
            # bounded plan.  If any V13 correlation is present, a missing
            # plan is itself a hard failure rather than permission to fall
            # back to the legacy free-form path.
            plan_backed = bool(
                execution is not None
                and (
                    plan is not None
                    or getattr(active, "task_id", "")
                    or getattr(active, "plan_id", "")
                    or getattr(intent, "plan_id", None)
                    or getattr(intent, "step_id", None)
                    or getattr(intent, "action_id", None)
                )
            )
            if plan_backed:
                if plan is None:
                    return False
                # A follow-up describes the next step, not an arbitrary
                # action chosen by the model.  Reject caller-supplied IDs
                # before mutating the ledger; the fresh IDs are assigned only
                # after the deterministic step has been claimed.
                if intent.plan_id and intent.plan_id != plan.id:
                    return False
                if intent.step_id or intent.action_id:
                    return False

                claim_state = self._capture_execution_claim_state(plan)
                try:
                    action_ids_before = {
                        str(key) for key in getattr(execution, "actions", {}).keys()
                    }
                except Exception:
                    action_ids_before = set()
                # ``autonomy.plan`` is the second half of this hand-off and
                # mutates the live episode.  Keep a small before-image too;
                # otherwise a ledger action could be rolled back while the
                # episode remains AWAITING_ACTION with a dead correlation.
                autonomy_fields = (
                    "status", "action_type", "tool_name", "intent_id",
                    "expected", "attempt_no", "step_count", "plan_id",
                    "step_id", "action_id", "success", "outcome",
                    "last_feedback", "result_summary", "result_quality",
                )
                autonomy_before = {
                    field: getattr(active, field, None) for field in autonomy_fields
                }
                autonomy_events_before = len(getattr(active, "events", []) or [])
                step = None
                action = None

                def rollback(reason: str) -> None:
                    try:
                        self._rollback_execution_claim(
                            execution,
                            plan,
                            claim_state,
                            action_ids_before,
                            action=action,
                            step_id=str(getattr(step, "id", "") or ""),
                            reason=reason,
                        )
                    except Exception as exc:
                        self._record_loop_error("task_execution_followup_rollback", exc)
                    for field, value in autonomy_before.items():
                        try:
                            setattr(active, field, value)
                        except Exception:
                            pass
                    try:
                        if isinstance(active.events, list):
                            active.events = active.events[:autonomy_events_before]
                            append_event = getattr(self.autonomy, "_append_event", None)
                            if callable(append_event):
                                append_event(
                                    active,
                                    "followup_rolled_back",
                                    self.state.total_ticks,
                                    reason[:240],
                                )
                    except Exception as exc:
                        self._record_loop_error("autonomy_followup_rollback", exc)

                try:
                    step = execution.next_step(
                        plan_id=plan.id, current_tick=self.state.total_ticks
                    )
                    if step is None:
                        # ``next_step`` may discover a terminal, blocked, or
                        # dependency-deadlocked plan.  Close this exact
                        # episode so it cannot remain the scheduler's active
                        # lane forever; the helper only projects verified
                        # completion and otherwise pauses/aborts safely.
                        self._close_unqueued_execution_episode(
                            self._find_goal(active.goal_id), active, plan
                        )
                        return False
                    if intent.tool_name and step.tool_name and intent.tool_name != step.tool_name:
                        rollback("后续意图工具与计划步骤不一致，已回滚执行认领")
                        return False
                    goal = self._find_goal(active.goal_id)
                    if goal is None:
                        rollback("后续意图所属目标不存在，已回滚执行认领")
                        return False
                    deterministic_intent = self._goal_to_intent(
                        goal,
                        episode_id=active.id,
                        attempt_no=getattr(goal, "attempt_count", 0),
                        step=step,
                        plan_id=plan.id,
                        step_id=step.id,
                    )
                    if deterministic_intent is None:
                        rollback("无法构造后续工具意图，已回滚执行认领")
                        return False
                    # Retain only the model's short reason.  Tool and
                    # arguments come from the bounded plan, never from the
                    # untrusted follow-up payload.
                    deterministic_intent.reason = (
                        intent.reason[:200] or deterministic_intent.reason
                    )
                    intent.type = deterministic_intent.type
                    intent.tool_name = deterministic_intent.tool_name
                    intent.tool_args = deterministic_intent.tool_args
                    intent.plan_id = plan.id
                    intent.step_id = step.id
                    intent.goal_id = active.goal_id
                    intent.episode_id = active.id
                    action = execution.record_action(
                        plan.id,
                        step.id,
                        action_type=intent.type.value,
                        tool_name=intent.tool_name or step.tool_name,
                        args=intent.tool_args,
                        expected=step.expected,
                        tick=self.state.total_ticks,
                        episode_id=active.id,
                        attempt_no=intent.attempt_no,
                    )
                    if action is None:
                        rollback("执行账本无法记录后续行动，已回滚执行认领")
                        return False
                    intent.action_id = action.id
                    planned = self.autonomy.plan(
                        episode_id=active.id,
                        action_type=intent.type.value,
                        tool_name=intent.tool_name or "",
                        tick=self.state.total_ticks,
                        reason=intent.reason,
                        intent_id=intent.intent_id,
                        expected=step.expected or intent.reason[:240],
                        attempt_no=intent.attempt_no,
                        plan_id=plan.id,
                        step_id=step.id,
                        action_id=action.id,
                    )
                    if not planned:
                        rollback("自主经历无法登记后续步骤，已回滚执行认领")
                        return False
                    binder = getattr(self.autonomy, "bind_action", None)
                    if callable(binder) and not binder(
                        active.id,
                        plan_id=plan.id,
                        step_id=step.id,
                        action_id=action.id,
                        tick=self.state.total_ticks,
                    ):
                        rollback("自主经历无法绑定后续行动，已回滚执行认领")
                        return False
                except Exception as exc:
                    self._record_loop_error("task_execution_followup", exc)
                    rollback("后续执行步骤交接异常，已回滚执行认领")
                    return False
                self._sync_autonomy_projection()
                return True

            # Compatibility path for episodes created before V13.  It still
            # requires the autonomy correlation checks above, but has no
            # ledger action to claim.
            try:
                planned = self.autonomy.plan(
                    episode_id=active.id,
                    action_type=intent.type.value,
                    tool_name=intent.tool_name or "",
                    tick=self.state.total_ticks,
                    reason=intent.reason,
                    intent_id=intent.intent_id,
                    expected=intent.reason[:240],
                    attempt_no=intent.attempt_no,
                )
            except Exception as exc:
                self._record_loop_error("autonomy_followup_plan", exc)
                planned = False
            self._sync_autonomy_projection()
            return planned
        if intent.type in {
            IntentType.RESPOND,
            IntentType.THINK,
            IntentType.ASK_QUESTION,
        } and active.status == EpisodeStatus.FEEDBACK_RECEIVED:
            outcome = (
                intent.response_text
                or intent.thought
                or intent.question
                or "反馈已吸收"
            )
            success = active.success is not False
            record = (
                self.autonomy.complete(outcome[:300], self.state.total_ticks)
                if success
                else self.autonomy.fail(outcome[:300], self.state.total_ticks)
            )
            self._finish_autonomy_episode(record, success, outcome)
            return True
        return False

    def _restore_snapshot(self, snapshot: dict) -> None:
        """Restore the durable parts of the consciousness runtime.

        Snapshots are an interoperability boundary: they may have been
        written by an older release or be partially damaged after a power
        loss.  The core state is decoded first, and each optional subsystem
        is restored independently so one bad component cannot erase the rest
        of the subject's continuity.
        """
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object")

        version = snapshot.get("schema_version", 1)
        if isinstance(version, (int, float)) and version > SNAPSHOT_SCHEMA_VERSION:
            logger.warning(
                "brain-stem: snapshot schema %s is newer than supported %s; best-effort restore",
                version,
                SNAPSHOT_SCHEMA_VERSION,
            )

        # BrainState has its own defensive decoder and is the source of truth
        # for self-model, curiosity, activation, and per-source sessions.
        self.state = BrainState.from_snapshot(snapshot)
        # Restore only hash-addressed P3 evidence.  Host capabilities,
        # evaluator instances and candidate paths are intentionally never
        # reconstructed from a snapshot; every later evaluation/promotion
        # must receive a fresh explicit host binding.
        self._restore_iteration_pipeline(snapshot)
        # Restore the constitutional seam separately from mutable cognition.
        # A malformed life snapshot is never downgraded to a fresh identity;
        # ``_restore_life`` marks the stem blocked and raises instead.
        self._restore_life(snapshot)
        self._restore_succession_runtime(snapshot)
        # A controlled environment is an authority boundary, not ordinary
        # mutable cognition.  Its root and network policy must be supplied by
        # the host on every restart; a snapshot may restore only the
        # hash-chained audit trail under that already-bound policy.  Refusing
        # to invent an environment here prevents a tampered snapshot from
        # redirecting the organism to an arbitrary host directory or enabling
        # networking after restart.
        raw_environment = snapshot.get("controlled_environment")
        if raw_environment is not None:
            if not isinstance(raw_environment, dict):
                self._mark_life_restore_blocked(
                    "controlled environment snapshot is invalid"
                )
                raise LifecycleError("controlled environment restore is blocked")
            configured_environment = self.controlled_environment
            if configured_environment is None:
                self._mark_life_restore_blocked(
                    "controlled environment must be injected before restore"
                )
                raise LifecycleError(
                    "controlled environment must be injected before restore"
                )
            raw_kind = str(raw_environment.get("kind", "")).strip().lower()
            raw_network = bool(raw_environment.get("network_enabled", False))
            if (
                raw_kind != configured_environment.kind.value
                or raw_network != configured_environment.network_enabled
            ):
                self._mark_life_restore_blocked(
                    "controlled environment policy differs from host binding"
                )
                raise LifecycleError(
                    "controlled environment policy differs from host binding"
                )
            try:
                self.controlled_environment = ControlledEnvironment.from_snapshot(
                    raw_environment,
                    approval_validator=configured_environment.approval_validator,
                    expected_root=configured_environment.root,
                )
            except Exception as exc:
                self._mark_life_restore_blocked(exc)
                raise LifecycleError(
                    f"controlled environment restore is blocked: {str(exc)[:240]}"
                ) from exc
        # Resource accounting is a safety boundary rather than an optional
        # cosmetic component: if a present homeostasis ledger is malformed,
        # fail closed instead of replacing it with a fresh budget.
        raw_homeostasis = snapshot.get("homeostasis")
        if isinstance(raw_homeostasis, dict) and {
            "budget", "ledger"
        }.issubset(raw_homeostasis):
            try:
                restored_homeostasis = HomeostasisController.from_snapshot(
                    raw_homeostasis
                )
            except Exception as exc:
                self._mark_life_restore_blocked(exc)
                raise LifecycleError(
                    f"homeostasis restore is blocked: {str(exc)[:240]}"
                ) from exc
            self.homeostasis = restored_homeostasis
            self.homeostasis_controller = restored_homeostasis
        # Motivation is a signal layer.  A damaged signal history may be
        # discarded without granting authority, but the incident remains
        # visible in the normal loop-error projection.
        raw_motivation = snapshot.get("motivation")
        if isinstance(raw_motivation, dict):
            try:
                # Preserve the host-bound provenance policy and attestor.  A
                # snapshot is data only; it cannot downgrade a production
                # stem to caller-forgeable source labels or mint a verifier.
                self.motivation = MotivationalPressure.from_snapshot(
                    raw_motivation,
                    source_attestor=self._motivation_source_attestor,
                    source_verifier=self._motivation_source_verifier,
                    require_source_attestation=self._motivation_require_source_attestation,
                )
                self.motivational_pressure = self.motivation
            except Exception as exc:
                self._record_loop_error("motivation_restore", exc)
                self.motivation = MotivationalPressure(
                    source_attestor=self._motivation_source_attestor,
                    source_verifier=self._motivation_source_verifier,
                    require_source_attestation=self._motivation_require_source_attestation,
                )
                self.motivational_pressure = self.motivation
        self._sync_life_projection()
        self._sync_self_maintenance_projection()

        raw_wm = snapshot.get("working_memory")
        if isinstance(raw_wm, dict):
            self.working_memory = WorkingMemory.from_snapshot(raw_wm)
        elif self.state.active_thoughts:
            # v1 snapshots exposed active thoughts as a bare list.
            self.working_memory = WorkingMemory.from_snapshot({
                "items": self.state.active_thoughts,
                "context_text": self.state.current_context,
            })
        self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
        if not self.state.current_context:
            self.state.current_context = self.working_memory.get_context()

        valid_sleep_states = {"awake", "drowsy", "light_sleep", "deep_sleep"}
        self.sleep_state = (
            self.state.sleep_state
            if self.state.sleep_state in valid_sleep_states
            else "awake"
        )

        def _restore_component(attribute: str, key: str, decoder):
            raw = snapshot.get(key)
            if raw is None:
                return
            try:
                restored = decoder(raw)
                if restored is not None:
                    setattr(self, attribute, restored)
            except Exception as exc:
                self._record_loop_error(f"restore_{key}", exc)
                logger.warning("brain-stem: component restore skipped (%s): %s", key, str(exc)[:120])

        _restore_component("goal_system", "goal_system", GoalSystem.from_snapshot)
        _restore_component(
            "task_scheduler",
            "task_scheduler",
            LongTermTaskScheduler.from_snapshot,
        )
        _restore_component("metacognition", "metacognition", Metacognition.from_snapshot)
        _restore_component("emotional_spectrum", "emotional_spectrum", EmotionalSpectrum.from_snapshot)
        _restore_component("procedural_memory", "procedural_memory", ProceduralMemory.from_snapshot)
        _restore_component("time_sense", "time_sense", TimeSense.from_snapshot)
        _restore_component("exploration_queue", "exploration_queue", ExplorationQueue.from_snapshot)
        _restore_component("reflection_engine", "reflection_engine", ReflectionEngine.from_snapshot)
        _restore_component("drive_engine", "drive_engine", DriveEngine.from_snapshot)
        if self.autonomy is not None and isinstance(snapshot.get("autonomy"), dict):
            _restore_component("autonomy", "autonomy", AutonomyEpisode.from_snapshot)
            self._sync_autonomy_projection()
        if self.task_execution is not None and isinstance(snapshot.get("task_execution"), dict):
            _restore_component("task_execution", "task_execution", TaskExecutionLedger.from_snapshot)
            try:
                self.task_execution.require_verified_completion = bool(
                    TASK_EXECUTION_REQUIRE_VERIFIED_COMPLETION
                )
                self.task_execution.auto_replan = bool(TASK_EXECUTION_AUTO_REPLAN)
            except Exception:
                pass
        if self.learning_feedback is not None and isinstance(snapshot.get("learning_feedback"), dict):
            _restore_component(
                "learning_feedback", "learning_feedback", VerifiedLearningFeedback.from_snapshot
            )

        raw_thalamus = snapshot.get("thalamus", {})
        if isinstance(raw_thalamus, dict):
            self.thalamus.last_input = str(raw_thalamus.get("last_input", ""))
            try:
                self.thalamus.noise_discarded = max(0, int(raw_thalamus.get("noise_discarded", 0)))
                self.thalamus.total_relayed = max(0, int(raw_thalamus.get("total_relayed", 0)))
            except (TypeError, ValueError):
                pass
        raw_amygdala = snapshot.get("amygdala", {})
        if isinstance(raw_amygdala, dict):
            for name in ("valence", "arousal", "dominance", "salience"):
                try:
                    setattr(self.amygdala, name, float(raw_amygdala.get(name, getattr(self.amygdala, name))))
                except (TypeError, ValueError):
                    pass

        # Optional V9/V10 regions are only restored when enabled by the
        # current configuration; a snapshot must not silently turn features
        # back on after an operator disabled them.
        if self.predictive_layer is not None:
            _restore_component("predictive_layer", "predictive_layer", PredictiveLayer.from_snapshot)
        if self.cognitive_dispatch is not None:
            _restore_component("cognitive_dispatch", "cognitive_dispatch", CognitiveDispatch.from_snapshot)
        if self.boredom_engine is not None:
            _restore_component("boredom_engine", "boredom_engine", BoredomEngine.from_snapshot)
        if self.social_emotion is not None:
            _restore_component("social_emotion", "social_emotion", SocialEmotionEngine.from_snapshot)
        if self.attachment_system is not None:
            _restore_component("attachment_system", "attachment_system", AttachmentSystem.from_snapshot)
        if self.reward_system is not None:
            _restore_component("reward_system", "reward_system", RewardSystem.from_snapshot)
        if self.autobiography is not None:
            _restore_component("autobiography", "autobiography", AutobiographicalNarrative.from_snapshot)
        if self.boundary is not None:
            _restore_component("boundary", "boundary", BoundaryEngine.from_snapshot)

        if isinstance(snapshot.get("exploration_executor"), dict):
            try:
                self.exploration_executor = ExplorationExecutor.from_snapshot(
                    snapshot["exploration_executor"]
                )
            except Exception as exc:
                self._record_loop_error("restore_exploration_executor", exc)

        # Scheduling timestamps are wall-clock values.  Restore them only
        # when valid; a missing value simply causes the corresponding task to
        # run on its next eligible cycle.
        maintenance = snapshot.get("maintenance", {})
        if not isinstance(maintenance, dict):
            maintenance = {}

        def _timestamp(name: str) -> float:
            value = maintenance.get(name, snapshot.get(name, 0.0))
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return 0.0

        self.last_dream_time = _timestamp("last_dream_time")
        self.last_consolidation_time = _timestamp("last_consolidation_time")
        self.last_reflection = _timestamp("last_reflection")
        self.last_snapshot = _timestamp("last_snapshot")
        self.last_decay = _timestamp("last_decay")
        try:
            self._last_archived_count = max(0, int(snapshot.get("last_archived_count", 0)))
        except (TypeError, ValueError):
            self._last_archived_count = 0
        try:
            self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
        except Exception as exc:
            self._record_loop_error("restore_task_scheduler", exc)

        # Recovery is ordered deliberately: first cancel ambiguous ledger
        # actions, then project any resulting PAUSED/BLOCKED plan onto its
        # owning Goal, and only then let the scheduler normalize its running
        # pointer.  If scheduler.sync runs only before ledger recovery, a
        # snapshot with an ACTIVE goal can retain ``running_goal_id`` even
        # though its in-flight action has just been cancelled; that stale
        # pointer blocks the next safe task and can invite a replay.
        if self.task_execution is not None:
            execution_before = {
                plan.id: (plan.status, plan.active_step_id)
                for plan in getattr(self.task_execution, "plans", {}).values()
            }
            try:
                self.task_execution.recover_inflight(
                    current_tick=self.state.total_ticks
                )
            except Exception as exc:
                self._record_loop_error("restore_task_execution", exc)
            for plan in list(
                getattr(self.task_execution, "plans", {}).values()
            ):
                goal = self._find_goal(getattr(plan, "goal_id", ""))
                if goal is None:
                    continue
                previous = execution_before.get(plan.id)
                changed = previous is None or previous != (
                    plan.status,
                    plan.active_step_id,
                )
                # A plan may already have been persisted as paused while the
                # legacy Goal was still ACTIVE (for example a crash between
                # the two component snapshots).  Project that mismatch too,
                # not just transitions observed during this recovery pass.
                needs_projection = changed or (
                    plan.status
                    in {
                        PlanStatus.PAUSED,
                        PlanStatus.BLOCKED,
                        PlanStatus.FAILED,
                        PlanStatus.COMPLETED,
                    }
                    and getattr(goal, "status", "")
                    in {
                        GoalStatus.PENDING,
                        GoalStatus.ACTIVE,
                    }
                )
                if needs_projection:
                    try:
                        self._sync_goal_with_plan(
                            goal,
                            plan,
                            "重启恢复后未确认行动已取消，计划需显式恢复",
                        )
                    except Exception as exc:
                        self._record_loop_error(
                            "restore_task_execution_projection", exc
                        )
            # The projection above clears a matching running pointer when a
            # goal is paused/terminal.  A second pure normalization pass also
            # handles malformed snapshots where no Goal object was available
            # for projection.
            try:
                self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
            except Exception as exc:
                self._record_loop_error("restore_task_scheduler_post_execution", exc)

        self._sync_life_projection()

    async def _tick(self):
        """One tick of consciousness. V6: ActivationField drives state dynamics."""

        # A tick owns at most one queued request.  The ID is used by the
        # per-request future so an exception can be reported to the correct
        # caller instead of waking every caller at once.
        self._active_input_id = None
        autonomy_feedback_seen = False
        autonomy_feedback_rejected = False
        autonomy_followup_intent = False
        # A tool-feedback intent may be consumed by the correlated autonomy
        # lane.  It must not also enter the generic intent queue, otherwise a
        # stale response/call_tool intent can be mistaken for the next plan
        # step (and can leak an uncorrelated action to the agent bridge).
        autonomy_intent_consumed = False
        # Tool feedback is an untrusted observation boundary.  Until the
        # correlated autonomy lane proves a valid follow-up, no intent parsed
        # from that text may enter the generic agent queue.
        autonomy_intent_queueable = True
        autonomy_feedback_success = False
        autonomy_feedback_tool = ""
        self._last_execution_outcome = None

        if self.autonomy:
            expired = self.autonomy.tick(self.state.total_ticks)
            if expired:
                self._finish_autonomy_episode(
                    expired,
                    False,
                    expired.outcome or "自主经历超时",
                )

        # ── V6: ActivationField tick — 状态扩散 + 基线回归 ──
        activation = self.state.activation
        activation.tick(dt=1.0)
        self.state.last_tick = datetime.now(timezone.utc).isoformat()

        # ── Step 0: Sleep state management ──
        was_asleep = self.sleep_state != "awake"
        self._update_sleep_state()
        if self.sleep_state != "awake" and not was_asleep:
            logger.info("brain-stem: entering %s", self.sleep_state)
        elif self.sleep_state == "awake" and was_asleep:
            logger.info("brain-stem: waking up")
            self.working_memory.push("Waking up. Resuming consciousness.", "brain_stem", 0.6)

        # ── Step 1: Check for input ──
        input_data = None
        previous_ticks_since_input = self.state.ticks_since_input
        previous_sleep_state = self.sleep_state
        try:
            input_data = self._pending_input.get_nowait()
            self._active_input_id = input_data.get("request_id")
            # Record the correlation metadata immediately, but defer session,
            # emotion, and wake-up changes until the boundary has accepted the
            # message.  A refused input must not be able to perturb the active
            # subject state merely by reaching the queue.
            self.state.last_input_id = self._active_input_id or ""
            self.state.last_input_source = input_data.get("source", "external")
            # An intent belongs to the current input.  Clear the previous one
            # before processing so a gated/failed input cannot return stale
            # output from an earlier turn.
            self.state.last_intent = None
            self.state.last_error = ""
            self.state.last_retrieved = []
            self.state.association_chain = []
        except asyncio.QueueEmpty:
            self.state.ticks_since_input += 1
            input_data = None

        # ── Step 2: Thalamus — sensory relay ──
        inner_signal = self.default_mode.get_recent_thoughts(1)
        inner_text = inner_signal[0] if inner_signal else None

        thalamus_out = self.thalamus.relay(
            input_text=input_data["text"] if input_data else None,
            inner_signal=inner_text,
            source=input_data.get("source", "external") if input_data else "internal",
        )

        # Apply the self-boundary immediately after sensory normalization.  A
        # refused message must not alter emotional state, prediction history,
        # or long-term-memory access counters.
        boundary_accepted = True
        boundary_reason = "accepted"
        if self.boundary and input_data and thalamus_out.get("has_input"):
            boundary_accepted, boundary_reason = self.boundary.should_accept_input(
                source=thalamus_out.get("source", input_data.get("source", "external")),
                text=thalamus_out.get("text", ""),
                cognitive_load=self.metacognition.cognitive_load,
                attachment_system=self.attachment_system,
            )

        refused_input = bool(
            input_data and thalamus_out.get("has_input") and not boundary_accepted
        )

        # Register tool feedback before committing any per-source context or
        # running cognition.  A callback that carries autonomous correlation
        # metadata (or arrives while an autonomous episode is active) is
        # treated as a quarantined input when the causal check rejects it.
        # This is stronger than merely refusing to close the ledger: rejected
        # feedback must not train prediction, emotion, habits, memory, or
        # reward state through the ordinary input pipeline either.
        if input_data and not refused_input and str(
            input_data.get("source", "")
        ).startswith("agent/tool/"):
            autonomy_feedback_candidate = bool(
                any(input_data.get(key) for key in (
                    "episode_id", "intent_id", "goal_id", "plan_id",
                    "step_id", "action_id",
                ))
                or (self.autonomy is not None and self.autonomy.is_active)
            )
            autonomy_feedback_success = self._feedback_success_hint(input_data)
            autonomy_feedback_tool = str(input_data.get("source", ""))[len("agent/tool/"):]
            autonomy_feedback_seen = self._record_autonomy_feedback(
                input_data,
                autonomy_feedback_success,
                autonomy_feedback_tool,
            )
            autonomy_feedback_rejected = bool(
                autonomy_feedback_candidate and not autonomy_feedback_seen
            )
            if autonomy_feedback_rejected:
                # Reuse the established refused-input guards below so this
                # untrusted callback is acknowledged and its waiter resolved,
                # but cannot become a learning event for another causal lane.
                refused_input = True
                boundary_reason = "自主反馈关联校验失败"

        # A new accepted subject input preempts an in-flight autonomous action;
        # a rejected message does not get to rewrite the episode timeline.
        if (
            input_data
            and not refused_input
            and self.autonomy
            and self.autonomy.is_active
            and not str(input_data.get("source", "")).startswith("agent/")
        ):
            interrupted = self.autonomy.abort(
                "外部输入打断自主经历",
                self.state.total_ticks,
            )
            self._finish_autonomy_episode(interrupted, False, "外部输入打断")

        # Commit per-source context only after the input boundary has passed.
        # Rejected traffic remains observable as a boundary event, but cannot
        # switch the active session, wake the subject, or reset its idle clock.
        if input_data and not refused_input:
            self.state.ticks_since_input = 0
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            session.last_active = datetime.now(timezone.utc).isoformat()
            # Replace, rather than update, the shared view.  Updating left
            # custom keys from the previous source alive when sessions used
            # different emotion dimensions, causing cross-session leakage.
            self.state.emotion_vector = dict(session.emotion_vector)
            self.state.current_emotion = session.current_emotion
            self.state.focus_entity = session.focus_entity
            self.state.inner_monologue = session.inner_monologue
            self.state.current_goal = input_data.get("goal") or None
            # Always replace the shared working-memory view, including with an
            # empty list.  Only copying non-empty sessions allowed source A's
            # context to bleed into a newly activated source B.
            self.working_memory.items = [dict(it) for it in session.working_memory.items]
            self.working_memory.context_text = session.working_memory.context_text
            explicit_goal = input_data.get("goal")
            if (
                explicit_goal
                and not str(input_data.get("source", "")).startswith("agent/")
            ):
                user_goal = self.task_scheduler.submit_user_goal(
                    self.goal_system,
                    str(explicit_goal),
                    current_tick=self.state.total_ticks,
                    source=str(input_data.get("source", "user")),
                )
                if user_goal is not None:
                    if self.task_execution is not None:
                        try:
                            self.task_execution.ensure_plan_for_goal(
                                user_goal, current_tick=self.state.total_ticks
                            )
                        except Exception as exc:
                            self._record_loop_error("task_execution_plan", exc)
                    self.working_memory.push(
                        content="[用户任务] {0}".format(user_goal.description[:120]),
                        source="task_scheduler",
                        base_salience=0.65,
                    )
                else:
                    self.working_memory.push(
                        content="[任务队列] 已满，未接收新的用户任务",
                        source="task_scheduler",
                        base_salience=0.55,
                    )
            if self.sleep_state != "awake":
                logger.info("brain-stem: input received, waking from %s", self.sleep_state)
                self.sleep_state = "awake"
                self.state.sleep_state = self.sleep_state
        elif refused_input:
            self.state.ticks_since_input = previous_ticks_since_input + 1
            self.sleep_state = previous_sleep_state
            self.state.sleep_state = self.sleep_state

        # ── V9 Predictive Layer: 在感知之前生成预测 ──
        # Build an expectation only for an accepted message.  A rejected
        # message must not train or mutate the predictive subsystem.
        if self.predictive_layer and input_data and not refused_input:
            wm_entities = [
                item.get("content", "")[:30]
                for item in self.working_memory.items[-3:]
            ]
            self.predictive_layer.build_expectation(
                current_tick=self.state.total_ticks,
                entities_from_wm=wm_entities,
                time_sense=self.time_sense,
            )

        # ── Step 3: Amygdala — emotion ──
        if refused_input:
            # Do not call Amygdala with an empty string: that path applies
            # emotion decay and changes short-term state for an input the
            # subject explicitly refused.  Keep a neutral event envelope for
            # downstream formatting without mutating the live values.
            amygdala_out = {
                "emotion_label": self.state.current_emotion,
                "emotion_vector": dict(self.state.emotion_vector),
                "emotional_tags": [],
                "salience": self.amygdala.salience,
            }
        elif thalamus_out["has_input"] and not thalamus_out["discarded"]:
            amygdala_out = self.amygdala.evaluate(
                text=thalamus_out["text"],
                current_state={"current_emotion": self.state.current_emotion, "emotion_vector": self.state.emotion_vector},
                activation=activation,  # V6
            )
            self.state.emotion_vector.update(amygdala_out["emotion_vector"])
            self.state.current_emotion = amygdala_out["emotion_label"]
        else:
            # No input — decay emotion
            amygdala_out = self.amygdala.evaluate(
                text="",
                current_state={"current_emotion": self.state.current_emotion, "emotion_vector": self.state.emotion_vector},
            )
            self.state.emotion_vector.update(amygdala_out["emotion_vector"])

        self.state.emotion_history.append({
            "tick": self.state.total_ticks,
            "emotion": self.state.current_emotion,
            "vector": dict(self.state.emotion_vector),
        })
        self.state.emotion_history = self.state.emotion_history[-50:]

        # ── V9 Predictive Layer: 计算预测误差，surprise → salience boost ──
        surprise_salience = 0.0
        if (
            self.predictive_layer
            and boundary_accepted
            and input_data
            and thalamus_out["has_input"]
            and not refused_input
        ):
            emotion_vec = amygdala_out.get("emotion_vector", {})
            error = self.predictive_layer.observe_and_compute(
                expectation=self.predictive_layer.last_expectation,
                input_data=input_data,
                emotion_vector=emotion_vec,
                ticks_since_input=self.state.ticks_since_input,
            )
            if error.is_surprising:
                # salience 不再只依赖 LLM — 系统自己感受到惊讶
                surprise_salience = self.predictive_layer.salience_from_surprise
                amygdala_out["salience"] = max(
                    amygdala_out.get("salience", 0.0),
                    surprise_salience,
                )
                # 惊讶事件写入工作记忆
                _ = self.predictive_layer.handle_surprise(
                    error=error,
                    working_memory=self.working_memory,
                    curiosity=self.state.curiosity,
                    hippocampus=self.hippocampus,
                    exploration_queue=self.exploration_queue,
                )
                logger.debug("brain-stem: predictive error=%.3f salience_boost=%.3f",
                           error.total_error, surprise_salience)

        # ── Step 4-8: LLM Processing (V9 dispatch or legacy unified) ──
        hippocampus_out = None
        encoded_memory = None
        input_source = thalamus_out.get("source", "none")
        # All non-internal sources are externally supplied from the brain's
        # point of view (creator/user/tool are distinct for policy, but none
        # should be silently downgraded to the legacy literal "external").
        is_external = bool(input_data) and input_source not in {"internal", "none"}
        is_subject_input = is_external and not input_source.startswith("agent/")
        dmn_out = None

        # Reset input tracking at start of each tick
        self.state.last_input_accepted = False
        self.state.last_input_gated = True
        
        gate = {"passed": False}
        if thalamus_out["has_input"] and not thalamus_out["discarded"]:
            if refused_input:
                # Keep only a minimal, non-content-bearing audit marker.  The
                # rejected text itself must not enter working memory.
                self.working_memory.push(
                    content=f"[边界] 已拒绝来自 {thalamus_out['source']} 的输入（原因: {boundary_reason}）",
                    source="boundary",
                    base_salience=0.25,
                )
            else:
                # Do not touch long-term memory until the input boundary has
                # accepted the message.  Retrieval can update access counters
                # and expose hit counts, so doing it first leaked side effects
                # from a request the subject had already refused.
                hippocampus_out = await self._bounded_cognitive_await(
                    self.hippocampus.retrieve(
                        query=thalamus_out["text"],
                        top_k=5,
                    ),
                    "hippocampus.retrieve",
                    {"results": [], "total_stored": 0},
                )
                if not isinstance(hippocampus_out, dict):
                    hippocampus_out = {"results": [], "total_stored": 0}
                self.state.last_retrieved = [
                    str(item.get("id", item.get("title", "")))
                    for item in hippocampus_out.get("results", [])
                    if isinstance(item, dict)
                ][-20:]

                # Gate check: should we process this?
                attn_boost = self.state.self_model.attention_weight(thalamus_out["text"])
                attn_importance = min(0.5 + attn_boost * 0.1, 1.0)

                gate = gate_check(
                    text=thalamus_out["text"],
                    importance=attn_importance,
                    novelty=0.5,
                    goal_relevance=GATE_GOAL_RELEVANCE_WITH_GOAL if self.state.current_goal else GATE_GOAL_RELEVANCE_DEFAULT,
                    explicit_mark=amygdala_out.get("salience", 0) > 0.7,
                )

            # Track gate result for API response (v5.0).  Refused input stays
            # gated without invoking the regular attention gate.
            self.state.last_input_gated = not gate["passed"]
            self.state.last_input_accepted = gate["passed"]
            # ``last_error`` was cleared at the input boundary above.  Keep
            # any error recorded while preparing this same request (for
            # example a bounded hippocampus timeout) so the health snapshot
            # and long-running metrics do not erase the evidence immediately.

            # LLM Processing: encoding + focus + monologue + intent
            # V9: 多通道认知调度；V5-V8: 统一单次调用
            if is_external and gate["passed"]:
                try:
                    from services.llm_client import get_llm
                    llm = get_llm()

                    # ── Build shared context (both paths need this) ──
                    tools_summary = ""
                    try:
                        from agent.tool_registry import registry as tool_reg
                        tools = tool_reg.get_enabled()
                        if tools:
                            tools_list = []
                            for t in tools[:10]:
                                ro = "只读" if t.is_read_only else "读写"
                                tools_list.append(f"  {t.emoji} {t.name} [{ro}]: {t.description[:120]}")
                            tools_summary = "\n".join(tools_list)
                    except Exception:
                        pass

                    id_mems = self.memory_store.get_identity_memories(5) if self.memory_store else []
                    id_context = self.state.self_model.identity_memories_context(id_mems, max_items=3)
                    temporal_ctx = self.time_sense.get_short_temporal_context()
                    skill_hint = self.procedural_memory.get_skill_suggestion(thalamus_out["text"])

                    # ── V9: Cognitive Dispatch (多通道) vs Legacy Unified (单次调用) ──
                    if self.cognitive_dispatch:
                        # 使用多通道认知调度器
                        cog_result = await self._bounded_cognitive_await(
                            self.cognitive_dispatch.dispatch(
                                text=thalamus_out["text"],
                                source=thalamus_out["source"],
                                goal=self.state.current_goal,
                                current_emotion={
                                    "valence": self.emotional_spectrum.valence,
                                    "arousal": self.emotional_spectrum.arousal,
                                    "dominance": self.emotional_spectrum.dominance,
                                },
                                recent_thoughts=self.working_memory.get_context()[:300],
                                identity_anchor=self.state.self_model.identity_anchor[:300],
                                identity_memories_context=id_context,
                                top_drives=[
                                    d["label"] for d in self.state.self_model.get_top_drives(2)
                                ],
                                tools_summary=tools_summary or "",
                                temporal_context=temporal_ctx,
                                skill_hint=skill_hint or "",
                                llm_client=llm,
                            ),
                            "cognitive_dispatch",
                            None,
                        )
                        # 转换为与旧 unified 格式兼容的 dict.  A timeout
                        # yields a deterministic think-only envelope so the
                        # remainder of the tick can still commit state and
                        # resolve the caller's future.
                        unified = (
                            cog_result.to_unified_dict()
                            if cog_result is not None
                            else self._cognitive_timeout_fallback(
                                thalamus_out["text"]
                            )
                        )

                        # ── V9: 规则引擎情绪已在 dispatch 中计算，跳过 LLM 情绪摄入 ──
                        # 直接用规则引擎的 VAD 值（不做 LLM 情绪混合）
                        llm_emotion = unified.get("emotion", {})
                        if llm_emotion:
                            v = float(llm_emotion.get("valence", 0.5))
                            a = float(llm_emotion.get("arousal", 0.5))
                            d = float(llm_emotion.get("dominance", 0.5))
                            u = float(llm_emotion.get("urgency", 0.0))
                            label = llm_emotion.get("label", "neutral")

                            # V9: 规则引擎情绪权重低于 LLM 情绪但更高频更新
                            self.emotional_spectrum.ingest_llm_emotion(v, a, d, label, u, activation=activation)
                            # 同步杏仁核（使用与旧路径相同的混合逻辑）
                            prev_v = self.amygdala.valence
                            prev_a = self.amygdala.arousal
                            prev_d = self.amygdala.dominance
                            keep_ratio = 1.0 - LLM_EMOTION_BLEND_RATIO
                            self.amygdala.valence = prev_v * keep_ratio + v * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.arousal = prev_a * keep_ratio + a * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.dominance = prev_d * keep_ratio + d * LLM_EMOTION_BLEND_RATIO
                            self.amygdala.salience = a * 0.4 + u * 0.6

                            amygdala_out["emotion_label"] = label
                            amygdala_out["emotion_vector"] = {
                                "valence": round(self.emotional_spectrum.valence, 3),
                                "arousal": round(self.emotional_spectrum.arousal, 3),
                                "dominance": round(self.emotional_spectrum.dominance, 3),
                                "urgency": u,
                                "salience": round(self.amygdala.salience, 3),
                            }
                            amygdala_out["salience"] = self.amygdala.salience
                            self.state.emotion_vector.update(amygdala_out["emotion_vector"])
                            self.state.current_emotion = self.emotional_spectrum.dominant_emotion
                    else:
                        # ── Legacy: 统一 LLM 调用（V5-V8 路径）──
                        from services.llm_prompts import UNIFIED_TICK_PROMPT

                        ctx_obj = {
                            "text": thalamus_out["text"][:3000],
                            "goal": self.state.current_goal or "none",
                            "emotion": amygdala_out.get("emotion_label", "neutral"),
                            "salience": amygdala_out.get("salience", 0.0),
                            "recent_thoughts": self.working_memory.get_context()[:300],
                            "identity": self.state.self_model.identity_anchor[:300],
                            "identity_memories": id_context,
                            "top_drives": [
                                d["label"] for d in self.state.self_model.get_top_drives(2)
                            ],
                        }
                        if tools_summary:
                            ctx_obj["available_tools"] = tools_summary
                        ctx_obj["temporal_context"] = temporal_ctx
                        if skill_hint:
                            ctx_obj["skill_memory"] = skill_hint
                        ctx = json.dumps(ctx_obj, ensure_ascii=False)

                        unified = await self._bounded_cognitive_await(
                            llm.chat_json(
                                system=UNIFIED_TICK_PROMPT,
                                user=ctx,
                                temperature=0.1,
                                max_tokens=1024,
                            ),
                            "llm.chat_json",
                            self._cognitive_timeout_fallback(thalamus_out["text"]),
                        )
                        if not isinstance(unified, dict):
                            unified = self._cognitive_timeout_fallback(
                                thalamus_out["text"]
                            )

                    # ── LLM Emotion — v5.3: 情感光谱摄入（替代旧杏仁核混合）──
                    llm_emotion = unified.get("emotion", {})
                    if llm_emotion:
                        v = float(llm_emotion.get("valence", 0.5))
                        a = float(llm_emotion.get("arousal", 0.5))
                        d = float(llm_emotion.get("dominance", 0.5))
                        u = float(llm_emotion.get("urgency", 0.0))
                        label = llm_emotion.get("label", "neutral")

                        # v5.3: 情感光谱摄入（带动量平滑）→ V6: 写入 ActivationField
                        self.emotional_spectrum.ingest_llm_emotion(v, a, d, label, u, activation=activation)

                        # 向后兼容：同步杏仁核（旧系统）
                        prev_v = self.amygdala.valence
                        prev_a = self.amygdala.arousal
                        prev_d = self.amygdala.dominance
                        keep_ratio = 1.0 - LLM_EMOTION_BLEND_RATIO
                        self.amygdala.valence = prev_v * keep_ratio + v * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.arousal = prev_a * keep_ratio + a * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.dominance = prev_d * keep_ratio + d * LLM_EMOTION_BLEND_RATIO
                        self.amygdala.salience = a * 0.4 + u * 0.6

                        amygdala_out["emotion_label"] = label
                        amygdala_out["emotion_vector"] = {
                            "valence": round(self.emotional_spectrum.valence, 3),
                            "arousal": round(self.emotional_spectrum.arousal, 3),
                            "dominance": round(self.emotional_spectrum.dominance, 3),
                            "urgency": u,
                            "salience": round(self.amygdala.salience, 3),
                        }
                        amygdala_out["salience"] = self.amygdala.salience
                        self.state.emotion_vector.update(amygdala_out["emotion_vector"])
                        self.state.current_emotion = self.emotional_spectrum.dominant_emotion

                    # Encode
                    enc = unified.get("encoding", {})
                    raw_ents = enc.get("entities", [])
                    entities = [e for e in raw_ents if e and e in thalamus_out["text"]]
                    mem = {
                        "type": enc.get("type", "episodic"),
                        "title": enc.get("title", thalamus_out["text"][:80]),
                        "content": thalamus_out["text"][:4000],
                        "entities": entities,
                        "emotion_tags": amygdala_out.get("emotional_tags", []),
                        "importance": max(0.0, min(1.0, float(enc.get("importance", 0.5)))),
                        "summary": enc.get("summary", thalamus_out["text"][:80]),
                        "source": thalamus_out["source"],
                        "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                        "emotion_vector": amygdala_out.get("emotion_vector", {}),
                        "created": datetime.now(timezone.utc).isoformat(),
                        "llm_encoded": True,
                    }
                    if self.memory_store:
                        mem_id = self.memory_store.save(mem)
                        mem["id"] = mem_id
                    encoded_memory = mem

                    # ── Self-model: ingest this experience ──
                    if is_subject_input and mem.get("importance", 0) > 0:
                        shift = self.state.self_model.ingest_experience(
                            text=thalamus_out["text"][:500],
                            emotion={
                                "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                                "emotion_vector": amygdala_out.get("emotion_vector", {}),
                                "salience": amygdala_out.get("salience", 0.0),
                            },
                            importance=mem.get("importance", 0.5),
                            memory_count=self.memory_store.count() if self.memory_store else 0,
                        )

                        # ── Identity memory: two paths ──
                        # Path A: significance high → identity shift + fact ingestion
                        if shift and self.memory_store:
                            mem["is_identity_forming"] = True
                            mem["importance"] = max(mem["importance"], 0.75)
                            try:
                                self.memory_store.save(mem)
                            except Exception:
                                pass
                            # ingest fact
                            fact = (mem.get("summary", "") or mem.get("title", ""))[:120]
                            if fact and len(fact) > 3:
                                self.state.self_model.ingest_identity_fact(
                                    fact=fact, memory_id=mem.get("id", ""),
                                    confidence=mem.get("importance", 0.75))

                        # Path B: LLM marked as important (≥0.7) but significance < 0.3
                        # → still ingest identity fact (e.g. naming, relationship)
                        elif mem.get("importance", 0) >= 0.7 and self.memory_store:
                            mem["is_identity_forming"] = True
                            try:
                                self.memory_store.save(mem)
                            except Exception:
                                pass
                            fact = (mem.get("summary", "") or mem.get("title", ""))[:120]
                            if fact and len(fact) > 3:
                                self.state.self_model.ingest_identity_fact(
                                    fact=fact, memory_id=mem.get("id", ""),
                                    confidence=mem.get("importance", 0.7))

                        # ── Curiosity: check if this resolves any open questions ──
                        entities = mem.get("entities", [])
                        resolved = self.state.curiosity.check_resolution(thalamus_out["text"], entities)
                        if resolved:
                            for rq in resolved:
                                self.working_memory.push(
                                    content="问题已解答: {0}".format(rq["question"][:100]),
                                    source="curiosity",
                                    base_salience=0.6,
                                )

                        # ── Curiosity: update exploration topics ──
                        self.state.curiosity.update_exploration_topics(entities)

                        # ── V10 Social Self: 互动社会情感评估 ──
                        if self.social_emotion and self.attachment_system and is_subject_input:
                            source = thalamus_out["source"]
                            other = self.attachment_system.get_or_create(source)
                            sentiment = amygdala_out.get("emotion_vector", {}).get("valence", 0.5)
                            # 将当前体验的情感映射为"他们对我的态度"
                            perceived_sentiment = (sentiment - 0.5) * 1.5  # 放大
                            other.record_interaction(
                                sentiment=perceived_sentiment,
                                impression=thalamus_out["text"][:80],
                            )
                            _ = self.social_emotion.evaluate_interaction(
                                self_model=self.state.self_model,
                                other=other,
                                my_action="",
                                their_response=thalamus_out["text"][:80],
                                their_sentiment=perceived_sentiment,
                                was_ignored=False,
                            )

                        # ── V10 Autobiographical: 转折点检测 ──
                        if self.autobiography and is_subject_input and mem.get("importance", 0) > 0:
                            tp = self.autobiography.detect_turning_point(
                                experience={
                                    "significance": shift.get("significance", 0) if shift else 0,
                                    "importance": mem.get("importance", 0),
                                    "text_snippet": thalamus_out["text"][:100],
                                    "emotion_label": amygdala_out.get("emotion_label", "neutral"),
                                    "reflection": shift.get("reflection", "") if shift else "",
                                },
                                identity_shift=shift if shift else None,
                                emotion_vector=amygdala_out.get("emotion_vector", {}),
                                current_tick=self.state.total_ticks,
                            )
                            if tp:
                                # 里程碑 → 奖励
                                if self.reward_system:
                                    self.reward_system.deliver_reward(
                                        channel="cognitive",
                                        actual_reward=0.8,
                                        context="turning_point",
                                    )
                    
                    # Focus
                    self.state.focus_entity = unified.get("focus", thalamus_out["text"][:80])
                    
                    # Monologue
                    monologue = unified.get("monologue", "")
                    if monologue:
                        self.state.inner_monologue = monologue
                        dmn_out = {"thought": monologue, "llm_used": True}

                    # ── Intent: parse and queue for agent layer (v5.0) ──
                    intent_data = unified.get("intent")
                    intent = Intent.from_llm_output(
                        intent_data,
                        source_input=thalamus_out["text"][:200],
                    )
                    if intent:
                        is_tool_feedback = bool(
                            input_data
                            and str(input_data.get("source", "")).startswith("agent/tool/")
                        )
                        if (
                            is_tool_feedback
                            and self.autonomy
                            and self.autonomy.is_active
                        ):
                            autonomy_followup_intent = self._observe_autonomy_intent(intent)
                            # A correlated CALL_TOOL follow-up is rewritten
                            # from the deterministic plan and still needs to
                            # be delivered once.  RESPOND/THINK follow-ups,
                            # however, are fully consumed by the autonomy lane
                            # and must not leak into the generic queue.
                            autonomy_intent_consumed = (
                                autonomy_followup_intent
                                and intent.type != IntentType.CALL_TOOL
                            )
                            # A CALL_TOOL follow-up is queueable only after
                            # the autonomy lane has correlated and rewritten
                            # it.  This covers both V13 plan-backed actions
                            # and the bounded legacy episode path.  An
                            # uncorrelated model action is discarded here.
                            autonomy_intent_queueable = bool(
                                autonomy_followup_intent
                                and intent.type == IntentType.CALL_TOOL
                                and intent.episode_id
                                and intent.goal_id
                                and str(intent.origin or "") != "external"
                            )
                        elif is_tool_feedback:
                            autonomy_intent_queueable = False
                        # V8: 行为倾向特质调制 intent 置信度
                        intent.confidence = self.state.self_model.modulate_intent(
                            intent.type.value, intent.confidence)
                        # v5.2: feed intent to metacognition for tracking
                        self.metacognition.feed_intent(
                            intent.type.value, intent.confidence,
                            intent.tool_name or "",
                        )

                    if (
                        intent
                        and intent.type.value in ("call_tool", "respond")
                        and not autonomy_intent_consumed
                        and autonomy_intent_queueable
                    ):
                        self.state.last_intent = intent.to_dict()
                        self.state.intent_count += 1
                        await self.intent_queue.put(intent)
                        logger.info(
                            "brain-stem: intent produced — %s (confidence=%.2f)",
                            intent.type.value, intent.confidence,
                        )
                    elif intent:
                        # think / ask_question — track but don't push to agent
                        self.state.last_intent = intent.to_dict()
                        self.state.intent_count += 1

                    logger.debug("brain-stem: unified LLM tick — encode+focus+monologue+intent in one call")
                except Exception as e:
                    err_msg = str(e)[:200]
                    logger.warning("brain-stem: unified LLM call failed: %s", err_msg)
                    self.state.last_error = err_msg
                    self.state.llm_error_count += 1
                    self.state.last_input_accepted = False
                    # v5.2: metacognition — LLM failure is a negative outcome
                    self.metacognition.feed_outcome(False, 0.0)
            
            # Chain association (async, fire and forget)
            if encoded_memory:
                chain = await self._bounded_cognitive_await(
                    self.hippocampus.associate(
                        query=thalamus_out["text"],
                        previous_results=hippocampus_out.get("results", []) if hippocampus_out else [],
                    ),
                    "hippocampus.associate",
                    [],
                )
                self.state.association_chain = chain if isinstance(chain, list) else []
        
        # Default mode + Curiosity — idle tick inner monologue + spontaneous thinking
        if not is_external and self.state.ticks_since_input >= 5:
            wm = self.working_memory.get_context()

            # ── Curiosity: spontaneous thought every ~10 idle ticks ──
            if self.state.ticks_since_input % 10 == 0:
                top_drives = self.state.self_model.get_top_drives(2)
                thought = self.state.curiosity.spontaneous_think(wm, top_drives)
                if thought:
                    self.state.inner_monologue = thought[:200]
                    self.working_memory.push(thought[:200], "curiosity", 0.35)
                elif wm:
                    self.state.inner_monologue = "Thinking: " + wm[:100]

            # ── Curiosity: generate new questions when running low ──
            if (self.state.curiosity.pending_count < 5
                    and self.state.ticks_since_input % 20 == 0
                    and self.memory_store
                    and self.memory_store.count() > 0):
                recent_entities = self.state.self_model.identity_traits + (
                    self.state.curiosity.exploration_topics[-5:]
                    if self.state.curiosity.exploration_topics else []
                )
                new_qs = self.state.curiosity.generate_questions(
                    top_drives=self.state.self_model.get_top_drives(3),
                    recent_entities=recent_entities,
                )
                if new_qs:
                    first_q = new_qs[0]["question"]
                    self.state.inner_monologue = "[好奇] {0}".format(first_q[:150])

            # V11: periodically turn persistent internal signals into a
            # bounded candidate goal.  Generation is throttled and deduped in
            # _tick_drive_engine; it no longer waits for a 10-minute deep
            # reflection before the subject can initiate an action.
            if (
                AUTONOMY_ENABLED
                and self.sleep_state == "awake"
                and AUTONOMY_GOAL_INTERVAL_TICKS > 0
                and self.state.total_ticks > 0
                and self.state.total_ticks % AUTONOMY_GOAL_INTERVAL_TICKS == 0
            ):
                self._tick_drive_engine(activation)

            # ── v5.1 Goal System: tick active goals, produce intent if actionable ──
            if self.state.ticks_since_input % 15 == 0 and self.sleep_state == "awake":
                # The long-term scheduler owns selection.  There is still one
                # causal lane for autonomous work; a running episode cannot be
                # preempted until its feedback reaches a safe boundary.
                active_episode = self.autonomy.active if self.autonomy else None
                episode_busy = bool(
                    active_episode
                    and active_episode.status not in {
                        EpisodeStatus.COMPLETED,
                        EpisodeStatus.FAILED,
                        EpisodeStatus.ABORTED,
                    }
                )
                active_goal = (
                    self.task_scheduler.select(
                        self.goal_system,
                        self.state.total_ticks,
                        episode_busy=True,
                    )
                    if episode_busy
                    else self.task_scheduler.claim_for_execution(
                        self.goal_system,
                        self.state.total_ticks,
                    )
                )
                if active_goal and active_goal.status == "active":
                    episode = None
                    plan = None
                    if self.autonomy and not episode_busy:
                        if self.task_execution is not None:
                            try:
                                plan = self._execution_plan_for_goal(active_goal.id)
                                if plan is None:
                                    plan = self.task_execution.ensure_plan_for_goal(
                                        active_goal, current_tick=self.state.total_ticks
                                    )
                                if plan is not None and plan.status in {
                                    PlanStatus.PAUSED,
                                    PlanStatus.BLOCKED,
                                    PlanStatus.COMPLETED,
                                    PlanStatus.FAILED,
                                    "abandoned",
                                }:
                                    self._sync_goal_with_plan(active_goal, plan)
                                elif plan is not None:
                                    episode = self.autonomy.begin(
                                        goal_id=active_goal.id,
                                        goal=active_goal.description,
                                        drive=getattr(active_goal, "source_drive", "") or active_goal.drive,
                                        trigger="drive",
                                        tick=self.state.total_ticks,
                                        task_id=plan.id,
                                        attempt_no=active_goal.attempt_count,
                                    )
                            except Exception as exc:
                                self._record_loop_error("task_execution_plan", exc)
                        if episode is None and self.task_execution is None:
                            episode = self.autonomy.begin(
                                goal_id=active_goal.id,
                                goal=active_goal.description,
                                drive=getattr(active_goal, "source_drive", "") or active_goal.drive,
                                trigger="drive",
                                tick=self.state.total_ticks,
                                attempt_no=active_goal.attempt_count,
                            )

                    # Convert the ready plan step (or legacy goal) to a
                    # correlated CALL_TOOL intent.  No plan-backed action is
                    # emitted until its ledger record exists.
                    action = None
                    if plan is not None and episode is not None:
                        goal_intent, action = self._queue_execution_step(
                            active_goal, episode, plan
                        )
                        # ``begin`` precedes the ledger claim so the episode
                        # can carry the exact causal IDs.  If the plan has no
                        # ready step, close that just-created lane here;
                        # otherwise ``episode_busy`` would remain true on
                        # every later tick and starve the scheduler.
                        if goal_intent is None and episode is not None:
                            self._close_unqueued_execution_episode(
                                active_goal, episode, plan
                            )
                    else:
                        can_issue = (self.autonomy is None) or (episode is not None)
                        goal_intent = (
                            self._goal_to_intent(
                                active_goal,
                                episode_id=episode.id if episode else (
                                    active_episode.id if episode_busy else None
                                ),
                                attempt_no=getattr(active_goal, "attempt_count", 0),
                            )
                            if can_issue and not episode_busy else None
                        )
                    if goal_intent:
                        self.state.last_intent = goal_intent.to_dict()
                        self.state.intent_count += 1
                        queued = await self.intent_queue.put(goal_intent)
                        if not queued and episode:
                            if action is not None and self.task_execution is not None:
                                try:
                                    self.task_execution.cancel_action(
                                        action.id,
                                        "意图队列已满，行动未交付",
                                        tick=self.state.total_ticks,
                                    )
                                except Exception as exc:
                                    self._record_loop_error("task_execution_queue_failure", exc)
                            failed = self.autonomy.fail(
                                "意图队列已满，行动未交付",
                                self.state.total_ticks,
                                result_quality=OutcomeQuality.FAILED,
                            )
                            self._finish_autonomy_episode(
                                failed,
                                False,
                                "意图队列已满，行动未交付",
                            )
                        self.working_memory.push(
                            content="[目标驱动] {0}".format(active_goal.description[:100]),
                            source="goal_system",
                            base_salience=0.5,
                        )
                        # v5.2: metacognition tracks goal-driven intents
                        self.metacognition.feed_intent(
                            goal_intent.type.value, goal_intent.confidence,
                            goal_intent.tool_name or "",
                        )
                        logger.info("brain-stem: goal-driven intent — %s", active_goal.description[:60])

            # ── v5.2 Metacognition: idle pattern detection ──
            if self.state.ticks_since_input % 30 == 0:
                self.metacognition.tick_idle()

            # ── V6: 元认知校准扩散权重（每30个空闲tick）──
            if self.state.ticks_since_input % 30 == 0:
                self.metacognition.calibrate_diffusion(activation)

            # ── V8: 探索循环 — 发现问题 → 创建任务（基于总tick，不只是空闲）──
            if self.state.total_ticks % 15 == 0 and self.state.total_ticks > 0:
                issues = self.exploration_executor.find_issues(self)
                if issues:
                    new_count = self.exploration_executor.issues_to_tasks(
                        issues, self.exploration_queue)
                    if new_count > 0:
                        self.working_memory.push(
                            content=f"[探索] 发现 {new_count} 个新问题",
                            source="exploration",
                            base_salience=0.35,
                        )

            # ── V8: 反思引擎 — 周期检查（基于总tick）──
            if self.state.total_ticks % 30 == 0 and self.state.total_ticks > 0:
                ref = self.reflection_engine.reflect(
                    self, self.memory_store, self.state.total_ticks)
                if ref:
                    self.working_memory.push(
                        content=f"[反思] {ref.get('summary', '')[:100]}",
                        source="reflection_engine",
                        base_salience=0.4,
                    )

            # ── v5.4 Procedural Memory: periodic decay ──
            if self.state.total_ticks % 300 == 0 and self.state.total_ticks > 0:
                self.procedural_memory.decay_skills()

            # ── v5.4 Time Sense: record important events ──
            if input_data and is_subject_input and not refused_input:
                self.time_sense.record_event("input", thalamus_out.get("text", "")[:80])

        # ── v5.2: detect tool result inputs (agent feedback loop) and feed outcomes ──
        if input_data and not refused_input and input_data.get("source", "").startswith("agent/tool/"):
            # Tool result came back. V13 uses the deterministic outcome when
            # one was recorded; legacy traffic retains the text fallback.
            success = autonomy_feedback_success
            tool = autonomy_feedback_tool or input_data.get("source", "").replace("agent/tool/", "")
            execution_outcome = self._last_execution_outcome
            verified_success = bool(
                execution_outcome is not None
                and getattr(execution_outcome, "status", "") == OutcomeQuality.VERIFIED
                and getattr(execution_outcome, "success", None) is True
            )
            outcome_success = verified_success if execution_outcome is not None else success
            outcome_confidence = (
                float(getattr(execution_outcome, "confidence", 0.6))
                if execution_outcome is not None else 0.6
            )
            self.metacognition.feed_outcome(outcome_success, outcome_confidence)
            # V13 learning is deferred until the episode closes, where the
            # idempotent feedback sink receives the full causal outcome.
            if execution_outcome is None:
                self.procedural_memory.record_experience(
                    "call_tool", tool, success, input_data.get("text", "")[:200]
                )
            # V10: 工具结果 → 奖励交付
            # Autonomous episodes receive one richer, correlated reward in
            # ``_finish_autonomy_episode``.  Avoid double-counting the same
            # tool result here; external tool traffic keeps the legacy path.
            if self.reward_system and not autonomy_feedback_seen and execution_outcome is None:
                actual_reward = 0.7 if success else 0.2
                self.reward_system.deliver_reward(
                    channel="achievement",
                    actual_reward=actual_reward,
                    context=f"tool:{tool}",
                )

            # In offline/rule-only mode no follow-up LLM intent may be
            # produced.  A successfully observed tool result is still a
            # complete one-step episode; close it here rather than leaving a
            # goal permanently in ``feedback_received``.
            if (
                autonomy_feedback_seen
                and self.autonomy
                and self.autonomy.is_active
                and not autonomy_followup_intent
            ):
                active = self.autonomy.active
                # A plan may have another dependency-ready step (or a retry)
                # after this observation. Keep the same causal episode alive
                # and queue exactly one next action where possible.
                queued_next = False
                plan = self._execution_plan_for_goal(
                    getattr(active, "goal_id", ""), getattr(active, "task_id", "")
                )
                if execution_outcome is not None and plan is not None:
                    goal = self._find_goal(getattr(active, "goal_id", ""))
                    next_intent, next_action = self._queue_execution_step(
                        goal, active, plan
                    )
                    if next_intent is not None and next_action is not None:
                        self.state.last_intent = next_intent.to_dict()
                        self.state.intent_count += 1
                        queued_next = await self.intent_queue.put(next_intent)
                        if not queued_next:
                            try:
                                self.task_execution.cancel_action(
                                    next_action.id,
                                    "意图队列已满，后续行动未交付",
                                    tick=self.state.total_ticks,
                                )
                            except Exception as exc:
                                self._record_loop_error("task_execution_queue_failure", exc)
                        autonomy_followup_intent = queued_next
                if not queued_next:
                    outcome = (
                        f"工具 {tool} {'执行成功' if outcome_success else '返回失败'}"
                    )
                    quality = (
                        getattr(execution_outcome, "status", OutcomeQuality.UNKNOWN)
                        if execution_outcome is not None else None
                    )
                    record = (
                        self.autonomy.complete(
                            outcome,
                            self.state.total_ticks,
                            result_quality=quality,
                        )
                        if outcome_success
                        else self.autonomy.fail(
                            outcome,
                            self.state.total_ticks,
                            result_quality=quality,
                        )
                    )
                    self._finish_autonomy_episode(record, outcome_success, outcome)

        # ── v5.3: 情感光谱 tick（每个 tick 漂移一步）──
        self.emotional_spectrum.tick()

        # ── v5.4: 时间感 tick ──
        has_recent_activity = (input_data is not None and not refused_input) or self.state.ticks_since_input < 10
        self.time_sense.tick(has_recent_activity, self.state.total_ticks, self.state.uptime_seconds)

        # ── V9 Boredom Engine: 无聊评估 + 行为触发 ──
        if self.boredom_engine:
            boredom_result = self.boredom_engine.tick(
                activation=activation,
                ticks_since_input=self.state.ticks_since_input,
                exploration_queue=self.exploration_queue,
                working_memory=self.working_memory,
                memory_store=self.memory_store,
                curiosity=self.state.curiosity,
                sleep_state=self.sleep_state,
                current_tick=self.state.total_ticks,
            )
            if boredom_result.get("actions"):
                logger.debug("brain-stem: boredom=%.2f(%s) actions=%s",
                           boredom_result["score"], boredom_result["level"],
                           boredom_result["actions"])

        # ── V10: 社会情感 + 奖励系统 + 边界 tick ──
        if self.social_emotion:
            self.social_emotion.tick()
        if self.reward_system:
            self.reward_system.tick()
        if self.boundary:
            self.boundary.tick(cognitive_load=self.metacognition.cognitive_load)
        if self.autobiography:
            self.autobiography.update_chapters(
                current_tick=self.state.total_ticks,
                total_experiences=self.state.self_model.total_experiences,
            )

        # ── v5.2: update cognitive load ──
        has_external_input = (
            input_data is not None
            and not refused_input
            and not (input_data.get("source", "") or "").startswith("agent/")
        )
        if not refused_input:
            self.metacognition.update_cognitive_load(
                llm_called=(is_external and not refused_input and thalamus_out.get("has_input") and not thalamus_out.get("discarded", True)),
                input_processed=has_external_input,
                recent_input_count=min(10, self.state.intent_count),
                activation=activation,  # V6: 同步 fatigue/uncertainty/confidence 到 ActivationField
            )

        # ── Step 6: Basal Ganglia — habit match ──
        habit_out = None
        if thalamus_out["has_input"] and not refused_input:
            habit_out = self.basal_ganglia.match(thalamus_out["text"])
            if habit_out and habit_out.get("matched"):
                self.state.active_habit = habit_out["habit"]["id"]
                self.state.habit_confidence = habit_out["confidence"]

        # ── Step 7: Cingulate — conflict monitor ──
        if refused_input:
            # A refused message is not evidence of an internal conflict.  Do
            # not let hostile text weaken habits or overwrite prior conflict
            # state through the normal cingulate path.
            cingulate_out = {
                "conflict_detected": False,
                "conflicts": [],
                "error_count": self.state.error_count,
            }
        else:
            cingulate_out = self.cingulate.monitor(
                input_text=thalamus_out.get("text", ""),
                emotion=amygdala_out,
                hippocampus_result=hippocampus_out,
                previous_state={"current_emotion": self.state.current_emotion},
            )
        if cingulate_out.get("conflict_detected"):
            self.state.conflict_detected = True
            self.state.conflict_detail = str(cingulate_out.get("conflicts", [])[:2])
            self.state.error_count = cingulate_out.get("error_count", 0)
            if self.state.active_habit:
                self.basal_ganglia.weaken(self.state.active_habit, delta=0.1)

        # ── Working Memory — update ──
        if thalamus_out["has_input"] and not thalamus_out["discarded"] and not refused_input:
            wm_content = thalamus_out["text"][:500]
            if encoded_memory and is_external:
                wm_content = "[记忆: {0}] {1}".format(encoded_memory.get("title", ""), wm_content[:400])
            self.working_memory.push(content=wm_content, source=thalamus_out["source"], base_salience=amygdala_out.get("salience", 0.5))
        if self.state.inner_monologue:
            self.working_memory.push(content=self.state.inner_monologue, source="inner_monologue", base_salience=0.3)
        self.working_memory.tick(activation=activation)  # V6: SalienceScore竞争保留
        self.state.current_context = self.working_memory.get_context() or self.working_memory.context_text
        self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
        self.state.goal_stack = [
            goal.description[:200] for goal in self.goal_system.get_active()
        ][-20:]
        if input_data and not refused_input:
            self.state.attention_span_ticks = 0
        else:
            self.state.attention_span_ticks += 1

        # ── Session sync: save per-source state ──
        if input_data and not refused_input:
            source_id = input_data.get("source", "default")
            session = self.state.session_manager.get(source_id)
            session.current_emotion = self.state.current_emotion
            session.emotion_vector = dict(self.state.emotion_vector)
            session.focus_entity = self.state.focus_entity
            session.inner_monologue = self.state.inner_monologue
            session.current_context = self.state.current_context
            # Save working memory by copying items (v5.0 fix: copy, not reference swap)
            session.working_memory.items = [dict(it) for it in self.working_memory.items]
            session.working_memory.context_text = self.working_memory.context_text
        
        # Signal input processed (always, even if discarded).  Keep the old
        # event for compatibility with legacy callers, but resolve the
        # correlated future as the authoritative completion signal.
        self._complete_input_waiter(input_data)
        self._input_processed.set()
        self._input_processed.clear()

        # ── Step 10: Output push ──
        output_event = self._build_output_event(
            thalamus_out, amygdala_out, hippocampus_out,
            cingulate_out, dmn_out,
        )
        if output_event:
            try:
                self._output_feed.put_nowait(output_event)
            except asyncio.QueueFull:
                pass

    async def _reflection_tick(self):
        """Scheduled reflection — uses self_model for identity-aware thinking."""
        wm_context = self.working_memory.get_context()
        sm = self.state.self_model

        # ── Self-model guided reflection ──
        top_drives = sm.get_top_drives(2)
        drives_str = ", ".join(d["label"] for d in top_drives)

        if wm_context:
            self.state.inner_monologue = "[{0}] {1}".format(drives_str, wm_context[:120])
            self.working_memory.push(
                content="反思[{0}]: {1}".format(drives_str, wm_context[:180]),
                source="reflection",
                base_salience=0.35,
            )
        else:
            if sm.last_reflection:
                self.state.inner_monologue = "Idle: {0}".format(sm.last_reflection[:120])

        # ── Periodic deep self-reflection (every ~20 min by default) ──
        if (DEEP_REFLECTION_ENABLED and
                self.state.total_ticks > 0 and
                self.state.total_ticks % DEEP_REFLECTION_INTERVAL_TICKS == 0):
            await self._deep_self_reflection()

        # ── v5.2 Metacognition insight — inject into monologue ──
        meta_insight = self.metacognition.get_insight()
        if meta_insight and "良好" not in meta_insight:
            self.working_memory.push(
                content="[元认知] {0}".format(meta_insight[:120]),
                source="metacognition",
                base_salience=0.45,
            )

        # ── v5.2 Self-improvement goal from metacognition ──
        improvement = self.metacognition.get_self_improvement_goal()
        if improvement:
            self.working_memory.push(
                content="[自我改进] {0}".format(improvement["description"][:100]),
                source="metacognition",
                base_salience=0.5,
            )

        # ── V7 Goal Generation: via DriveEngine + GoalGenerator ──
        if (self.state.total_ticks > 0 and
                self.state.total_ticks % (DEEP_REFLECTION_INTERVAL_TICKS // 2) == 0):
            self._tick_drive_engine(self.state.activation)

        # Push reflection event
        try:
            self._output_feed.put_nowait({
                "type": "reflection",
                "thought": wm_context[:200] if wm_context else sm.last_reflection[:200],
                "emotion": self.state.current_emotion,
                "top_drives": [d["label"] for d in top_drives],
                "identity_version": sm.identity_version,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except asyncio.QueueFull:
            pass

        # Reset cingulate errors
        if self.state.error_count > 0:
            self.cingulate.acknowledge()
            self.state.error_count = 0
            self.state.conflict_detected = False


    async def _deep_self_reflection(self):
        """Deeper self-reflection using LLM — identity check-in."""
        sm = self.state.self_model
        wm = self.working_memory.get_context()

        ctx = sm.prepare_reflection_prompt(
            working_memory=wm,
            recent_events=sm.self_narrative[-10:] if sm.self_narrative else [],
        )

        try:
            from services.llm_client import get_llm
            llm = get_llm()
            result = await llm.chat_json(
                system="""[SYSTEM CONSTRAINT]
You are the SELF-REFLECTION module of a brain memory agent.
You examine the state of the brain and produce a brief self-reflection.
You are NOT giving advice. You are the brain thinking about itself.
Output JSON only:
{
  "thought": "a brief self-observation — max 100 chars",
  "mood_tendency": "balanced|curious|cautious|excited|tired",
  "trait_shift": {"add": "optional new trait name"} or null,
  "drive_shifts": {"curiosity": 0.01, "coherence": -0.01} or {}
}
Rules:
- Only suggest trait_shift if a genuinely new trait has emerged (rare).
- Drive shifts should be tiny (+-0.02 max).
- Never fabricate events.
- Keep thoughts authentic, sometimes uncertain.""",
                user=ctx,
                temperature=0.4,
                max_tokens=512,
            )
            sm.integrate_reflection(result)

            # ── V7.1: 从所有身份记忆中重新合成 identity_anchor ──
            if self.memory_store:
                id_mems = self.memory_store.get_identity_memories(20)
                if id_mems:
                    await sm.synthesize_anchor(llm, id_mems)

            # ── v5.1: Apply goal feedback to self-model ──
            goal_feedback = self.goal_system.get_feedback_for_self_model()
            if goal_feedback:
                for drive_name, delta in goal_feedback.items():
                    sm.update_drive(drive_name, delta)
                logger.debug("brain-stem: goal feedback applied to drives — %s", goal_feedback)

            logger.debug("brain-stem: deep self-reflection completed")
        except Exception as e:
            logger.debug("brain-stem: deep self-reflection skipped: %s", str(e)[:60])

    def _tick_drive_engine(self, activation):
        """V7: DriveEngine tick — 评估信号 → 更新驱动力 → 生成候选目标。

        长期任务的分层、容量和执行顺序由 ``task_scheduler`` 统一负责。
        """
        # 1. 构建状态快照
        state_snap = build_state_snapshot_for_drive_engine(self)
        state_snap["memories_archived"] = self._last_archived_count

        # 2. 评估信号，更新 ActivationField 中的驱动力维度
        self.drive_engine.tick(activation, state_snap, self.state.total_ticks)

        # 3. 用 GoalGenerator 生成新目标
        sm = self.state.self_model
        recent_entities = (
            self.state.curiosity.exploration_topics[-5:]
            if self.state.curiosity.exploration_topics else
            sm.identity_traits
        )
        curiosity_qs = self.state.curiosity.open_questions[-5:]
        memory_count = self.memory_store.count() if self.memory_store else 0

        new_goals = self.goal_generator.generate(
            activation=activation,
            identity_traits=sm.identity_traits,
            recent_entities=recent_entities,
            curiosity_questions=curiosity_qs,
            memory_count=memory_count,
            current_tick=self.state.total_ticks,
        )

        # 4. 注册到长期任务队列。GoalGenerator 是基于驱动的候选生成器，
        # 不是持久化 owner；任务层负责分级、去重和有限容量。
        self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
        existing = {
            (getattr(goal, "drive", ""), getattr(goal, "description", "")[:160])
            for goal in self.goal_system.get_active()
        }
        for g in new_goals:
            fingerprint = (getattr(g, "drive", ""), getattr(g, "description", "")[:160])
            # Always let the policy layer make the capacity decision.  In
            # particular, a full queue may still accept a maintenance/user
            # task by evicting its lowest-priority exploration item.
            if fingerprint in existing:
                continue
            registered = self.task_scheduler.register_goal(
                self.goal_system,
                g,
                current_tick=self.state.total_ticks,
                source="drive_engine",
            )
            if not registered:
                continue
            if self.task_execution is not None:
                try:
                    self.task_execution.ensure_plan_for_goal(
                        g, current_tick=self.state.total_ticks
                    )
                except Exception as exc:
                    self._record_loop_error("task_execution_plan", exc)
            self.drive_engine.total_goals_generated += 1
            existing.add(fingerprint)
            self.working_memory.push(
                content="[V7目标] {0}".format(g.description[:80]),
                source="drive_engine",
                base_salience=0.4,
            )

        # 5. 选择在下一次安全边界执行的任务；不再让旧 V7 scheduler
        # 直接 abandon 队列中的低层级任务。
        self.task_scheduler.sync(self.goal_system, self.state.total_ticks)

    def _generate_goals_from_state(self):
        """v5.1 向后兼容——委托给 V7 DriveEngine。"""
        activation = self.state.activation
        self._tick_drive_engine(activation)

    def _goal_to_intent(
        self,
        goal,
        episode_id: str | None = None,
        attempt_no: int = 0,
        step=None,
        plan_id: str | None = None,
        step_id: str | None = None,
    ) -> Intent | None:
        """v5.1: Convert a goal into a CALL_TOOL intent."""
        from brain.intent import IntentType

        # Map goal drive to appropriate tool
        tool_map = {
            "curiosity": ("web_search", {"query": goal.description[:100]}),
            "coherence": ("memory_search", {"query": goal.description[:100]}),
            "growth": ("memory_search", {"query": goal.description[:100]}),
            "connection": ("memory_search", {"query": "最近的对话和未完成的事项"}),
            "self_preservation": ("memory_search", {"query": "身份变化 核心认知"}),
        }

        if step is not None:
            tool_name = getattr(step, "tool_name", "") or "memory_search"
            metadata = getattr(step, "metadata", {})
            query_hint = (
                metadata.get("query_hint")
                if isinstance(metadata, dict) else None
            ) or goal.description[:160]
            if tool_name in {"memory_search", "web_search"}:
                tool_args = {"query": str(query_hint)[:300]}
            elif tool_name == "file_read":
                path = metadata.get("path", "") if isinstance(metadata, dict) else ""
                tool_args = {"path": str(path)[:300]} if path else {}
            else:
                # A plan may describe a non-read-only step for an operator,
                # but autonomous defaults never synthesize arbitrary args.
                tool_args = {}
        else:
            tool_name, tool_args = tool_map.get(goal.drive, ("memory_search", {"query": goal.description[:100]}))

        return Intent(
            type=IntentType.CALL_TOOL,
            tool_name=tool_name,
            tool_args=tool_args,
            confidence=goal.priority * 0.8,
            reason="goal-driven: {0}".format(goal.description[:60]),
            source_input=goal.description[:200],
            episode_id=episode_id,
            goal_id=getattr(goal, "id", None),
            plan_id=plan_id,
            step_id=step_id,
            attempt_no=max(0, int(attempt_no or 0)),
            origin="autonomous",
            created_tick=self.state.total_ticks,
        )

    async def _snapshot_state(self):
        """Persist brain state. V6: includes ActivationField."""
        if self.state_store is not None:
            if self._life_restore_blocked:
                self._sync_life_projection()
                self._record_loop_error(
                    "snapshot_life", self._life_restore_error or "life restore is blocked"
                )
                return False
            if not self._renew_life_control(force=True):
                self._record_loop_error(
                    "snapshot_life", "life-control lease could not be renewed"
                )
                return False
            try:
                self._attach_life_sink(self.life_kernel)
            except (LifecycleError, ValueError, TypeError) as exc:
                self._mark_life_restore_blocked(exc)
                self._record_loop_error("snapshot_life", exc)
                return False
            # Keep the compact BrainState fields and the lossless working
            # memory view in sync immediately before journaling.
            self._sync_life_projection()
            self._sync_self_maintenance_projection()
            self.state.sleep_state = self.sleep_state
            self.state.active_thoughts = [dict(item) for item in self.working_memory.items]
            self.state.current_context = self.working_memory.get_context()
            if self.autonomy:
                self._sync_autonomy_projection()
            self.task_scheduler.sync(self.goal_system, self.state.total_ticks)
            snap = self.state.snapshot()  # State.snapshot() already includes activation
            snap["life_kernel"] = self.life_kernel.snapshot()
            coordinator = getattr(self, "succession_coordinator", None)
            if coordinator is not None:
                try:
                    snap["succession_runtime"] = coordinator.snapshot()
                    snap["succession_activation"] = {
                        "required": bool(self._successor_activation_required),
                        "receipt_hash": self._successor_evaluation_receipt_hash,
                    }
                except Exception as exc:
                    # A succession snapshot is an integrity boundary.  Do
                    # not publish a partial brain snapshot that could make a
                    # child look ordinary after restart.
                    self._record_loop_error("snapshot_succession", exc)
                    return False
            # Keep the full safety/signal records outside the compact API
            # projections so restart can verify their independent history.
            snap["homeostasis"] = self.homeostasis.snapshot()
            snap["motivation"] = self.motivation.snapshot_for_persistence()
            # Persist only bounded, hash-addressed evidence metadata.  Host
            # capabilities and candidate source paths are intentionally absent;
            # a restart must receive a fresh explicit host binding.
            snap["iteration_pipeline"] = self._iteration_snapshot_for_persistence()
            if self.controlled_environment is not None:
                # The host-bound root/approval validator are not serialized
                # as capabilities.  Only the policy fingerprint and redacted
                # audit chain cross the bounded snapshot boundary.
                snap["controlled_environment"] = self.controlled_environment.snapshot()
            snap["working_memory"] = self.working_memory.snapshot()
            snap["last_archived_count"] = self._last_archived_count
            snap["maintenance"] = {
                "last_dream_time": self.last_dream_time,
                "last_consolidation_time": self.last_consolidation_time,
                "last_reflection": self.last_reflection,
                "last_snapshot": self.last_snapshot,
                "last_decay": self.last_decay,
            }
            snap["goal_system"] = self.goal_system.snapshot()
            snap["task_scheduler"] = self.task_scheduler.snapshot(self.goal_system)
            if self.task_execution is not None:
                snap["task_execution"] = self.task_execution.snapshot()
            if self.learning_feedback is not None:
                snap["learning_feedback"] = self.learning_feedback.snapshot()
            snap["metacognition"] = self.metacognition.snapshot()
            snap["emotional_spectrum"] = self.emotional_spectrum.snapshot()
            snap["procedural_memory"] = self.procedural_memory.snapshot()
            snap["time_sense"] = self.time_sense.snapshot()
            snap["exploration_queue"] = self.exploration_queue.snapshot()
            snap["exploration_executor"] = {
                "cycles_completed": self.exploration_executor.cycles_completed,
                "total_issues_found": self.exploration_executor.total_issues_found,
            }
            snap["reflection_engine"] = self.reflection_engine.snapshot()
            snap["drive_engine"] = self.drive_engine.snapshot()
            if self.autonomy:
                snap["autonomy"] = self.autonomy.snapshot()
            snap["thalamus"] = {
                "last_input": self.thalamus.last_input,
                "noise_discarded": self.thalamus.noise_discarded,
                "total_relayed": self.thalamus.total_relayed,
            }
            snap["amygdala"] = {
                "valence": self.amygdala.valence,
                "arousal": self.amygdala.arousal,
                "dominance": self.amygdala.dominance,
                "salience": self.amygdala.salience,
            }
            if self.predictive_layer:
                snap["predictive_layer"] = self.predictive_layer.snapshot()
            if self.cognitive_dispatch:
                snap["cognitive_dispatch"] = self.cognitive_dispatch.snapshot()
            if self.boredom_engine:
                snap["boredom_engine"] = self.boredom_engine.snapshot()
            if self.social_emotion:
                snap["social_emotion"] = self.social_emotion.snapshot()
            if self.attachment_system:
                snap["attachment_system"] = self.attachment_system.snapshot()
            if self.reward_system:
                snap["reward_system"] = self.reward_system.snapshot()
            if self.autobiography:
                snap["autobiography"] = self.autobiography.snapshot()
            if self.boundary:
                snap["boundary"] = self.boundary.snapshot()
            saved = self.state_store.save(snap)
            if saved is False:
                self._record_loop_error("snapshot_persist", "state store rejected snapshot")
                return False
            logger.debug("brain-stem: state snapshot saved (V6 activation: %d dims)",
                         len(snap.get("activation", {}).get("values", {})))
            return True
        return False


    def _update_sleep_state(self):
        """Update sleep state based on ticks since last input. V9: considers boredom."""
        t = self.state.ticks_since_input

        # V9: 极度无聊时抗拒深睡 — 先找事做再考虑睡觉
        if self.boredom_engine and self.boredom_engine.should_resist_sleep(t):
            # 保持 drowsy 或 light_sleep，不进入 deep_sleep
            if t >= LIGHT_SLEEP_THRESHOLD_TICKS:
                self.sleep_state = "light_sleep"
            elif t >= DROWSY_THRESHOLD_TICKS:
                self.sleep_state = "drowsy"
            else:
                self.sleep_state = "awake"
            self.state.sleep_state = self.sleep_state
            return

        if t >= DEEP_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "deep_sleep"
        elif t >= LIGHT_SLEEP_THRESHOLD_TICKS:
            self.sleep_state = "light_sleep"
        elif t >= DROWSY_THRESHOLD_TICKS:
            self.sleep_state = "drowsy"
        else:
            self.sleep_state = "awake"
        self.state.sleep_state = self.sleep_state

    async def _dream_tick(self):
        """Generate a dream during sleep."""
        if not self.memory_store:
            return

        wm_snapshot = [item["content"][:100] for item in self.working_memory.get_top(3)]
        dream = await self.dream_engine.dream(self.memory_store, wm_snapshot)

        if dream:
            self.state.inner_monologue = "[{0}] {1}".format(
                self.sleep_state, dream.get("narrative", "")[:200]
            )
            self.working_memory.push(
                "Dream: " + dream.get("narrative", "")[:200],
                "dream",
                dream.get("significance", 0.3),
            )
            # Push dream to output feed
            try:
                self._output_feed.put_nowait({
                    "type": "dream",
                    "sleep_state": self.sleep_state,
                    "dream_title": dream.get("dream_title"),
                    "narrative": dream.get("narrative", "")[:200],
                    "emotional_tone": dream.get("emotional_tone"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except asyncio.QueueFull:
                pass

    async def _consolidation_tick(self):
        """Memory consolidation during sleep — uses v3 pipeline."""
        if not self.memory_store:
            return

        result = await run_consolidation(self.memory_store, self.memory_store.db_path)
        if result["consolidated"]:
            logger.info("brain-stem: consolidation complete — %d changes, %d clusters",
                       result["changes"], result["compressed_clusters"])

    def _build_output_event(
        self,
        thalamus_out: dict,
        amygdala_out: dict,
        hippocampus_out: dict | None,
        cingulate_out: dict,
        dmn_out: dict | None,
    ) -> dict | None:
        """Build an output event for external agents to consume."""
        if not thalamus_out.get("has_input"):
            return None

        return {
            "type": "brain_tick",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "focus": self.state.focus_entity,
            "emotion": self.state.current_emotion,
            "salience": amygdala_out.get("salience", 0.0),
            "priority": "normal",  # no longer tracked via prefrontal
            "memory_hits": len(hippocampus_out.get("results", [])) if hippocampus_out else 0,
            "conflict": cingulate_out.get("conflict_detected", False),
            "inner_monologue": self.state.inner_monologue[:200],
            "working_memory": self.state.current_context[:200],
            "active_goals": len(self.goal_system.get_active()),
            "cognitive_load": round(self.metacognition.cognitive_load, 2),
            "emotional_expression": self.emotional_spectrum.get_expression(),
        }
