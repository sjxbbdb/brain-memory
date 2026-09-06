"""Canonical product release version.

The project release number is intentionally independent from historical
capability, API-route, database-schema, and identity-evolution labels.  Those
labels remain useful for compatibility and architecture tracking; this module
is the single source of truth for the user-facing product version.
"""

PRODUCT_VERSION = "0.1.0"
PRODUCT_VERSION_LABEL = "v0.1"

# Conventional alias for tooling and embedders.
__version__ = PRODUCT_VERSION
