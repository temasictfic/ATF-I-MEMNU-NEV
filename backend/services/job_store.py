"""In-memory verification state shared between routes and the orchestrator.

Lives at services/ rather than api/ so api/parsing.py can clear stale entries
on source updates without forming a parsing→verification import edge.

Two dicts:

* ``verify_jobs`` — job_id -> {"status", "pdfs", "error"?}; lifecycle states
  are ``running`` / ``done`` / ``failed`` and are written by the verification
  endpoint job wrappers.
* ``verify_results`` — pdf_id -> {source_id -> VerificationResult}; the
  orchestrator writes here as each source finalises.
"""

from models.verification_result import VerificationResult

verify_jobs: dict[str, dict] = {}
verify_results: dict[str, dict[str, VerificationResult]] = {}
