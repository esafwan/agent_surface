# Agent Backend CLIs - Non-Interactive Invocation Reference

This document describes the exact non-interactive ("print mode") CLI invocations for locally-installed coding-agent CLIs, suitable for thin Python adapter wrapping.

---

## opencode

**Binary**: `opencode`

**Non-Interactive Invocation Template**:
```bash
opencode run -m "<MODEL_ID>" "<PROMPT>"
```

**Model Selection**:
- Flag: `-m` (or `--model`)
- Format: `opencode-go/<model-id>`
- Example models from `opencode models`:
  - `opencode-go/minimax-m3`
  - `opencode-go/minimax-m2.7`
  - `opencode-go/glm-5.3`
  - `opencode-go/glm-5.1`
  - `opencode-go/deepseek-v4-pro`
  - `opencode-go/kimi-k3`
  - (See `opencode models` for complete list)

**Output Capture**:
- Format: Plain text to stdout
- Success indicator: Model router header followed by response text
- No JSON output mode for print invocations

**Exit Code on Success**: `0`

**Auth/Rate-Limit Failure Indicators**:
- Stderr will contain connection/auth errors
- Look for: "Unable to connect", "Unauthorized", or provider-specific error messages
- Exit code on auth failure: non-zero

**Sample Invocation and Output**:
```bash
$ opencode run -m "opencode-go/glm-5.3" "Reply with exactly this and nothing else: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>"
```

```
> oc-router · glm-5.3
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
```

---

## codex

**Binary**: `codex`

**Non-Interactive Invocation Template**:
```bash
codex exec [OPTIONS] "<PROMPT>"
# or with stdin:
echo "<PROMPT>" | codex exec [OPTIONS]
```

**Model Selection**:
- Flag: `-m` (or `--model`)
- Default model: `gpt-5.6-luna` (OpenAI)
- Format: Model identifier from configuration (e.g., `gpt-5.6-luna`, `o3`, `claude-3-5-sonnet`)
- Example: `codex exec -m "gpt-5.6-luna" "your prompt"`

**Output Capture**:
- Format: Plain text to stdout
- Session metadata is printed first (workdir, model, provider, session id)
- Response text appears after the divider line
- Optional: Use `--json` flag for JSONL-formatted events (one JSON object per line)
- Optional: Use `-o <FILE>` to write only the last message to a file

**Exit Code on Success**: `0`

**Auth/Rate-Limit Failure Indicators**:
- Stderr contains error messages prefixed with timestamps
- Look for: "Error:", "failed to", "Authentication failed", or specific model provider errors
- Rate limit: Check model provider error messages (e.g., OpenAI rate limit text)
- Exit code on failure: non-zero

**Sample Invocation and Output**:
```bash
$ echo "Reply with exactly this and nothing else: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>" | codex exec -m "gpt-5.6-luna"
```

```
OpenAI Codex v0.144.6
--------
workdir: /Users/safwan/Code/Experiments/agent_surface
model: gpt-5.6-luna
provider: openai
approval: never
sandbox: danger-full-access
reasoning effort: low
reasoning summaries: none
session id: 01a0daa1-9fac-7520-bcee-1a68c266cff2
--------
user
Reply with exactly this and nothing else: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>

warning: Exceeded skills context budget of 2%. All skill descriptions were removed and 5 additional skills were not included in the model-visible skills list.
codex
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
tokens used
9,741
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
```

---

## agy (Antigravity CLI)

**Binary**: `agy`

**Non-Interactive Invocation Template**:
```bash
agy --model "<MODEL_ID>" --print '<PROMPT>'
```

**Model Selection**:
- Flag: `--model` (must appear before `--print`)
- Format: Model identifier string
- Example models from `agy models`:
  - `claude-sonnet-4-6`
  - `claude-opus-4-6-thinking`
  - `gemini-3.8-flash-high`
  - `gemini-3.8-flash-medium`
  - `gpt-oss-120b-medium`
  - (See `agy models` for complete list)

**Output Capture**:
- Format: Plain text to stdout
- Contains only the model's response, no metadata headers
- Optional: Use `--output-format json` for structured output (see `--print-timeout` for response timeout)
- Optional: Use `--output-format stream-json` with `--input-format stream-json` for streaming

**Exit Code on Success**: `0`

**Auth/Rate-Limit Failure Indicators**:
- Stderr contains error messages
- Look for: "error:", "invalid model selection", "not recognized", or provider-specific failures
- Exit code on failure: non-zero (typically 1 or 2)

**Sample Invocation and Output**:
```bash
$ agy --model "claude-sonnet-4-6" --print 'Reply with exactly this and nothing else: <<<SURFACE_CONTENT OK SURFACE_CONTENT>>>'
```

```
<<<SURFACE_CONTENT OK SURFACE_CONTENT>>>
```

---

## kimi (Kimi Code CLI)

**Status**: Installed but requires active subscription authentication.

**Binary**: `kimi` (at `/Users/safwan/.kimi-code/bin/kimi`)

**Non-Interactive Invocation Template** (when authenticated):
```bash
kimi -p "<PROMPT>"
# or with model selection:
kimi -m "<MODEL_ID>" -p "<PROMPT>"
```

**Model Selection** (when authenticated):
- Flag: `-m` (or `--model`)
- Format: Model identifier from configuration
- Default: Uses `default_model` from `~/.kimi-code/config.toml`

**Output Capture** (when authenticated):
- Format: Plain text to stdout
- Response text only, no metadata headers

**Exit Code on Success**: `0` (when authenticated)

**Auth/Rate-Limit Failure Indicators**:
- Stderr output: `error: failed to run prompt: provider.auth_error: 403 Your current subscription does not have access to Kimi Code right now. Upgrade your plan to keep coding with Kimi Code:`
- Look for: "provider.auth_error" with HTTP status codes
- URL provided for upgrading subscription
- Exit code on auth failure: non-zero

**Current Authentication Status**:
```
kimi version 0.39.0
error: failed to run prompt: provider.auth_error: 403 Your current subscription does not have access to Kimi Code right now. Upgrade your plan to keep coding with Kimi Code: https://www.kimi.com/code/#pricing
See log: /Users/safwan/.kimi-code/logs/kimi-code.log
```

---

## Implementation Notes

### Common Patterns

1. **Prompt Input**: Most CLIs accept prompts as arguments, though some support stdin piping
2. **Model Selection**: Each CLI uses its own model ID format and naming conventions
3. **Output Parsing**: Plain text output is default; some support JSON output modes
4. **Exit Codes**: All return 0 on success, non-zero on errors
5. **Authentication**: Each CLI manages credentials independently; auth failures include provider-specific error codes

### Adapter Recommendations

When building Python adapters:
- **Command construction**: Template-based with variable substitution for MODEL and PROMPT
- **Output parsing**: Capture stdout/stderr separately; some CLIs include metadata before responses
- **Error handling**: Parse stderr for error patterns; distinguish between auth, rate-limit, and execution errors
- **Subprocess**: Use `subprocess.run()` with shell=False and proper argument arrays
- **Timeouts**: Consider implementing per-CLI timeout values (opencode and kimi may require longer durations)
