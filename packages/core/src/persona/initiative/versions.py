"""Which prompt versions a held notice may still be delivered under (R9-249/R9-256 review).

A notice stores the candidate that was held, text and citations included. Candidates from
before ``a5-scan-v7`` (the scan) and ``a7-event-candidate-v5`` (A7's events) may cite a
conversation summary the scan no longer reads, speak of "the user" to the user, or carry an
offer the template used to re-case (third review LOW 5: the floors are the versions in
force, raised with every user-facing prompt change).
Such a notice must never reach the user: the flush expires it and the digest hides it, so
re-enabling initiative needs no manual data step. A version string this module cannot
read is treated as superseded (precision first).
"""

from __future__ import annotations

import re
from types import MappingProxyType

__all__ = [
    "MAX_CITED_NODE_READS",
    "MIN_DELIVERABLE_PROMPT_VERSIONS",
    "prompt_version_deliverable",
]

#: How many cited nodes the wellbeing subject rule reads for their sources. Past this
#: bound the subject is gated whole (fail closed), audited as ``too_many_node_citations``.
MAX_CITED_NODE_READS = 3

#: The oldest deliverable version number per prompt family.
MIN_DELIVERABLE_PROMPT_VERSIONS = MappingProxyType({"a5-scan": 7, "a7-event-candidate": 5})

_VERSION_RE = re.compile(r"(?P<family>[a-z0-9]+(?:-[a-z0-9]+)*?)-v(?P<number>[0-9]+)")


def prompt_version_deliverable(version: str) -> bool:
    """Whether a held notice produced under ``version`` may still be delivered."""
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        return False
    floor = MIN_DELIVERABLE_PROMPT_VERSIONS.get(match["family"])
    return floor is not None and int(match["number"]) >= floor
