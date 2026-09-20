"""Shared helpers for mock provider job-id encoding.

Per SPEC section 40 ("Poller crash: durable jobs remain; restart and
continue"), a fresh Poller/provider instance created after a restart must be
able to correctly answer status() for a job it did not submit in this
process. Real providers have their own durable/remote state to consult; these
mocks instead encode the one piece of information they need (the
polls-to-success target) directly into `provider_job_id` at submit time, so a
fresh provider instance can lazily reconstruct an in-memory tracking entry
for an unrecognized id instead of treating it as failed/unknown.
"""

import re
import uuid
from typing import Optional

_TARGET_RE = re.compile(r"_p(\d+)$")


def build_job_id(prefix: str, target_polls: int) -> str:
    """Build a provider_job_id encoding the polls-to-success target.

    Format: "<prefix>_<random_hex>_p<target_polls>".
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}_p{target_polls}"


def parse_target(job_id: str, prefix: str) -> Optional[int]:
    """Extract the polls-to-success target embedded in a provider_job_id.

    Returns None if the id doesn't look like one of ours (e.g. a genuinely
    unknown/bogus id passed by a caller), so callers can fall back to
    reporting "failed" for those.
    """
    if not job_id.startswith(prefix + "_"):
        return None
    match = _TARGET_RE.search(job_id)
    if not match:
        return None
    return int(match.group(1))
