"""Shared primitives every stage, tier, and Adapter is built on.

Nothing in here knows about tiers or workflow shapes — those layer on top.
What lives here is the machinery the Decision Records require to be shared
rather than re-implemented: one tool definition and cloning rule (DR-008), one
batch editor (DR-007), one approval gate (DR-012), one context budget
(DR-010), one oversized-output policy (DR-011), one cancel token every long
loop watches (DR-030), and the agent loop that binds them together.
"""

from __future__ import annotations

from .agent import DEFAULT_MAX_TOOL_ROUNDS, AgentResult, AgentStream, BaseAgent
from .approval import (
    APPROVAL_REQUIRED_TOOLS,
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    AutoApproveGate,
    CallbackApprovalGate,
    DenyingApprovalGate,
    requires_approval,
)
from .cancel import CancelToken
from .context import (
    ContextBudget,
    ContextMetrics,
    HeuristicTokenEstimator,
    TokenEstimator,
)
from .events import (
    CallbackEventSink,
    CompositeEventSink,
    Event,
    EventSink,
    NullEventSink,
    RecordingEventSink,
)
from .execution import ToolExecutor, ToolResult
from .memory import AgentMemory
from .tool import Tool, ToolHandler, clone_tools, tools_schema
from .tool_output import (
    BoundedToolOutput,
    FileToolOutputStore,
    NullToolOutputStore,
    ToolOutputStore,
)
from .tools import (
    BUILTIN_TOOLS,
    MUTATING_TOOL_NAMES,
    NETWORK_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    catalog_subset,
)
from .workspace import Workspace

__all__ = [
    "APPROVAL_REQUIRED_TOOLS",
    "AgentMemory",
    "AgentResult",
    "AgentStream",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "AutoApproveGate",
    "BUILTIN_TOOLS",
    "BaseAgent",
    "BoundedToolOutput",
    "CallbackApprovalGate",
    "CallbackEventSink",
    "CancelToken",
    "CompositeEventSink",
    "ContextBudget",
    "ContextMetrics",
    "DEFAULT_MAX_TOOL_ROUNDS",
    "DenyingApprovalGate",
    "Event",
    "EventSink",
    "FileToolOutputStore",
    "HeuristicTokenEstimator",
    "MUTATING_TOOL_NAMES",
    "NETWORK_TOOL_NAMES",
    "NullEventSink",
    "NullToolOutputStore",
    "READ_ONLY_TOOL_NAMES",
    "RecordingEventSink",
    "Tool",
    "ToolExecutor",
    "ToolHandler",
    "ToolOutputStore",
    "ToolResult",
    "TokenEstimator",
    "Workspace",
    "catalog_subset",
    "clone_tools",
    "requires_approval",
    "tools_schema",
]
