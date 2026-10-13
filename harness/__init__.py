"""Experiment agent harness: runs Claude (claude-opus-5-5) against the rig's tools and serves the dashboard.

    python -m harness                     # dashboard on http://127.0.0.1:8750, rig at MHAPI_URL
    MHAPI_URL=http://127.0.0.1:8743 ...   # the digital twin (python -m twin)

The agent sees the hardware API's tools (from the service's `describe`), plus three harness tools: `wait`,
`note` and `python` (sandboxed analysis of the run's recorded data). The harness records every turn,
tool call and reading under harness_runs/<run_id>/.
"""
DEFAULT_PORT = 8750
MODEL = "claude-opus-5-5"
