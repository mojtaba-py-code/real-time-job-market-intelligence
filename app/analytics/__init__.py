"""Analytics engines: market, trends, salary, geography, companies, skills."""

from __future__ import annotations

from app.analytics.company.analyzer import CompanyAnalytics
from app.analytics.geography.analyzer import GeographyAnalytics
from app.analytics.market.overview import MarketAnalytics
from app.analytics.recommendations.market_fit import MarketFitEngine
from app.analytics.salary.analyzer import SalaryAnalytics
from app.analytics.skills.cooccurrence import CooccurrenceAnalyzer
from app.analytics.trends.emerging import EmergingTechnologyDetector
from app.analytics.trends.engine import TrendEngine

__all__ = [
    "CompanyAnalytics",
    "CooccurrenceAnalyzer",
    "EmergingTechnologyDetector",
    "GeographyAnalytics",
    "MarketAnalytics",
    "MarketFitEngine",
    "SalaryAnalytics",
    "TrendEngine",
]
