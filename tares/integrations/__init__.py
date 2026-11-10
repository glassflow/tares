"""Integrations: the parts of the factory a cell can swap (tickets, review, ci, memory,
monitoring, guardrails, runtime). See registry.py for how they are found."""
from .base import Integration
from .registry import SLOTS, available, get, load
from .types import (CIState, Decision, Field, LaunchSpec, PRRef, Status, TicketRef,
                    Verdict)

__all__ = ["Integration", "SLOTS", "available", "get", "load", "CIState", "Decision", "Field",
           "LaunchSpec", "PRRef", "Status", "TicketRef", "Verdict"]
