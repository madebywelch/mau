"""Regression fixtures for the Evolution Agent prototype.

Each `*.json` here defines a self-contained request the Evolution Agent's
`RegressionSuite` will replay against the deterministic `MockBackend` to
verify that a proposed harness mutation hasn't broken convergence.

Schema (minimal):
    {
        "request": "human-readable initiative",
        "max_turns": 12  // optional; defaults to RegressionSuite.max_turns
    }
"""
