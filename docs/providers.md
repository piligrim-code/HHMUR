# Explicit Provider Adapters

`api.providers` supplies synchronous `GeminiEvaluator` and `TelegramNotifier`
callbacks for the opt-in durable pipeline. This does not repair or import the
historical root `model.py`, scraper or root `main.py`. The existing demo remains
offline and never uses provider credentials.

## Configuration And Ownership

Install `requirements-test.txt` for the full tested environment. The HTTP
dependency is isolated in `requirements-providers.txt`. These are bounded
requirements, not a reproducible full dependency lock.

Both constructors reject actual network access unless `allow_network=True`
is passed explicitly. Only an explicitly supplied `httpx.MockTransport` bypasses
that gate for tests. A mock handler is trusted test code, not a security sandbox.
Imports and constructors do not send requests; calls on a live adapter do.

An application supplies secrets from its own deployment secret management.
No `.env`, credential file or environment variable is read by these adapters.
Never print constructor arguments, configuration dictionaries or request objects.

```python
from api.providers import GeminiEvaluator, TelegramNotifier
from api.durable import process_durable_batch, dispatch_outbox

# Variables are supplied by the application, not loaded by this module.
# Initialize the reviewed durable schema separately before starting workers.
with GeminiEvaluator(
    api_key=gemini_key, model=reviewed_model_id, policy=evaluation_policy,
    allow_network=True,
) as evaluate:
    result = process_durable_batch(vacancies, scope=policy_and_audience_scope,
                                   evaluate=evaluate)

with TelegramNotifier(token=telegram_token, chat_id=numeric_chat_id,
                      allow_network=True) as notify:
    delivery = dispatch_outbox(scope=policy_and_audience_scope, notify=notify)
```

This is a wiring example, not a deployment script or permission to use real
credentials. The application must confirm data-processing and notification
authorization before enabling either service. Adapters own their HTTP client
and supplied mock transport; close them or use the context managers. Closing
is idempotent, and subsequent calls fail. Instances are intended for one
synchronous worker; do not close a client while another thread is using it.

## Gemini Contract

- The model ID and policy are mandatory. There is no hardcoded retired model,
  fallback account, model download or local inference engine.
- Requests use the fixed Google Generative Language HTTPS host and v1beta
  `generateContent` endpoint. The API key is an `x-goog-api-key` header, not a
  query parameter. Arbitrary base URLs and model paths are rejected.
- Policy is passed separately as system instructions. Vacancy title/description
  are encoded as data in one user message. This does not prove resistance to
  semantic prompt injection; no returned content is executed as code or tools.
- Structured output uses `generationConfig.responseFormat.text` with
  `mimeType=APPLICATION_JSON` and a JSON schema for integer score 0/1 and a
  response string. This follows the current documented API shape rather than
  the legacy `responseMimeType` field. The selected model/account must support it.
- Exactly one completed `STOP` candidate is accepted. Blocked, truncated,
  tool/modality output and thought-only responses fail. Thought-marked text is
  not exposed as the evaluation. The assembled final text must be strict JSON
  with exactly `score` and `response`; duplicate keys and nonfinite values fail.
- `Evaluation` validates the score and nonempty explanation. Invalid output
  never silently becomes rejection. The model response cap is 16,000 characters.
- Input limits: 1,000 title characters, 20,000 description characters and
  12,000 policy characters. Output token cap defaults to 2,048, configurable
  from 1 to 8,192. Truncation causes failure, not acceptance of partial JSON.

The policy must define the actual selection criteria. A schema-valid answer
is not proof of a correct, unbiased or useful assessment. Account quotas, model
availability, quality and billing have not been tested with a real provider.

## Telegram Contract

- The endpoint is fixed to Telegram HTTPS `sendMessage`. Only a numeric chat ID
  is accepted; mutable usernames and arbitrary endpoints are not supported.
- Payloads have no parse mode or entities. Link previews and paid broadcasting
  are explicitly disabled. Messages are nonempty and capped at 3,500 UTF-16
  units; direct calls reject oversize text rather than splitting it into sends.
- Success requires `ok=true`, a positive integer message ID and the exact
  configured numeric chat ID in the returned Message. A response for another
  chat, malformed receipt or provider error fails the callback.
- HTTP failures, rate limiting and timeouts are not retried. A receipt proves
  API acceptance only, not that a person read the message.

Telegram requires the bot token in its URL path. Fixed host, TLS verification,
no redirects and log redaction reduce exposure; they do not make request URLs
safe to record. Do not use real request URLs as diagnostic artifacts.

## Transport And Error Boundaries

- HTTPX is used with environment proxy configuration disabled, certificate
  verification enabled, HTTP/2 disabled and zero transport retries. Redirects
  are not followed, including redirects to a different host.
- The timeout defaults to 20 seconds for each connect/read/write/pool operation.
  A separate 60-second budget is checked after response data becomes available
  and between unbuffered chunks. Both values are configurable up to 120 seconds.
  This is NOT a hard process-level deadline: DNS/TLS/platform blocking and an
  in-progress I/O operation can exceed the cooperative budget. Worker supervision
  remains a deployment responsibility.
- Response bodies are capped at 256 KiB. Oversize declared lengths or streaming
  data fail. Compressed responses are rejected; requests ask for identity
  encoding, avoiding an unbounded decompression step.
- Error diagnostics contain fixed provider/code/status fields, not response
  bodies, request URLs or secret-bearing transport exception text. There is
  no retry loop or key switching. Responses and clients have explicit cleanup.
- While a client is open, filters redact its exact credential (and URL-encoded
  form) from the known HTTPX/HTTP/1 transport loggers. Closing removes only that
  client's filters. This is not universal redaction: arbitrary tracing, custom
  handlers, request inspection, application logs and provider-side logs remain
  outside this boundary. Do not enable uncontrolled HTTP tracing in production.
- Callback errors flow into the durable pipeline's existing semantics: failed
  evaluation commits no new batch; failed delivery becomes uncertain and is
  never automatically requeued. See `docs/durable-pipeline.md` for recovery.

## Verification And Remaining Gates

Default tests inspect requests and inject response, timeout, redirect, rate-limit,
oversize, malformed-output and incorrect-recipient failures with synthetic keys.
They also check client cleanup and credential redaction in known logger paths.
Disposable PostgreSQL tests run the adapters through result/outbox commits and
uncertain-delivery handling using mocked HTTP responses, not Google or Telegram.

Before deployment: review the actual policy/model/scope, confirm account access
and recipient authorization, perform a separately approved synthetic live canary,
verify logs and worker deadlines, lock reviewed dependencies and configure
monitoring/retention. No live canary was performed during repository remediation.

Official API references consulted on October 2, 2026:

- `https://ai.google.dev/api/generate-content` (responseFormat and candidate fields)
- `https://core.telegram.org/bots/api` (sendMessage and Message receipt fields)
- `https://www.python-httpx.org/advanced/timeouts/`
- `https://www.python-httpx.org/advanced/transports/`
