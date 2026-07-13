# ruff: noqa: E402
import sys

class SafeWriter:
    def __init__(self, original):
        self._original = original

    def write(self, data):
        try:
            self._original.write(data)
        except BrokenPipeError:
            self.write = lambda x: None

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)

sys.stdout = SafeWriter(sys.stdout)
sys.stderr = SafeWriter(sys.stderr)


from .providers import (
    ClaudeInteractiveProvider, ClaudeProvider, CodexProvider, CursorProvider,
    FakeProvider, GeminiProvider, Provider, ProviderError, RateLimited,
)
from .learn import adjust_order
from .provider_models import list_provider_models, resolve_model
from .sessions import format_sessions_text, probe_sessions
from .routing import CAPABILITIES, KIND_ROUTES, infer_kind, route
from .scheduler import AgentResult, BudgetExceeded, Runner, ROUTES, log
from .stats import provider_stats, run_summary
from .toolsets import ToolCatalog, ToolKind, ToolSet, ToolSpec
from .workflow import (
    DECISION_SCHEMA, SUPERVISION_SCHEMA, Limits, WorkflowError, WorkflowLimitError,
    WorkflowSpec, render, run_spec,
)
from .recipes import all_recipes, get_recipe, list_recipes

__all__ = [
    "Runner", "AgentResult", "BudgetExceeded", "ROUTES", "log",
    "Provider", "CodexProvider", "ClaudeProvider", "ClaudeInteractiveProvider",
    "CursorProvider", "GeminiProvider", "FakeProvider", "RateLimited", "ProviderError",
    "CAPABILITIES", "KIND_ROUTES", "infer_kind", "route", "provider_stats",
    "run_summary", "adjust_order",
    "probe_sessions", "format_sessions_text", "list_provider_models", "resolve_model",
    "ToolCatalog", "ToolKind", "ToolSet", "ToolSpec",
    "run_spec", "render", "WorkflowSpec", "WorkflowError", "WorkflowLimitError",
    "Limits", "DECISION_SCHEMA", "SUPERVISION_SCHEMA",
    "all_recipes", "get_recipe", "list_recipes",
]
