# Design Review — llmform

> **HISTORICAL — resolved, retained for rationale.**
> The recommendations here were accepted on 2026-08-23 and are encoded as decisions
> D1–D4 in [SPEC.md §12](./SPEC.md#12-decision-log). §5's code findings refer to a
> skeleton that has since been deleted; recover it with `git checkout 7a1a350 -- .`
> if you need to read them against the source. §5.8's housekeeping items are stale —
> the tree is committed. §12's next steps are done.
> **Current spec: [SPEC.md](./SPEC.md).**

Review date: 2026-08-22
Reviewed: `docs/ARCHITECTURE.md`, `docs/RUNTIME.md`, `examples/support-bot/llmform.yaml`,
`internal/` + `pkg/` (~750 LOC, since deleted), and the incoming requirements document
(v0.1–v0.3 scope).

---

## 0. The headline finding

**The repository and the new requirements document describe two different products.**
Neither is wrong, but they cannot both be built, and several of their terms collide.

| Dimension | Repo today (`ARCHITECTURE.md`) | Requirements doc |
|---|---|---|
| Core metaphor | Terraform **provisioner** — CRUD remote resources, state file, `apply`/`destroy`/`import` | Terraform **workflow** — declare, validate, run; no remote resources |
| `policy` means | System prompt fragments (`"Be concise and friendly"`) | Runtime enforcement (`ALLOW`/`DENY`/`REQUIRE_APPROVAL`/`TRANSFORM`) |
| Enforcement lives in | `guardrail` resource | `policy` resource |
| Top-level unit | `assistant` | `agent` |
| Data layer | `data_source` + `knowledge_base` (RAG-first) | `source` + `tool` (operation-first) |
| Execution | Separate product (**Chatform**), separate binary | Same binary: `run`, `serve` |
| MVP commands | `init validate plan apply destroy state import fmt` | `init validate run serve` |
| State | Central (`llmform.tfstate`, remote backends, locking) | Absent from MVP; "state management" deferred to v0.3 |

The word `policy` is the worst of these. In the repo it is a *prompt*; in the doc it is
an *enforcement rule*. Every downstream discussion — validation, `plan`, evals, the
audit log — is ambiguous until this is settled.

**Recommendation: adopt the requirements doc's model, keep the repo's runtime.**
Reasons in §1–§3. Concretely:

- `resource.policy` (prompt rules) → **`instructions`** (or fold into the agent's prompt file)
- `resource.guardrail` (enforcement) → **`policy`**
- `assistant` → **`agent`**
- Delete `apply`, `destroy`, `import`, `state` from the roadmap
- Merge Chatform into llmform as `internal/runtime`; keep `pkg/chatform` as the SDK type package
- Keep the seven-stage gateway pipeline — it is the best asset in the repo

---

## 1. Why the provisioner model should go

`ARCHITECTURE.md` commits to Terraform's *mechanism*, not just its workflow: a state file
mapping config to `remote_id: pol_abc123`, refresh, drift detection, `import`, destroy
semantics. That mechanism only pays for itself when there are mutable remote resources
with server-side identity to reconcile.

Three problems:

**1. The remote resources barely exist.** The state example provisions an OpenAI
Assistants-API object. OpenAI has announced the Assistants API's retirement in favour of
the Responses API — verify the current sunset date before building on it, but the
direction is settled. Anthropic, Gemini, and Ollama have no comparable
"create a persistent assistant resource" surface at all. Strip Assistants and there is
almost nothing left to CRUD: a model name is a string you send per request, not a
resource you provision. `apply` would be a no-op with extra steps for most of the
provider matrix.

**2. State is the single most expensive subsystem in Terraform** — locking, backends,
drift, refresh, `import`, state surgery, and a decade of foot-guns. `ARCHITECTURE.md`'s
own Open Questions section is four state questions with no answers. That is a strong
signal.

**3. It contradicts local-first.** A developer should be able to clone, `validate`, and
`run` with no backend and no lifecycle ceremony. The requirements doc's success
criteria (§13) get this right.

**What to keep from Terraform:** the *workflow feel* — declarative config, offline
validation with precise errors, a reviewable diff before behaviour changes, and
reproducibility. All achievable without state. See §4 on redefining `plan`.

---

## 2. Why the gateway should stay

`RUNTIME.md`'s pipeline (auth → router → policy → execplan → evidence → contract →
observability) is genuinely better than the requirements doc's §5 flow diagram, which
shows a single policy engine sandwiched between model and tool.

The gateway already has the right shape: a linear stage chain over a shared
`RequestContext` with a `Blocked` short-circuit. `internal/gateway/gateway.go` is ~70
lines and correct. Keep it.

Two things the gateway has that the requirements doc omits entirely, and which should be
carried forward: **tenant/principal context** (`auth/stage.go`) and **trace + audit**
(`observability/stage.go`). See §6.

**But the stage list is not the same thing as policy hook points.** Today `policy` is one
stage that runs once, pre-flight. Policy needs to act at six points:

| Hook | Purpose | Present today? |
|---|---|---|
| `on_request` | Redact/tokenize PII before it reaches any provider | partial (pre-flight stage) |
| `on_model_call` | Provider/model restriction by data class; token + cost ceilings | no |
| `on_tool_call` | Argument-level authorization — the refund example | no |
| `on_tool_result` | **Filter data re-entering the prompt** | no |
| `on_response` | Egress filtering, secret leakage | partly (`evidence`, `contract`) |
| loop-level | Max iterations, cumulative cost, wall-clock timeout | no |

`on_tool_result` is the important omission in *both* documents. Redacting the user's
input does nothing about a `SELECT *` that returns a column of SSNs straight into the
next prompt. That is where real leakage happens.

Restructure so the policy engine is a set of interceptors invoked by the execution
planner at each hook, not a single stage in the chain. Stages remain for auth, routing,
and validation.

---

## 3. `TRANSFORM` needs tokenization, not just redaction

Redaction is destructive and self-defeating: redact an account number before the model
sees it, and the model can no longer pass it to `get_customer`.

The runtime needs **tokenization** — replace the value with a stable opaque handle, hold
the mapping in request-scoped memory, never send it to the provider, and re-substitute on
the way *out* to a tool and on the way out to the user. Model these as two distinct
transform kinds in the schema (`redact` — destructive; `tokenize` — reversible).

Without tokenization, "PII redaction" is a demo feature. With it, this is the reason
someone in a regulated industry adopts the framework.

---

## 4. Redefine `plan` as a config diff

The requirements doc §7 example is excellent, and note what it actually shows:

```
~ agent.support
model: gpt-5-mini → gpt-5
~ policy.refunds
max_amount: $100 → $1,000
WARNING: Agent gains additional financial authority.
```

Every line of that is derivable from **two revisions of config**. None of it needs a
state file or a remote refresh. This is `git diff` with a semantic, security-aware
renderer.

`internal/plan/plan.go` today diffs config against an empty state, so every plan is
"N to add" — structurally it is already a two-config differ with one side hardcoded
empty. Change the other side from "state" to "the previous config revision" and the
subsystem is done, minus the renderer.

**Recommendation:** `llmform plan [--from <git-ref>]`, defaulting to HEAD vs working
tree. Classify each change as cosmetic / behavioural / **privilege-escalating**, and make
the third category loud. Privilege escalation is the interesting signal: new tool granted,
policy relaxed, approval requirement removed, model swapped to a different provider,
budget raised.

This also makes `plan` shippable much earlier than v0.3, and it gives you the CI story
(`llmform plan` on a PR) with no infrastructure.

---

## 5. Concrete defects in the current code

Ordered by severity. All are cheap to fix now and expensive later.

### 5.1 Interpolation references attribute *values*, not resources — and isn't implemented

`examples/support-bot/llmform.yaml`:

```yaml
assistant:
  support_bot:
    model:        ${model.support_model.id}          # → "gpt-4o"
    policies:     [ ${policy.support_tone.name} ]    # → "support-tone"
    data_sources: [ ${data_source.product_api.url} ] # → "https://api.example.com/products"
    tools:        [ ${tool.search_products.description} ]
```

The last line resolves a tool reference to the string
`"Search the product catalog by keyword"`. These should be *addresses*
(`model.support_model`), not dereferenced attributes — otherwise two tools with the same
description are indistinguishable, and the dependency graph cannot be built from the
references at all.

Separately: **no interpolation is implemented.** `internal/config/load.go` calls
`yaml.Unmarshal` and returns; `${OPENAI_API_KEY}` survives into the config as a literal
string. The README and both design docs describe a feature that does not exist.

Fix: references are addresses; interpolation is a closed, typed set resolved at load
(`${env.X}`, `${var.X}`, `${secret.X}`) with no expressions, no functions, and no
string-concatenation into SQL.

### 5.2 The JSON Schema is never used

`schemas/llmform.schema.json` exists. `internal/config/load.go` does not reference it.
`Validate()` checks only that resource type names are in a hardcoded allowlist and that
instance names are non-empty. Everything inside a resource is `map[string]any` and
entirely unchecked — a model with `temperature: "hot"` validates clean.

`llmform validate` is the command that has to feel professional; right now it is a
type-name spellchecker.

### 5.3 No source positions on errors

`load.go` decodes straight into structs. There is no way to report `llmform.yaml:24:7`.
Decode via `yaml.v3` **Node** trees and thread positions through the AST. This is cheap
on day one and miserable to retrofit, and error quality *is* the felt experience of a
validator.

### 5.4 Untyped resource model

`pkg/types/config.go`: `ResourceMap map[string]map[string]any`. Every consumer
type-asserts. `policy/stage.go` already shows the cost:

```go
topics, _ := g["blocked_topics"].([]any)
for _, t := range topics {
    topic, _ := t.(string)
```

Three silent failure modes in four lines — a mistyped `blocked_topics` yields zero
enforcement and no error. For a security component, silently-degrading type assertions
are the wrong default. Decode into typed structs per resource kind.

### 5.5 Guardrail matching is substring containment

`policy/stage.go` blocks when the lowercased message contains the lowercased topic. This
will block "I have no medical advice to give" and miss every paraphrase. Fine as a
skeleton, but it must not be what ships — and the roadmap should name what replaces it
(classifier, embedding similarity, or an explicit LLM-judge policy kind) so the placeholder
doesn't become permanent.

### 5.6 Gateway can return a nil response

`Gateway.Handle` returns `rc.Draft` unconditionally after the stage loop. If no stage sets
`Draft` — misconfigured pipeline, planner early-returns — callers get `(nil, nil)`. Return
an explicit error when `Draft` is nil.

### 5.7 Docs describe ~5× the system that exists

`ARCHITECTURE.md` documents `internal/{schema,graph,apply,state,provider}`; none exist.
`RUNTIME.md` documents `internal/provider/{retrieval,tool,sql,llm}`; none exist. There is
no LLM provider of any kind — no OpenAI, no Ollama, no HTTP call to a model anywhere in
the tree. The docs read as shipped rather than planned.

For an open-source project this is a credibility risk. Mark unbuilt sections
**Planned** explicitly, or move them into `SPEC.md`.

### 5.8 Housekeeping

- **Zero commits.** `git log` fails — everything is untracked. Commit before refactoring.
- `bin/llmform` is a checked-in binary; confirm `.gitignore` covers it.
- `.DS_Store` present at repo root and untracked.
- `go.mod` says `go 1.22`; bump.
- `cmd/chatform` and `cmd/llmform` both exist with no shared entry story.

---

## 6. Gaps in both documents

1. **Principal in the policy context.** The gateway has `TenantID`, but no policy can
   reference the caller. Real rules key on *who* asks
   (`principal.role == "supervisor"`). Policy without a subject is half a policy, and
   adding it later breaks every rule already written. Add `principal` to the evaluation
   context now, with `run` injecting a local dev identity.
2. **Audit log as a first-class artifact.** `observability/stage.go` is a stub. A
   governance framework must emit a structured, append-only record of every policy
   decision: hook, policy ID, verdict, input hash, principal, timestamp. This is v0.1,
   not "Phase R3".
3. **Conversation/session state.** `serve` implies multi-turn; nothing in either document
   says where history lives, who owns trimming, or how it interacts with `on_request`
   policies (do you re-evaluate the whole history or the new turn?).
4. **Streaming vs. egress policy.** `chatform.Client` declares `Stream()`. You cannot
   redact bytes already flushed. Either buffer before egress policies (kills TTFB) or
   accept that response-side policies force non-streaming. Decide and document; v0.1
   should be non-streaming with the interface shaped to allow it.
5. **Reproducibility, honestly defined.** LLMs are not reproducible; the *config closure*
   is. Ship `llmform.lock` pinning model versions, prompt-file hashes, tool schemas, and
   the policy set. Cheap, and it is the truthful version of the Terraform pitch.
6. **Max agent loop iterations**, tool timeouts, and provider retry semantics — absent
   from both. Will otherwise be invented ad hoc in three places.
7. **Default posture.** Is a tool with no matching policy allowed or denied? Unspecified.
   Make it per-agent, defaulting to allow, with `default: deny` available.
8. **Eval metric contract.** `threshold: 0.90` of what? Define the metric interface, and
   store baselines so LLM-as-judge nondeterminism reports deltas rather than flapping CI.

---

## 7. The expression language decision

`allow_if: {amount: "<=100"}` is a stringly-typed mini-DSL. It will accrue boolean
composition, field paths, and string matching until it is a bad programming language with
no type checker — and once users have written policies against it, it cannot change.

**Recommendation: CEL** (`github.com/google/cel-go`). Typed, non-Turing-complete,
sandboxed, fast, Go-native, no external runtime. Rego/OPA is the alternative but is a much
heavier mental model for what are mostly single-predicate rules.

The decisive argument is **validate-time type checking**: if a tool's input schema has no
`amount` field, a CEL policy referencing `args.amount` fails at `llmform validate` —
offline, with a line number. That single behaviour is the best demo this project has, and
an ad-hoc string DSL cannot produce it.

```yaml
policies:
  refunds:
    on: tool_call
    tool: issue_refund
    rule: 'args.amount <= 100 || principal.role == "supervisor"'
    otherwise: REQUIRE_APPROVAL
```

---

## 8. `REQUIRE_APPROVAL` forces a resumable runtime

Under `run`, approval is a terminal prompt. Under `serve`, it means suspending a mid-flight
agent loop, persisting it, exposing an approval endpoint, and resuming — possibly in a
different process. That makes the agent loop a **serializable state machine**, not a
straight-line `for` over tool calls.

It is scoped v0.2, but it constrains the v0.1 runtime shape. Retrofitting resumability
into a straight-line executor is a rewrite.

Pick one explicitly:

- **(a)** Build the executor as a step machine from day one (`state → step() → state`),
  persisted behind an interface with an in-memory default. ~1 week now; saves the rewrite.
- **(b)** Declare approvals synchronous-only forever — the HTTP request blocks until
  approve/deny/timeout. Simpler, but rules out Slack/email approval, which is most of the
  value.

Recommend (a).

---

## 9. Positioning

Neither document has a competitive analysis, and several planned subsystems duplicate
mature tools:

- **Source + Tool YAML over Postgres, in Go** is close to Google's **MCP Toolbox for
  Databases** (`genai-toolbox`).
- **Declarative YAML evals with LLM-as-judge** is **promptfoo**.
- **Structured output from declarative model definitions** overlaps **BAML**.
- **`knowledge_base`/RAG** is a crowded field and is listed as a non-goal in the
  requirements doc while remaining a v1 resource type in `ARCHITECTURE.md` — another
  direct conflict between the two designs.

What nothing else owns: **runtime-enforced policy over LLM actions, plus a plan-time
security-impact diff.** That is the product. Everything else should be as boring and
interoperable as possible.

Two consequences:

- **Promote MCP sources into v0.1**, ahead of Postgres. Being an MCP *client* makes
  llmform a policy layer over the existing tool ecosystem rather than a competitor to it.
  `RUNTIME.md` already lists "API and MCP tools" as a backend — good instinct, wrong
  priority order.
- Consider making evals promptfoo-compatible rather than rebuilding them.

The failure mode for v0.1 as currently scoped is shipping six mediocre subsystems and one
good one.

---

## 10. Scope recommendation

**Cut from v0.1:** Postgres (pooling, secrets, type mapping, injection surface — HTTP and
MCP cover every demo), `knowledge_base`/RAG entirely, `apply`/`destroy`/`state`/`import`,
streaming.

**Add to v0.1:** principal, audit log, lockfile, MCP source, `on_tool_result` policy hook.

**On providers:** design the provider interface against the *harder* semantics — OpenAI
tool-calling and structured outputs — implement OpenAI first, then Ollama to prove the
abstraction isn't OpenAI-shaped. Building Ollama first feels aligned with local-first but
under-constrains the interface.

**On plugins:** keep interfaces internal-only until v0.3. When you do ship them, use
`hashicorp/go-plugin` (gRPC subprocess), **not** Go's `plugin` package — it is effectively
unusable across platforms and toolchain versions. Worth recording now so nobody starts
down that path.

See `SPEC.md` for the revised resource model, hook points, and milestones.

---

## 11. Validation tiers

Worth specifying explicitly, since `validate` is a headline command:

| Tier | Checks | Network |
|---|---|---|
| **L0** | Syntax, JSON Schema conformance | offline |
| **L1** | Reference integrity, cycles, unreachable resources | offline |
| **L2** | Tool schema validity; **CEL type-check against tool input schemas**; policy/hook compatibility | offline |
| **L3** | Provider reachable, model exists, source connects | `--online` only |

Default must be fully offline and fast enough for a pre-commit hook.

---

## 12. Immediate next steps

All five are resolved; see [SPEC.md §12](./SPEC.md#12-decision-log) and §11.

1. ~~**Decide the reconciliation** in §0~~ — adopted the requirements model (D1).
2. ~~Commit the current tree~~ — done; the skeleton was then deleted at `b2a7cd6`.
3. ~~Mark unbuilt doc sections **Planned**, or move them to `SPEC.md`~~ — ARCHITECTURE.md
   and RUNTIME.md carry superseded banners; the buildable design lives in SPEC.md.
4. ~~Decide §7 (CEL) and §8 (resumable runtime)~~ — D3 and D2, both accepted.
5. Build order stands, as SPEC.md §11 v0.1: positioned parser → typed AST → schema
   validation → reference graph → CEL policy engine with typed hook contexts → OpenAI
   provider → agent loop → `run`.
