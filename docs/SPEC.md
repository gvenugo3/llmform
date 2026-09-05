# llmform — Specification (v0.1 target)

Status: **Accepted** (2026-08-23), revised 2026-09-04. Supersedes `ARCHITECTURE.md` and
`RUNTIME.md`, which are retained only as the record of the abandoned provisioner design.
The skeleton that implemented that design was removed; recover it with
`git checkout 7a1a350 -- .`

See [§12](#12-decision-log) for the decisions this spec encodes and why.

---

## 1. What llmform is

A framework for declaring an LLM application in configuration — its models, data
access, tools, and **the rules governing what it may do** — and then validating,
diffing, testing, and running it.

The differentiator is the policy engine: rules enforced by the runtime at defined
interception points, not by asking the model nicely in a prompt.

### Principles

1. **Declarative** — config states what, not how.
2. **Policy outside the model** — enforcement is runtime code, never prompt text.
3. **Offline by default** — `validate` and `plan` contact nothing and execute nothing.
4. **Local-first** — clone, `validate`, `run` with no backend and no account.
5. **Provider-agnostic** — the core knows no vendor specifics.
6. **Reproducible closure** — the config, not the output, is what's pinned.
7. **Auditable** — every policy decision produces a tamper-evident record.
8. **Interoperable** — speak MCP; don't rebuild the tool ecosystem.
9. **No implicit authority** — anything that grants privilege (a tool, an approver, a
   detokenized field, a subprocess) is written down or unavailable.

### Explicit non-goals

Visual editors · multi-agent orchestration · hosted SaaS · vector DB / RAG ·
model training · HCL · Kubernetes · **remote-resource provisioning (`apply`/`destroy`/
state)** · **LLM-generated SQL** · a vendored model price table (§6.4).

---

## 2. Resource model

Six kinds. Addressed as `<kind>.<name>`, e.g. `agent.support`.

```
provider ──< model ──┐
                     ├──< agent >── policy
source ──< tool ─────┘
```

### 2.1 `provider`

```yaml
providers:
  openai:
    type: openai
    api_key: ${secret.openai_api_key}
  local:
    type: ollama
    endpoint: http://localhost:11434
```

v0.1 types: `openai`, `ollama`. v0.2: `anthropic`.

Credentials use `${secret.*}` by convention (§7.1), which in v0.1 is env-backed — but
routing them through the secret namespace is what keeps them out of `validate` and
`plan` output.

### 2.2 `model`

```yaml
models:
  default:
    provider: openai
    id: gpt-5
    temperature: 0.3
    max_tokens: 2048
    price:                        # required iff a cost ceiling or cost policy exists
      input_per_mtok:  1.25
      output_per_mtok: 10.00
      currency: USD
```

A model has no `output_schema`. The response contract belongs to the agent (§2.6), which
is the thing a caller talks to; the runtime derives the provider's structured-output
request from it. See [D6](#d6--the-response-contract-belongs-to-the-agent--2026-08-25).

`price` is declared, not fetched — see §6.4.

### 2.3 `source`

A backend, owning a **named operation catalog**. Sources declare parameterized
operations; tools expose them. SQL never leaves this layer, and is never generated.

```yaml
sources:
  product_api:
    type: http
    base_url: https://api.example.com
    auth: { type: bearer, token: ${secret.product_api_token} }
    operations:
      search_products:
        method: GET
        path: /products
        params:
          query: { type: string, required: true }
        returns: ./schemas/product_list.json

      get_customer:
        method: GET
        path: /customers/{id}
        classes: [pii, restricted]        # §2.3.1
        returns: ./schemas/customer.json

  tools_server:
    type: mcp
    command: ["npx", "-y", "@example/mcp-server"]   # subprocess — see §7.3
```

v0.1 types: `http`, `mcp`. v0.2: `postgres`.

Rationale for source-owned operations: keeps queries out of the agent layer, lets one
operation be exposed as several policy-differentiated tools, and gives a clean plugin
boundary.

#### 2.3.1 Data classes

An operation may declare `classes: [...]` — free-form labels naming the sensitivity of
what it returns. Classes are the only mechanism by which `data.classes` (§3.3) is
populated; there is no inference and no per-field classification in v0.1.

Semantics:

- When a `tool_result` from an operation is admitted into the run, that operation's
  classes are unioned into the run's class set (`RunState.Classes`).
- The set is **monotonic within a run** and survives suspension and resume. Once
  restricted data has entered the transcript, no later hook can un-see it.
- A `redact` transform covering every field of the result clears nothing: the class set
  is operation-level, so redaction does not lower it. Expose a redacted view as a
  separate operation with its own `classes` if you need a lower ceiling.
- Classes are declared strings. L2 checks that any class named in a CEL rule is declared
  by at least one reachable operation, so a typo is an offline error, not silent
  non-enforcement.

The class set is what makes rules like "restricted data may only reach a local provider"
enforceable at the `model_call` edge, on the turn *after* the data arrives.

### 2.4 `tool`

The exposure of a source operation to an agent, plus its schema and policy attachment
point.

```yaml
tools:
  search_products:
    source: product_api
    operation: search_products
    description: Search the product catalog by keyword
    input:  ./schemas/search_input.json
    output: ./schemas/product_list.json   # optional; must narrow the operation's returns
    timeout: 10s
    retries: 2
    detokenize: []                        # §3.5.2 — default: no re-substitution
```

`input`/`output` are JSON Schema. They are the contract the policy type-checker
validates CEL expressions against.

- `input` types `args` at the `tool_call` hook. It must be satisfiable by the
  operation's declared `params`; L2 checks this.
- `output` types `result` at the `tool_result` hook. It defaults to the operation's
  `returns`. If given, it must be a **narrowing** of `returns` — every property it
  declares must exist there with a compatible type, and it may drop properties but not
  add or widen them. L2 checks this. Narrowing is how one operation becomes several
  policy-differentiated tools.

### 2.5 `policy`

See §3.

### 2.6 `agent`

```yaml
agents:
  support:
    model: model.default
    instructions: ./prompts/support.md
    input:  ./schemas/support_request.json    # optional; default in §2.6.1
    output: ./schemas/support_answer.json     # optional; the response contract
    tools: [tool.search_products, tool.issue_refund]
    policies: [policy.refunds, policy.pii]
    max_iterations: 8
    max_cost_usd: 0.50
    default_tool_posture: allow   # or: deny
```

**References are addresses, never dereferenced attributes.** `model.default`, not
`${model.default.id}`. This is what makes the dependency graph derivable.

`default_tool_posture` decides what happens when a `tool_call` reaches no matching
policy: `allow` proceeds and records `NOT_APPLICABLE`; `deny` refuses. Default `allow`,
per-agent overridable. This closes the open question carried in the previous revision.

#### 2.6.1 Default schemas

If `input` is omitted, the agent's input is typed as:

```json
{ "type": "object",
  "properties": { "message": { "type": "string" } },
  "required": ["message"], "additionalProperties": false }
```

If `output` is omitted, `draft` at the `response` hook is typed as
`{ "text": "string" }` and no structured output is requested from the provider.

These defaults exist so that `request` and `response` policies are type-checkable at L2
without every project having to author two schemas. See
[D7](#d7--every-hook-context-is-statically-typed--2026-08-25).

---

## 3. Policy system

### 3.1 Shape and scope

```yaml
policies:
  refunds:
    on: tool_call
    match: { tool: tool.issue_refund }
    rule: 'args.amount <= 100 || principal.role == "supervisor"'
    otherwise: REQUIRE_APPROVAL
    approval:
      approvers: 'principal.role == "supervisor" && principal.tenant == run.principal.tenant'

  pii:
    on: tool_result
    match: { source: source.product_api, operation: get_customer }
    transform:
      - { kind: tokenize, fields: [ssn, card_number] }
      - { kind: redact,   fields: [internal_notes] }

  provider_restriction:
    on: model_call
    # no match: → every invocation of this hook for the attaching agent
    rule: '!data.classes.contains("restricted") || model.provider == "local"'
    otherwise: DENY
```

**Attachment gates; `match` filters.** A policy applies to a run only if the agent lists
it in `policies:` — a policy nobody attaches is dead config, and L1 reports it as
unreachable. Within an attached policy, `match` narrows which invocations of its hook it
sees; an omitted `match` means every invocation. There is exactly one question to ask of
a policy ("is it attached, and does it match?") and both halves are visible in the diff
`plan` renders. See [D5](#d5--attachment-gates-match-filters--2026-08-25).

`match` keys are a closed set, ANDed, exact-match only (no globs): `tool`, `source`,
`operation`, `model`, `provider`. Each is legal only at hooks where the corresponding
context exists (§3.3); L2 rejects the rest — `match: {tool: ...}` on a `request` policy
is an offline error.

`rule` evaluating true ⇒ `ALLOW`. Otherwise the `otherwise` verdict applies
(default `DENY`). A policy with `transform` and no `rule` always applies its transform
and allows.

### 3.2 Verdicts and outcomes

Authorable in `otherwise:`:

| Verdict | Meaning |
|---|---|
| `ALLOW` | Proceed |
| `DENY` | Abort the action; surface a structured error to the agent loop |
| `REQUIRE_APPROVAL` | Suspend, persist, await an out-of-band decision (§3.6) |

Recorded by the engine, never written by a user:

| Outcome | Meaning |
|---|---|
| `TRANSFORM` | A `transform` block mutated the payload at this hook |
| `NOT_APPLICABLE` | Attached, hook reached, `match` did not fire — distinguished from `ALLOW` for audit and for `default_tool_posture: deny` |

The split exists because the previous revision's five-value table mixed a grammar with a
log vocabulary: `TRANSFORM` is implied by the presence of `transform:`, and
`NOT_APPLICABLE` was never authorable at all.

### 3.3 Hook points

Invoked by the execution planner, not as gateway stages.

| Hook | Context available | `match` keys | Typical use |
|---|---|---|---|
| `request` | `principal`, `input`, `agent`, `run` | — | Ingress PII tokenization, injection screening |
| `model_call` | `+ model`, `messages`, `data.classes` | `model`, `provider` | Provider restriction by data class, token ceiling |
| `tool_call` | `+ tool`, `args` | `tool`, `source`, `operation` | Argument authorization |
| `tool_result` | `+ result` | `tool`, `source`, `operation` | **Egress filtering of data re-entering the prompt** |
| `response` | `+ draft` | — | Secret leakage, contract enforcement |
| `loop` | `+ iteration`, `cost_usd`, `elapsed` | — | Iteration / budget / timeout ceilings |

#### 3.3.1 Evaluation order and transform commit

Order is the **agent's `policies:` array order**, not the order of the top-level
`policies:` block. The agent is where privilege is granted, so the agent is where
precedence is read. L1 warns on an attachment list whose order changes meaning versus
declaration order, so a config that relies on the distinction says so out loud.

At a single hook invocation:

1. Every attached, matching policy is evaluated. Transforms are **staged**, not applied.
2. First `DENY` wins: evaluation stops and all staged mutations for that hook are
   discarded. Nothing partially-transformed ever reaches a provider or a tool.
3. `REQUIRE_APPROVAL` likewise stops evaluation; staged mutations are discarded and
   re-derived when the run resumes, since resume replays the hook.
4. Otherwise staged transforms commit in array order and the action proceeds.

Discarding on `DENY` is what makes the tokenize vault (§3.5.1) consistent: a denied hook
mints no tokens.

### 3.4 Expression language

**CEL** (the `cel-python` implementation). Typed, non-Turing-complete, sandboxed.

The environment is typed per hook, from declared schemas only:

| Variable | Type source |
|---|---|
| `input` | agent `input` schema, or the §2.6.1 default |
| `args` | tool `input` schema |
| `result` | tool `output`, defaulting to the operation's `returns` |
| `draft` | agent `output` schema, or `{text: string}` |
| `principal` | §4 |
| `model`, `tool`, `source`, `agent` | resource identity structs (name, type, provider) |
| `data.classes` | `list(string)`, members checked against declared classes (§2.3.1) |
| `run` | `{id, iteration, cost_usd, elapsed, principal}` |
| `messages` | `list(Message)` |

So `args.amount <= 100` against a tool with no `amount` field is a **`validate`-time
error with a line number**, offline. This is the property that justifies the choice — and
it is why §2.6.1 gives the agent-level hooks default schemas rather than leaving them
dynamically typed.

#### 3.4.1 JSON Schema profile for CEL

Schemas used as policy types are deliberately a closed subset of JSON Schema:

- objects with named `properties`, a string `required` list, and
  `additionalProperties: false`;
- homogeneous arrays whose `items` is one supported schema;
- `string`, `number`, `integer`, and `boolean` scalars;
- string-valued `enum`; and
- acyclic `$ref` references to the same document's `$defs`.

Validation-only keywords that do not change the CEL type, such as numeric bounds,
string lengths, `format`, and array size limits, remain valid and are enforced when the
payload is validated. Constructs that change or erase the static type are rejected at
L0: `oneOf`, `anyOf`, `allOf`, `not`, nullable/type arrays, tuple-form `items`, remote
or cyclic `$ref`, `patternProperties`, and open or schema-valued
`additionalProperties`. Because JSON Schema defaults an omitted `additionalProperties`
to `true`, policy-facing object schemas must state `additionalProperties: false`.

An unsupported construct produces a positioned diagnostic naming its schema path. It
never falls back to CEL `dyn`; accepting a schema that cannot be checked would silently
disable the guarantee this feature exists to provide. See
[D13](#d13--policy-facing-json-schema-is-a-closed-profile--2026-09-04).

### 3.5 Transforms

| Kind | Reversible | Behaviour |
|---|---|---|
| `redact` | no | Replace with a fixed marker |
| `tokenize` | yes | Replace with a stable opaque handle held in the run's token vault (§3.5.1); never sent to a provider; re-substituted only where §3.5.2 permits |

Tokenization is what makes the model able to *reference* a value it is never shown.

#### 3.5.1 The token vault

The token→plaintext mapping is **run-scoped**, not process-scoped, and lives behind an
interface:

```python
class TokenVault(Protocol):
    # Origin records whether plaintext entered from the caller or a tool result.
    def put(self, run_id: str, plaintext: str, origin: Origin) -> str: ...
    def resolve(self, run_id: str, token: str) -> tuple[str, Origin]: ...
    def purge(self, run_id: str) -> None: ...
```

- v0.1 default: in-memory, purged when the run reaches a terminal state.
- The persistent implementation (needed the moment a tokenizing agent also has a
  `REQUIRE_APPROVAL` policy) encrypts at rest with a key that is never in the config,
  and carries a TTL after which the run cannot resume and is marked `expired`.
- Tokens are stable within a run and meaningless across runs.

A process-memory-only vault would have made the previous revision's claim ("request-scoped
in memory") incompatible with its own resumable-loop decision: `RunState.Messages` is
persisted across processes, so tokens in the transcript would outlive their mapping.
See [D8](#d8--tokenization-is-backed-by-a-run-scoped-vault--2026-08-25).

#### 3.5.2 Re-substitution is a declared privilege

A token is a bearer handle to the plaintext. Re-substituting it anywhere the model
controls is an exfiltration path — the model can move a value it was never shown into any
tool that writes outward. So:

- **To tools:** only for input fields named in that tool's `detokenize:` list. Default
  empty. L2 errors if a `detokenize` field is absent from the tool's `input` schema, and
  warns when a tool with a non-empty `detokenize` targets a write operation (any HTTP
  operation whose method is not GET/HEAD, or any MCP tool not annotated read-only).
- **To the caller:** only for tokens whose `origin` is `OriginRequest` — the caller
  already had that plaintext. A value tokenized out of a `tool_result` is never restored
  on the way out, because the caller was never entitled to it.

### 3.6 Approvals

`REQUIRE_APPROVAL` suspends the run. The agent loop is therefore a **serializable step
machine** (`state → step() → state`), persisted behind an interface with an in-memory
default (D2).

**Approval is authorization, not a notification.** Any policy with
`otherwise: REQUIRE_APPROVAL` must declare `approval.approvers` — a CEL predicate over
the *approver's* principal, with the suspended run available as `run`. L2 errors if it is
missing; there is no implicit approver, because an unguarded approve path is a bypass for
every rule that routes through it.

The same predicate governs both drivers:

- `run` prompts locally, checking the acting principal (`--principal`, else the dev
  identity of §4) against `approvers`. If it cannot be satisfied it prints the predicate
  and the run ends `denied` rather than silently self-approving.
- `serve` (v0.2) resumes via `POST /runs/{id}/approve`, authenticating the caller and
  evaluating `approvers` against them.

Resume is refused unless `RunState.Closure` matches the running config's closure hash
(§4.1) — an approval granted under `amount <= 100` must not resume under a relaxed rule.

---

## 4. Principal

Every request carries a principal. Policies may reference it.

```python
class Principal(BaseModel):
    id: str = ""
    tenant: str = ""
    role: str
    scopes: list[str] = Field(default_factory=list)
    attrs: dict[str, str] = Field(default_factory=dict)
```

`run` injects a local dev principal (`role: developer`) unless `--principal` is given.
`serve` derives it from the configured auth method (v0.2).

### 4.1 Run state

```python
class RunState(BaseModel):
    run_id: str
    closure: str             # config-closure SHA-256; resume refuses on mismatch
    agent: str
    principal: Principal
    messages: list[Message] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)  # monotonic; see §2.3.1
    iteration: int = 0
    cost_usd: float = 0.0
    elapsed: timedelta = timedelta(0)
    pending: PendingApproval | None = None  # non-null means suspended

class Loop(Protocol):
    def step(self, state: RunState) -> RunState: ...
```

`Closure` and `Classes` are what make a resumed run enforce the same rules against the
same sensitivity ceiling as the run that suspended.

---

## 5. Audit log

Append-only, structured, tamper-evident. One record per policy decision, per applied
transform, and per approval decision.

```json
{
  "seq": 41,
  "prev_sha256": "…",
  "ts": "2026-08-22T14:29:03Z",
  "run_id": "run_01J...",
  "kind": "decision",
  "hook": "tool_call",
  "policy": "policy.refunds",
  "verdict": "REQUIRE_APPROVAL",
  "agent": "agent.support",
  "tool": "tool.issue_refund",
  "principal": {"id": "u_123", "role": "agent", "tenant": "acme"},
  "data_classes": ["pii"],
  "input_sha256": "…",
  "rule": "args.amount <= 100 || principal.role == \"supervisor\""
}
```

`kind` ∈ `decision | transform | approval`.

- `transform` records the policy, kind, and the **field paths** touched — never the
  values, and never the token→plaintext mapping.
- `approval` records the approver's principal, the `approvers` predicate, the decision,
  and the `run_id` and `seq` of the `REQUIRE_APPROVAL` record it answers.

`seq` is per-run and gap-free; `prev_sha256` chains each record to its predecessor so a
deletion or edit is detectable. Payloads are hashed, not stored, unless
`audit.record_payloads: true`. Sinks: stderr JSONL (default), file, OTLP.

---

## 6. Runtime

```
                  ┌─────────────── gateway stages ───────────────┐
request ──▶ auth ──▶ router ──▶ [ agent loop ] ──▶ contract ──▶ response
                                      │
                        ┌─────────────┴─────────────┐
                        │  policy hooks (§3.3) at   │
                        │  every model & tool edge  │
                        └───────────────────────────┘
```

- **auth** — resolve principal + tenant; fail closed.
- **router** — resolve `agent.<name>` to a compiled manifest; fail fast if undeclared.
- **agent loop** — step machine: assemble prompt → `model_call` hook → provider →
  `tool_call` hook → execute → `tool_result` hook → repeat until answer,
  `max_iterations`, or budget. Suspends on `REQUIRE_APPROVAL`.
- **contract** — validate against the agent's `output` schema; `response` hook.
- **observability** — spans + audit, alongside all stages.

### 6.1 Streaming

v0.1 is **non-streaming**. Response-egress policies require a complete draft; the
provider interface is shaped to allow streaming later for agents with no `response`
policies.

### 6.2 Retries

A tool's `retries` are transport-level. Each attempt is a distinct action, so:

- `tool_call` policies are **re-evaluated per attempt** — fail-closed, since principal
  scopes or the class set may have changed between attempts.
- Retries do **not** increment `iteration` (they are not model turns) but do accrue
  `elapsed` and any cost incurred.
- A `DENY` on a retry ends the retry sequence; it is not itself retried.
- Provider-level retries on `model_call` follow the same rule.

### 6.3 Budgets

`max_iterations`, `max_cost_usd`, and wall-clock are enforced at the `loop` hook, checked
before each model call. Exceeding a ceiling ends the run `failed` with a structured
reason; it is not a policy `DENY` and does not need a policy to be declared.

### 6.4 Cost

Cost is computed from **declared** prices (`model.price`, §2.2) and provider-reported
token usage. There is no bundled price table: a vendored list of vendor prices is stale
the week it ships, and silently wrong cost enforcement is worse than none.

- `max_cost_usd` or any CEL rule referencing `cost_usd` requires `price` on every model
  reachable from that agent. L2 errors otherwise, naming the model and the ceiling.
- Providers with no per-token billing (`ollama`) are priced zero implicitly and need no
  `price` block.
- Mixed currencies across one agent's models are an L2 error; there is no FX.
- `price` is pinned in the lockfile, so `plan` can render a price change as a diff line
  rather than discovering it at runtime.

---

## 7. Configuration language

YAML, decoded via PyYAML **Node** trees using YAML 1.2 scalar semantics so every AST
node carries `file:line:col`. In particular, the policy key `on` remains a string rather
than the YAML 1.1 boolean value accepted by PyYAML's default loader.

The v0.1 configuration grammar is published as Draft 2020-12 JSON Schemas under
`llmform.schemas`: one artifact for each of the six resource kinds, plus variables and
the top-level project. Every artifact carries `x-llmform-schema-version: "0.1"`; this is
the schema version recorded by the lockfile. The committed artifacts are generated from
the strict typed models with `python scripts/generate_config_schemas.py`, while runtime
validation reads only the packaged artifacts and remains fully offline.

### 7.0 Project discovery and flat-file merge

Starting from a file or directory, discovery walks toward the filesystem root and uses
the nearest directory containing either `llmform.yaml` or a `*.llmform.yaml` fragment.
Only files directly in that directory belong to the project; discovery does not recurse.

If present, `llmform.yaml` loads first. All `*.llmform.yaml` fragments then load in
lexical filename order. A fragments-only project is valid. Resource blocks merge by
address, but declaring the same `<kind>.<name>` in two files is an error that cites both
locations. Singleton top-level keys, including `version` and `audit`, may appear in only
one file. There is no last-write-wins behavior.

File order affects diagnostic ordering only. It cannot override a resource or change
policy precedence; policy evaluation order comes exclusively from each agent's explicit
`policies:` list. This is the flat v0.1 model—recursive includes and modules remain out
of scope. See [D14](#d14--projects-use-nearest-root-flat-file-merging--2026-09-04).

### 7.1 Interpolation

A closed, typed set resolved at load time:

| Form | Source |
|---|---|
| `${env.NAME}` | process environment |
| `${var.NAME}` | `variables:` block / `--var` |
| `${secret.NAME}` | secret provider (v0.1: env-backed) |

No expressions, no functions, no concatenation into queries. `validate` and `plan` never
print resolved secret values. Values from `${secret.*}` are additionally scrubbed from
error messages and from audit payloads.

### 7.2 Lockfile

`llmform.lock` pins the config closure:

- SHA-256 of every config file, prompt file, and JSON Schema
- model IDs **as written** and their declared `price` blocks
- the normalized policy set, including attachments and `match` blocks
- the source operation catalog, including MCP `command` argv hashes (§7.3)
- the config schema version and the llmform minor version that wrote it

It does **not** pin a resolved model version: resolving one requires a network call,
which would break offline-by-default, and providers do not expose stable content hashes
for a model ID anyway. This is the honest form of "reproducible" — the closure is pinned;
model output is not.

`llmform lock` writes or updates it; `init` writes one; `validate --locked` fails on
drift. The previous revision described drift-checking with nothing to produce the file.

### 7.3 Subprocess trust

An `mcp` source's `command` is arbitrary local code execution described by config. Given
the local-first pitch — clone, `validate`, `run` — that has to be explicit:

- `validate` (all tiers, including `--online`) and `plan` **never** launch a source.
  Offline-by-default means "executes nothing", not just "connects to nothing"; `--online`
  L3 checks connectivity for `http` sources and confirms an `mcp` command is resolvable
  on `PATH` without running it.
- `run` and `serve` launch MCP subprocesses only after the command's argv hash is
  recorded in `llmform.lock`. An unrecorded or changed argv prompts for confirmation and
  then records it; `--trust-sources` accepts non-interactively for CI.
- The prompt shows the full argv. `plan` classifies any change to an MCP `command` as a
  **privilege** change (§8).

---

## 8. CLI

| Command | v | Behaviour |
|---|---|---|
| `llmform init` | 0.1 | Scaffold a project, including `llmform.lock` |
| `llmform validate` | 0.1 | L0–L2 offline; `--online` adds L3; `--locked` checks the lockfile |
| `llmform fmt` | 0.1 | Canonical YAML formatting |
| `llmform lock` | 0.1 | Write / update `llmform.lock` |
| `llmform run <agent>` | 0.1 | Run locally; interactive approvals |
| `llmform plan` | 0.2 | Config diff with security + cost impact |
| `llmform serve` | 0.2 | HTTP API over configured agents |
| `llmform test` | 0.3 | Run evaluations |

`serve` moved to v0.2, where the auth, sessions, and async approvals it depends on
already live. Shipping an HTTP surface a version before its authorization model meant an
unauthenticated `POST /runs/{id}/approve` — a bypass for every `REQUIRE_APPROVAL` policy.
The v0.1 exit criterion exercises only `validate` and `run`.
See [D9](#d9--serve-ships-with-its-authorization-model-not-before-it--2026-08-25).

### Validation tiers

| Tier | Checks | Network | Executes |
|---|---|---|---|
| L0 | Syntax, JSON Schema conformance | no | no |
| L1 | Reference integrity, cycles, unreachable resources, unattached policies | no | no |
| L2 | Tool schema validity, output-narrowing, **CEL type-check against declared schemas**, hook/`match` compatibility, declared data classes, `approvers` presence, price coverage for cost ceilings | no | no |
| L3 | Provider reachable, model exists, HTTP source connects, MCP command resolvable | `--online` | no |

### Diagnostics

Diagnostic codes are stable public identifiers. The namespace is partitioned by layer:

| Range | Layer |
|---|---|
| `LLMF000`–`LLMF099` | discovery and positioned YAML |
| `LLMF100`–`LLMF199` | typed configuration shape |
| `LLMF200`–`LLMF299` | interpolation and secrets |
| `LLMF300`–`LLMF399` | references and dependency graph |
| `LLMF400`–`LLMF499` | schema profile and CEL compilation |
| `LLMF500`–`LLMF599` | cross-resource L2 semantics |

Human output is ordered by file, line, column, and code, capped at 50 findings, and ends
with error/warning totals plus an omitted count when capped. A diagnostic may carry a
context line, actionable hint, and spelling suggestion as separate fields.

`validate --json` emits an object with `diagnostics` and `summary`. Each diagnostic has
`code`, `severity`, `message`, `file`, `line`, `column`, `context`, `hint`, and
`suggestion`; the summary has `errors`, `warnings`, `total`, `shown`, and `omitted`.
Warnings alone exit zero; any error exits one.

### `plan`

`llmform plan [--from <git-ref>]` — diffs the config closure between two revisions.
Every change is classified:

- **cosmetic** — descriptions, formatting
- **behavioural** — prompts, temperature, model swap within a provider
- **privilege** — tool granted, policy detached or relaxed, approval requirement removed,
  `approvers` widened, `detokenize` field added, `default_tool_posture` flipped to
  `allow`, data class dropped from an operation, budget or price raised, provider changed,
  MCP `command` altered

Privilege changes render as warnings and set a non-zero exit under
`--fail-on-privilege`, which is the CI hook.

---

## 9. HTTP API (`serve`, v0.2)

```
POST /v1/agents/{name}/runs     → { run_id, status, output? }
GET  /v1/runs/{id}              → run state
POST /v1/runs/{id}/approve      → resume a suspended run
POST /v1/runs/{id}/deny
GET  /healthz
```

`status` ∈ `completed | awaiting_approval | denied | failed | expired`.

Every endpoint except `/healthz` requires an authenticated principal. `approve`/`deny`
additionally evaluate the pending policy's `approval.approvers` predicate against that
principal (§3.6) and refuse if the closure hash or the vault TTL has lapsed
(`expired`).

Sessions: `POST /v1/agents/{name}/runs` accepts an optional `session_id`; history is held
by a session store (in-memory default, pluggable). Ingress policies evaluate the new turn
only; `tool_result` policies have already sanitized prior turns. The session store owns
trimming, and must preserve the accumulated class set (§2.3.1) even when it drops the
messages that raised it — otherwise trimming silently lowers the sensitivity ceiling.

---

## 10. Package layout

Implemented today:

```
src/llmform/
  cli.py                Typer command entrypoint
  diagnostics.py        positioned diagnostics and renderers
  types.py              Principal, Message, RunState, Verdict, approval state
  config/               discovery, YAML, typed AST, interpolation, L0–L2
  schemas/              packaged Draft 2020-12 configuration schemas
  policy/cel/           schema types, hook environments, parser and type-checker
tests/                  unit and golden diagnostic fixtures
scripts/                deterministic schema generation
```

Planned v0.1 additions:

```
src/llmform/
  graph/                DAG, cycles, topo order
  policy/
    engine/             hook dispatch, verdicts, transform staging
    transform/          redact, tokenize
    vault/              TokenVault (§3.5.1)
  runtime/
    gateway/            stage chain (auth, router, contract, observability)
    loop/               agent step machine
    session/            history store
    approval/           suspend/resume store, approver authorization
    cost/               usage → declared price accounting
  provider/
    llm/                openai, ollama
    source/             http, mcp (incl. subprocess trust, §7.3)
  audit/                chained record writer + sinks
  plan/                 config diff + impact classification
  lock/                 closure lock read/write
examples/support-bot/
```

Modules not intended as public API remain private by convention and are omitted from
the package exports. Public SDK types live under `llmform.types`; transport helpers live
under `llmform.client`.

Plugins stay internal-only until v0.4. External plugins use isolated subprocesses over
gRPC or MCP rather than importing untrusted provider code into the runtime process.

---

## 11. Roadmap

### v0.1 — policy-enforced agent, local
Positioned parser → typed AST → L0–L2 validation → reference graph → CEL policy engine
with all six hooks and statically typed contexts → transforms (redact + tokenize) with
run-scoped vault → data classes → principal → chained audit log → declared-price cost
accounting → OpenAI + Ollama providers → HTTP + MCP sources with subprocess trust →
resumable agent loop with closure-pinned resume → lockfile → `init`, `validate`, `fmt`,
`lock`, `run`.

**Exit criterion:** `llmform validate` catches a CEL policy referencing a field the
tool's schema doesn't have, offline, with a line number — and `llmform run support`
denies an over-limit refund without the model's cooperation.

### v0.2 — governed service
`plan` with privilege classification and cost impact · `serve` with auth, sessions, and
async approvals · persistent encrypted token vault · Postgres source · cost and rate
policies · Anthropic provider.

### v0.3 — verification
Evaluations, policy tests, tool-invocation assertions, eval baselines, `test`.

### v0.4 — ecosystem
Modules · provider SDK · isolated subprocess plugin protocol · registry.

---

## 12. Decision log

### D1 — Resource model: adopt agents/sources/tools/enforcement-policies ✅ 2026-08-23

The Terraform *provisioner* model (state file, `apply`, `destroy`, `import`, `remote_id`)
is dropped. There are too few real remote resources to reconcile — the primary one, the
OpenAI Assistants API, is being retired in favour of Responses, and no other provider has
an equivalent surface. State is also the most expensive subsystem in Terraform and
contradicts local-first.

Renames: `assistant` → `agent`; `guardrail` → `policy`; the old prompt-fragment `policy`
→ agent `instructions`. `knowledge_base` is dropped entirely (RAG is a non-goal).

What survives from the old design: the gateway stage chain and the trace/audit stage.

### D2 — Agent loop is a resumable step machine ✅ 2026-08-23

`state → Step() → state`, serializable, persisted behind an interface with an in-memory
default. See §4.1 for the state struct as revised.

`run` drives it in-memory and prompts on `Pending`; `serve` persists and resumes via
`POST /runs/{id}/approve`. Chosen over synchronous-only approvals because out-of-band
approval (Slack, email, ticket) is most of `REQUIRE_APPROVAL`'s value, and retrofitting
resumability into a straight-line executor is a rewrite rather than a refactor.

### D3 — CEL is the policy expression language ✅ 2026-08-23

`cel-python`. Typed, sandboxed, non-Turing-complete, and embedded in the validator.

Decisive property — offline type-checking of rules against declared schemas:

```
$ llmform validate
llmform.yaml:31:11: error [LLMF411]: undefined field 'amount'
  on tool.issue_refund (schema: refund_input.json)
  did you mean 'amount_cents'?
Summary: 1 error, 0 warnings
```

Rego/OPA was rejected as a heavier mental model for mostly single-predicate rules with
weaker static checking against tool schemas. A closed predicate set was rejected because
it grows into a language anyway, without a type checker.

### D4 — "Chatform" is retired ✅ 2026-08-23

One package, one CLI, one name. The runtime lives under `llmform.runtime`; shared SDK
types live in `llmform.types`. Two names for one project splits the documentation and the
pitch before there are any users. The control-plane/data-plane framing survives as an
internal package boundary, not as a separate product.

### D5 — Attachment gates, `match` filters ✅ 2026-08-25

A policy applies only if an agent attaches it; `match` narrows within that. The previous
revision had both mechanisms with no stated precedence, so "does this rule apply here"
had two possible answers — unacceptable in an enforcement engine. Attachment was chosen
as the gate because per-agent privilege is what `plan` needs to diff, and an unattached
policy is then detectable dead config (L1) rather than an invisible global.
`match` keys are a closed, ANDed, exact-match set, legal only where their context exists.

### D6 — The response contract belongs to the agent ✅ 2026-08-25

`model.output_schema` is removed; agents declare `input` and `output`. Two resources
declaring the response shape left no rule for which wins, and the schema is a property of
the interface a caller talks to, not of the model that happens to be behind it. Swapping
`model.default` for a cheaper model must not silently change the contract.

### D7 — Every hook context is statically typed ✅ 2026-08-25

D3's whole argument is offline type-checking, but `input` at `request` and `draft` at
`response` had no declared schema, so two of six hooks were dynamically typed and L2
coverage was quietly partial. Agents now declare `input`/`output`, with defaults
(§2.6.1) so the common case authors nothing. Same reasoning for data classes: `classes`
is declared on source operations (§2.3.1) and L2 checks class names in rules, rather than
`data.classes` being a variable no config could populate.

### D8 — Tokenization is backed by a run-scoped vault ✅ 2026-08-25

"Request-scoped in memory" contradicted D2: `RunState.Messages` is persisted and may
resume in another process, so tokens in the transcript would outlive their mapping and
re-substitution would fail exactly for the regulated users tokenization is for. The
mapping moves behind `TokenVault` (§3.5.1) keyed by run ID — in-memory default, encrypted
and TTL'd when persisted.

Rejected: forbidding tokenize+async-approval at L2 (loses Slack/email approval, the
main reason approvals are asynchronous); re-tokenizing on resume (impossible — the
plaintext was stripped).

The corollary is §3.5.2: a token is a bearer handle, so re-substitution is a declared
privilege (`detokenize`) and never flows to a caller who did not supply the value.

### D9 — `serve` ships with its authorization model, not before it ✅ 2026-08-25

`serve` moves to v0.2. In v0.1 it would have exposed
`POST /runs/{id}/approve` with auth scheduled for the next version — a bypass for every
`REQUIRE_APPROVAL` policy, which is the feature `serve` most needs. Relatedly,
`approval.approvers` is now mandatory on any `REQUIRE_APPROVAL` policy: there is no
implicit approver, and `run` checks the same predicate it will check under `serve`.

### D10 — Cost comes from declared prices ✅ 2026-08-25

Three features referenced cost (`max_cost_usd`, the `loop` hook's `cost_usd`, `plan`'s
cost impact) with no source for a price. Prices are declared on the model and pinned in
the lockfile (§6.4); a vendored price table is a non-goal because it is stale the week it
ships and silently wrong enforcement is worse than none. L2 requires price coverage
whenever a cost ceiling or cost rule exists.

### D11 — Offline means executes nothing ✅ 2026-08-25

An `mcp` source's `command` is local code execution declared in config. `validate` and
`plan` never launch one; `run`/`serve` launch only argv recorded in the lockfile, with a
confirmation prompt on change and `--trust-sources` for CI (§7.3). Without this,
"clone, validate, run" was an invitation to execute a stranger's `npx`.

### D12 — Lockfile pins what can be pinned offline ✅ 2026-08-25

`llmform lock` is added — drift-checking existed with nothing to write the file. The
lockfile no longer claims to pin "resolved model IDs" (resolution needs a network call,
and providers expose no stable content hash per model ID) or "provider versions"
(providers are internal packages until v0.4). It pins file hashes, model IDs as written,
declared prices, the normalized policy set with attachments, the operation catalog with
MCP argv hashes, and schema/binary versions (§7.2).

### D13 — Policy-facing JSON Schema is a closed profile ✅ 2026-09-04

The supported profile is defined in §3.4.1. Open objects, unions, tuple arrays, nullable
types, and references outside acyclic local `$defs` are rejected at L0 rather than
mapped to CEL `dyn`. In particular, `additionalProperties: true` (including its implicit
default when omitted) is rejected. Treating undeclared properties as absent from the
type environment was rejected because the runtime contract would still admit data that
the policy author could neither name nor type-check. Requiring closed objects makes the
runtime validation boundary and policy type environment describe the same values.

### D14 — Projects use nearest-root flat-file merging ✅ 2026-09-04

Discovery and merge semantics are defined in §7.0. Both a conventional primary file and
lexically ordered fragments are supported, including fragments-only projects. The
nearest ancestor wins so a command run inside a nested project cannot accidentally load
an outer project. Duplicate addresses and singleton keys fail with both source locations
rather than overriding. Recursive discovery and explicit include order were rejected:
the former makes project closure surprising, while the latter lets load order alter
security-sensitive meaning.

### Still open

- Is `llmform` available on GitHub and pkg.go.dev under the intended org?
- Vault key management for the persistent implementation: operator-supplied key,
  OS keychain, or KMS interface. Needed for v0.2, not v0.1.
- Whether `redact` should be able to lower a data class when it provably covers every
  field carrying it — currently no (§2.3.1), which is conservative but coarse.
- Whether `match` needs an `agent` key once modules land in v0.4 and one policy may be
  attached by many agents.
