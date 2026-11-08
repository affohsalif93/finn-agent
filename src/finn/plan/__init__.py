"""Plan reader: Excel business plan → building blocks + plan facts + findings (D6)."""

from finn.models.run import PlanReadResult
from finn.plan.reader import read_plan

__all__ = ["PlanReadResult", "read_plan"]
