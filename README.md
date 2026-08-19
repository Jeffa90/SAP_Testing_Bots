# saptest

Role-aware UAT automation for SAP S/4HANA. Drives the real SAP GUI client as a real
user, remediates known SAP errors from a maintained catalogue, and writes the
evidence back into the test-plan workbooks the team already uses.

Replaces seven standalone bot scripts in which 16 helper functions were duplicated
across 6 files, leaving the transaction sequence as the only thing a new test needs
to define.

```bash
pip install -e ".[windows]"        # on the Windows test machine
saptest doctor --region au         # check the environment first
saptest ui                         # or drive it from the browser
```

Try it without a SAP landscape at all:

```bash
pip install -e .
saptest run --region au --flow p2p.pr_create \
            --data tests/fixtures/pr_test_data.xlsx --driver demo
```

---

## Why SAP GUI Scripting

The requirement is to test what a user's roles actually permit, behaving as that
user. Only one approach satisfies that while being robust:

| Approach | Enforces the user's roles? | Robust? |
|---|---|---|
| **SAP GUI Scripting** (this library) | Yes — the kernel runs the full dialog authorisation path | Yes, stable element ids |
| Keystrokes + image matching | Yes | No — breaks on resolution, DPI, theme, GUI version, tab order |
| BAPI / RFC direct | **No** — BAPIs run their own checks, and `S_TCODE` is never checked | Very |

Because scripting drives the actual SAP GUI client, every request travels the
ordinary dialog path: `S_TCODE`, every authorisation object, every field check, all
applied exactly as they would be for a person. Anything that bypasses the GUI stops
testing the thing under test.

