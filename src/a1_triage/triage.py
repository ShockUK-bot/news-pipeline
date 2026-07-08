"""A1 invocation core: model call -> code-side validation -> one retry with
the error appended -> TriageResult or TriageRejected. Never crashes on model
misbehavior; never drops (the REJECT lands in the journal with raw output).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.log import get_logger, kv

from .backends import ModelBackend
from .prompt import build_messages
from .schema import TriageOutput, TriageValidationError, triage_json_schema, validate_triage

log = get_logger("a1.triage")


@dataclass
class TriageResult:
    triage: TriageOutput
    model_id: str
    latency_ms: int
    attempts: int


class TriageRejected(Exception):
    """Model failed to produce contract-valid output within the retry budget."""
    def __init__(self, detail: str, raw: str, model_id: str, latency_ms: int, attempts: int):
        self.detail = detail
        self.raw = raw
        self.model_id = model_id
        self.latency_ms = latency_ms
        self.attempts = attempts
        super().__init__(detail)


async def run_triage(backend: ModelBackend, item: dict, cluster: dict,
                     retries_on_invalid: int = 1) -> TriageResult:
    schema = triage_json_schema()
    total_latency = 0
    error: TriageValidationError | None = None

    for attempt in range(1 + retries_on_invalid):
        messages = build_messages(item, cluster,
                                  retry_error=error.detail if error else None)
        reply = await backend.complete(messages, schema)
        total_latency += reply.latency_ms
        try:
            triage = validate_triage(reply.text)
            return TriageResult(triage=triage, model_id=reply.model_id,
                                latency_ms=total_latency, attempts=attempt + 1)
        except TriageValidationError as e:
            error = e
            log.warning("invalid triage output",
                        extra=kv(attempt=attempt + 1, detail=e.detail[:120]))

    raise TriageRejected(detail=error.detail, raw=error.raw,
                         model_id=reply.model_id, latency_ms=total_latency,
                         attempts=1 + retries_on_invalid)
