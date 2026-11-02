"""Goal lifecycle state machine (Phase 8B).

The single source of truth for goal transitions. The worker and the API
both go through `transition()`; the database conditional-UPDATE is the
mechanism, this module is the authority.

States (constants):
  queued              created, not yet planned
  planning            planner (provider) call in progress or attributed to it
  ready               plan validated + locally safety-approved, may execute
  executing           steps running/pending under the worker
  awaiting_approval   a proposed write waits for explicit human
                      authorization (exact digest-bound proposal)
  verifying           all tool steps done, criteria coverage being computed
  succeeded           terminal: all criteria verified against persisted steps
  blocked             cannot proceed for a named reason (recoverable via retry,
                      unless the code says otherwise)
  failed              terminal: honest failed outcome recorded
  cancelled           terminal: cancellation honored
"""

BLOCKED_CODES_REPORTED = (
    "invalid_plan",
    "unsupported_capability",
    "permission_unsupported",
    "not_configured",
    "reconciliation_required",
    "provider_timeout",
    "upstream_error",
    "connection_failed",
    "rate_limited",
    "review_rejected",
    "reviewer_unavailable",
    "approval_withdrawn",
    "approval_claim_conflict",
    "criteria_unverified",
    "goal_time_exceeded",
    "step_retry_exhausted",
    "repair_budget_exhausted",
    "verification_unavailable",
    "verification_timed_out",
    "loop_budget_exhausted",
    "decision_invalid",
    "contract_unverifiable",
)

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

# The transition table. Keys: current status. Values: allowed next ones.
# Everything else is refused.
_TRANSITIONS = {
    "queued": ("planning", "cancelled"),
    "planning": ("ready", "blocked", "failed", "cancelled"),
    "ready": ("executing", "cancelled"),
    "executing": ("verifying", "awaiting_approval", "blocked", "failed",
                  "cancelled",
                  "planning"),          # 10B: verification-driven repair
    "awaiting_approval": ("executing", "blocked", "cancelled"),
    "verifying": ("succeeded", "failed", "blocked", "cancelled",
                  "planning"),          # 10B: repair after verify
    "blocked": ("queued", "failed", "cancelled"),
    "succeeded": (),
    "failed": (),
    "cancelled": (),
}


def transition(current, new):
    """Validate one lifecycle transition. Returns `new` if legal.

    Raises ValueError (invalid_request semantics) when the transition is
    not in the table — callers must never move a goal through an
    unlisted path.
    """
    if new == current:
        return new  # idempotent no-op transition (e.g. re-scan)
    allowed = _TRANSITIONS.get(current)
    if allowed is None or new not in allowed:
        raise ValueError(
            f"invalid lifecycle transition '{current}' -> '{new}'"
        )
    return new


def is_terminal(status):
    return status in TERMINAL_STATUSES


def cancellable(status):
    """States from which cancellation may complete."""
    return status in ("queued", "planning", "ready", "executing",
                      "awaiting_approval", "verifying", "blocked")


def retryable(status, error_code):
    """blocked goals an operator may re-queue (state machine gate).

    Every blocked goal is operator-retryable through /retry EXCEPT the
    classes where automatic requeue would be dishonest:
    reconciliation_required — the outcome of an interrupted external
    operation is unknown, so a human must reconcile first;
    approval_withdrawn — the human rejected the exact proposal, and a
    re-run must never silently bypass that decision (a NEW goal may be
    created instead).
    """
    return status == "blocked" and error_code not in (
        "reconciliation_required", "approval_withdrawn")
