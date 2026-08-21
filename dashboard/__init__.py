"""
SmartVyapar Commercial POS — Enterprise Dashboard Analytics Package.
Aggregates routers, services, filters, and Pydantic schemas for executive analytics.
"""

__version__ = "1.3.5"
__author__ = "SmartVyapar Architecture Team"
__description__ = "Enterprise Analytics & Real-Time POS Dashboard Engine"

from dashboard.router import router

__all__ = [
    "__version__",
    "__author__",
    "__description__",
    "router",
]
