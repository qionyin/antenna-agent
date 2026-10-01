"""Evaluation-driven diagnosis and change proposals for the learning module."""

from .service import EvolutionService
from .adapter import SelfEvolvingEvaluationAdapter
from .extraction_quality import ExtractionQualityEvaluator
from .executor import ControlledEvolutionExecutor
from .llm_advisor import LLMEvolutionAdvisor
from .policy import EvolutionPolicy

__all__ = [
    "ControlledEvolutionExecutor",
    "EvolutionPolicy",
    "EvolutionService",
    "ExtractionQualityEvaluator",
    "LLMEvolutionAdvisor",
    "SelfEvolvingEvaluationAdapter",
]
