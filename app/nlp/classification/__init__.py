"""Classifiers for experience, contract type and language."""

from __future__ import annotations

from app.nlp.classification.employment import EmploymentClassifier, LanguageDetector
from app.nlp.classification.experience import ExperienceClassifier, ExperienceRequirement

__all__ = [
    "EmploymentClassifier",
    "ExperienceClassifier",
    "ExperienceRequirement",
    "LanguageDetector",
]