A fallback keystroke driver is planned for landscapes where Basis has not enabled
`sapgui/user_scripting`. Flows need no changes to run on it — see
[Field bindings](#field-bindings).

## What it does differently

**Errors are identified by message class and number, not English text.** SAP exposes
`MessageId` and `MessageNumber` on the status bar (`ME` `083`). That identity is
stable across logon languages and never embeds runtime data. Matching on rendered
text cannot survive a German client, and a rule keyed on
`"Item 10 Delivery Date 08.09.2025 is in the past"` stops matching the day that date
passes.

**Authorisation gaps are a distinct outcome.** `BLOCKED_AUTH`, never `FAIL`. A user
who lacks a role produced no test result at all, so it is a role finding rather than
a defect — and reports group it by user, which is the answer the exercise exists to
produce.

**Teardown always runs.** A case that aborts still returns to Easy Access, logs off
and releases the session, so the next case starts clean. The pattern this replaces
(`except Exception: break`) left SAP on an arbitrary screen still logged in, turning
one real failure into a cascade of phantom ones.

**The catalogue grows from real runs.** Any message with no rule is captured with its
transaction, screen, user, case and step, then turned into a draft rule by
`saptest catalog review`. Drafts generalise embedded digits to `\d+`, so a rule
written from one message still matches the next one.

## Commands

| Command | Purpose |
|---|---|
| `saptest doctor` | Preflight: environment, scripting, profile, bindings, catalogue, data |
| `saptest run` | Execute a flow and write the reports |
| `saptest inspect` | Dump the current SAP screen's elements, to build field bindings |
| `saptest catalog review <run>` | Turn captured unknown messages into draft rules |
| `saptest catalog validate` | Check every rule references a real handler and field |
| `saptest ui` | Local web interface |
| `saptest flows` / `regions` | What is available |

## What a run produces

```
runs/run_20260819_015548/
  summary.html            outcome tally, cases, authorisation blocks by user
  summary.xlsx            the same, as Cases / Steps / Authorisation sheets
  summary.json            machine-readable, drives the UI's history
  run.log
  unknown_messages.jsonl  messages with no catalogue rule yet
  evidence.jsonl          every screenshot, SHA-256 hashed, with its context
  1/  2/  4/              screenshots per case
  test_plans/
    AU_PTP_Standard PO_1205_A03_1.xlsx    Actual Result + Pass/Fail filled in,
                                          screenshots on the numbered step tabs
```

## Configuration

Everything that differs between landscapes is YAML. Adding a region should never
require a code change.

- `profiles/regions/<region>.yaml` — connection, date and decimal formats, default
  values remediation draws on, plant exclusions, which drivers to try, template
  paths, and the test-plan cell layout.
- `profiles/bindings/*.yaml` — logical field name to SAP element id, plus the
  spreadsheet-column to field map.
- `catalog/messages.yaml` — error rules. Region files can layer over the base.

### Field bindings

A flow names a field once; each driver resolves it its own way:

```yaml
item.material:
  id: "wnd[0]/usr/tbl.../ctxtMEREQ3211-MATNR[3,{row}]"   # scripting driver
  label: "Material"                                       # fallback lookup
  image: "MaterialField.png"                              # keystroke driver
```

`{row}` is substituted per table line, so one binding serves every line of a
multi-line requisition.

**Element ids must be captured from your own system.** They embed dynpro and
subscreen numbers that vary by release and configuration, so shipping guesses would
fail silently. With SAP GUI open on the screen you want to bind:

```bash
saptest inspect --filter MATNR --yaml
```

which prints pasteable YAML. The bindings in `profiles/bindings/pr_au.yaml` are
marked `TODO` where this is still needed; `saptest doctor` reports how many remain.

## Adding a flow

Steps are methods. The decorator carries the metadata the test plan needs, and
`on_error` is what delivers "stop this case but carry on with the run":

```python
@register_flow
class PrCreateFlow(Flow):
    name = "p2p.pr_create"
    template = "standard_po"

    def build_cases(self, data, profile):
        ...                                     # rows -> test cases

    @step(id="1", name="Create Purchase Req.", tcode="ME51N",
          expected="PR saves with correct information",
          on_error=ErrorPolicy.ABORT_CASE)
    def create_pr(self, ctx):
        ctx.shot("1A")
        for index, row in enumerate(ctx.case.rows):
            ctx.fill_row(row, self._columns, row=index)
            ctx.enter()                         # submits and checks the status bar
        result = ctx.save()
        ctx.remember("pr_number", extract_document_number(result.message))
        ctx.shot("1B")
```

Logon, navigation, screenshots, status-bar checking, remediation, retry and teardown
are the runner's job. `ctx.press()` checks by default — every key press is a round
trip that can raise a message, and opting *in* to checking is how a "not authorised"
on an innocuous keystroke gets swallowed.

`on_error`: `continue` (log and move on) · `fail_step` · `abort_case` (stop the case,
still run teardown) · `abort_run`. A catalogue rule can escalate but not relax it, so
an authorisation rule stops a case even inside a lenient step.

## Deployment

**The tester does not need Python installed.** The app ships as a PyInstaller
`--onedir` folder containing `saptest.exe` and an embedded interpreter. Copy the
folder, double-click, the browser opens. No pip, no admin rights.

```bash
pip install -e ".[windows,build]"
python packaging/build.py          # -> dist/saptest/
```

**The real constraint is the desktop session, not Python.** The runner must execute
on the same machine as SAP GUI, in the same interactive session — this is inherent to
SAP GUI Scripting, not to the language. It will not run as a Windows Service or
against a locked or disconnected RDP session. Use a dedicated VM whose console
session stays unlocked.

Under the scripting driver the SAP window does **not** need focus: the session is
addressed by object reference and calls are synchronous, so a tester can use their
machine while a run executes without corrupting it.

## Credentials

Bot credentials live in the data workbook, in `First User` / `First Password` (and
`Second …`), as they do today. Two logon modes, detected at runtime rather than
assumed:

- **password present** — the logon screen is filled in.
- **password blank** — SSO; the connection resolves straight to Easy Access as the
  Windows user.

The driver probes for the logon screen and raises `LogonModeMismatch` if reality
disagrees with the profile, then records `session.Info.User` as the user the run
actually executed under, so reports always state whose roles were exercised.

Note that SSO ties a run to whoever is logged into that desktop, so a shared runner
VM needs explicit bot credentials to be useful. Populated workbooks are `.gitignore`d.

## Testing

```bash
pip install -e ".[dev]"
pytest                     # 104 tests, no SAP, no Windows, no network
```

Flows, remediation, policy, teardown and reports are all exercised against
`drivers/fake.py`, a scriptable stand-in that returns programmed status messages.
`drivers/demo.py` goes further and simulates plausible S/4HANA behaviour, so the
whole pipeline can be rehearsed and a real report produced on any machine.

## Status

Working end to end: core, error catalogue, the PR create/display flow, reporting,
CLI and web UI. Not yet built: the remaining six flows (ME21N, ME23N, MIGO,
MI01–MI07, the approval views), the keystroke fallback driver, and the Playwright
driver for the Fiori approval steps — which are declared and reported as `MANUAL`
rather than silently omitted. The PyInstaller build is written but has not been run
on Windows from this environment.
