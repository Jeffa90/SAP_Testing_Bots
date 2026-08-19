# How a run works

```
data workbook ──> Flow.build_cases() ──> [Case, Case, ...]
                                            │
                              for each case │
                                            ▼
                        connect ──> login (explicit | SSO)
                                            │
                              for each step │
                                            ▼
                    start_transaction ──> step function
                                            │
                                 ctx.enter()│ ctx.save()
                                            ▼
                                   read the status bar
                                            │
                              ┌─────────────┴─────────────┐
                        clean │                           │ a message
                              ▼                           ▼
                            PASS                  catalogue lookup
                                                          │
                            ┌────────────┬────────────────┴──────┐
                       no rule           rule + actions        rule, no actions
                            │                 │                     │
                            ▼                 ▼                     ▼
                    capture as unknown   remediate, recheck    classify
                         FAIL            (bounded by budget)   (BLOCKED_AUTH, ...)
                                                │
                                     cleared ───┴─── still failing
                                        │                │
                                       PASS      outcome + policy
                                                         │
                              continue │ fail_step │ abort_case │ abort_run
                                            │
                                            ▼
                        teardown: go_home, logoff, close  (ALWAYS)
```

## The pieces

**`core/status.py`** — turns the SAP status bar into a `StatusMessage` carrying
message class, number, type, text and parameters. `key` (`ME083`) is the identity
everything else matches on.

**`errors/catalog.py`** — rules, most specific first: class+number scoped to a
transaction, then class+number, then regex scoped, then regex. Declaration order
breaks ties, so later files layer as overrides.

**`errors/resolver.py`** — runs the remediation loop. Bounded three ways: per-rule
`max_attempts`, a global action budget, and loop detection when a rule's own fix
brings back the same message.

**`core/runner.py`** — cases, steps, policy composition and teardown. The step's
`on_error` and the catalogue's `on_exhausted` are combined by taking the stricter,
so an authorisation rule can stop a case inside a lenient step but not the reverse.

**`core/evidence.py`** — screenshots namespaced per run and case, each SHA-256 hashed
and recorded with the transaction, window title and SAP user it was taken under.
`verify()` re-hashes them, which matters when the screenshots are the audit record.

## Outcomes

| Outcome | Meaning |
|---|---|
| `PASS` | The step did what the test expected |
| `FAIL` | The system misbehaved — a defect |
| `ERROR` | The harness could not complete the check; the result is **unknown**, not bad |
| `BLOCKED_AUTH` | The user lacked the authorisation — a role finding, not a defect |
| `SKIPPED` | Not executed (excluded data, or an earlier step stopped the case) |
| `MANUAL` | Needs a human (Fiori approval steps, until the web driver lands) |

Cases and runs roll up to the most severe of their parts, ranked
`PASS < SKIPPED < MANUAL < BLOCKED_AUTH < FAIL < ERROR`. `ERROR` outranks `FAIL`
because a harness failure means the check never ran; `FAIL` outranks `BLOCKED_AUTH`
because a defect is a stronger finding than an untested step.
