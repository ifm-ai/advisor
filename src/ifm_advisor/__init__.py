"""Advisor: a privileged advisor for agentic coding rollouts.

An advisor that can read a task's reference solution, hidden tests and private notes reviews a
solver's draft turns - every proposed finish, and optionally every k turns - and either keeps the
draft or has the solver regenerate it after a solver-safe cue. Only the solver's selected response
is committed, so advised and ordinary trajectories share one representation.
"""

from . import litellm_hook
from .advisor import Advisor, Regenerate, Revision
from .config import AdvisorConfig, EndpointConfig, ReasoningPrefillCue, SelfCheckCue
from .decision import AdvisorDecision, AdvisorDecisionError
from .errors import AdvisorRequestError
from .material import TaskLayout, TaskMaterial

__version__ = "0.1.0"

__all__ = [
    "Advisor",
    "AdvisorConfig",
    "AdvisorDecision",
    "AdvisorDecisionError",
    "AdvisorRequestError",
    "EndpointConfig",
    "ReasoningPrefillCue",
    "Regenerate",
    "Revision",
    "SelfCheckCue",
    "TaskLayout",
    "TaskMaterial",
    "litellm_hook",
]
