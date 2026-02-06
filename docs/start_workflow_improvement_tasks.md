# Start Workflow Improvement Tasks

1. Validate lane initialization success by checking the `parallel_chat_lanes.py` exit code and capture stderr when it fails, ensuring the JSON report reflects actual state.
2. Harden controls reset handling by treating non-zero exit from `reset_workflow_state.py` as failure, persisting stdout/stderr, and emitting an explicit failure notification.
3. Propagate the outcome of `open_agent_mode_tabs.py`, recording any gating error text and only logging a success event when chat tabs were truly opened.
4. Detect existing user activity monitor processes before spawning a new one so duplicate monitors are avoided and the report logs the skip decision.
