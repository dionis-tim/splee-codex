# Run the Splee agent with Codex

This optional Linux backend uses Codex CLI app-server over stdio. Build Splee normally. Install Python 3.11+ with jsonschema available to /usr/bin/python3 -I. Sign in with Codex using the service account and confirm its ChatGPT login. The adapter was exercised with Codex CLI 0.163.0.

Set these variables on the Splee API service and restart it:

```ini
SPLEE_AGENT_PROVIDER=codex
SPLEE_CODEX_BIN=/absolute/path/to/native/codex
SPLEE_CODEX_DRIVER=/absolute/path/to/splee/backend/codex_driver.py
AGENT_MODEL=<model available to your signed-in account>
```

Use a dedicated OS service account. Each invocation opens an ephemeral thread and receives the tools selected by its Splee task. The model output is validated against the task schema. Splee reserves the configured per-call allowance for accounting; this is not a measured API charge. Start with SENDING_ENABLED=false and check a draft or audience-segment job before connecting mailboxes.
