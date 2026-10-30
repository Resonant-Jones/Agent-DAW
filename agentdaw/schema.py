"""JSON Schema export.

The schemas are the published contract. A hosted product would serve these
to third parties so their patches and sessions are portable, and the
internal implementation could then be rewritten in any language without
breaking anyone.
"""

from __future__ import annotations

from .spec import json_schema_bundle

__all__ = ["json_schema_bundle"]
