"""Shared LLM-response parsing helpers, used by both agents/base.py's
WorkerAgent and guardian/coverage_check.py's coverage check. Extracted here
(rather than duplicated, as it was originally in coverage_check.py) so the
single-line code-fence fix documented in _strip_code_fence's docstring
applies to both callers, not just whichever one happened to get fixed.
"""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

# Exceptions that indicate an LLM response could not be turned into valid
# structured data -- malformed JSON, missing keys, or a Pydantic model
# rejecting the parsed fields. A specific tuple, not `except Exception`.
PARSE_ERRORS = (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError)

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?(.*?)\n?```$", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Best-effort removal of a ```/```json wrapper, in case the model adds
    one despite being told not to. Does not affect well-formed output.

    A line-splitting approach here previously mishandled a fence collapsed
    onto a single line (```{"a": 1}``` with no embedded newlines) by
    stripping it down to an empty string -- caught by the JSONDecodeError
    retry, but silently burning the one allotted retry on an otherwise valid
    response. A regex match on the whole string, not line count, handles the
    single-line and multi-line cases uniformly."""
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped
