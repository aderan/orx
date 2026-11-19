"""orx-analytics — read-only execution observability across projects and agents.

Design invariants (see docs/ in the repo):
- Source databases and transcript stores are opened read-only (mode=ro +
  query_only). This tool never migrates, checkpoints, or rewrites them.
- Unknown stays unknown: missing values are never zero-filled, guessed, or
  upgraded; partial coverage always publishes its denominator.
- Historical correlations record their basis and confidence; they are never
  written as facts. A stored session reference (written by ORX itself) is the
  only authoritative session identity for an attempt.
- No billing basis exists in any source; no fees are computed.
"""

__version__ = "0.4.5"

READER_VERSION = f"orx-analytics {__version__} reader"
