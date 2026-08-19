# Adding a region

A new region or landscape is a YAML file. If it needs a code change, that is a bug
in the abstraction — raise it rather than working around it.

## 1. Copy the profile

```bash
cp profiles/regions/au.yaml profiles/regions/uk.yaml
```

Then work through it:

| Key | What it controls |
|---|---|
| `connection.name` | The SAP Logon entry name, matched exactly as it appears in SAP Logon |
| `connection.client` / `language` | Filled into the logon screen |
| `connection.logon_mode` | `auto` (decide per row), `explicit`, or `sso` |
| `formats.date` | The SAP **user profile's** date format, not the country's convention |
| `formats.decimal` | Also a user-profile setting, and a common source of silent wrong values |
| `defaults.*` | Values remediation draws on: tax code, GL account, net price, delivery date |
| `exclusions.plants` | Rows to record `SKIPPED` with a reason instead of running |
| `templates.*` | Test-plan templates by logical name; flows refer to these names |
| `testplan.*` | Where the header fields and step table live in that template |

`formats.date` and `formats.decimal` follow the SAP user's own profile
(`SU3`), so two users in the same region can need different values. If dates come
back rejected, check the bot account's profile before the region file.

## 2. Capture the field bindings

Element ids differ per landscape. On the Windows machine, with SAP GUI open on the
screen you want to bind:

```bash
saptest inspect --tcode ME51N --filter MATNR --yaml
```

Paste the result into `profiles/bindings/<flow>_<region>.yaml` and rename the
suggested keys to the logical names the flow uses (`item.matnr` → `item.material`).

For table controls, replace the row index in the id with `{row}`:

```yaml
item.material:
  id: "wnd[0]/usr/tbl.../ctxtMEREQ3211-MATNR[3,{row}]"
```

## 3. Map the spreadsheet columns

In the same binding file:

```yaml
columns:
  Material: item.material
  Plant: item.plant
```

Column lookup collapses whitespace and ignores case, so `Requisition  Group` with
two spaces still resolves.

## 4. Check before running

```bash
saptest doctor --region uk --data <workbook> --flow p2p.pr_create
```

Fix the failures; warnings about unbound fields tell you what `inspect` still needs
to cover.

## 5. Localise the error catalogue

The base catalogue matches on English text where the message keys are not yet known.
For a non-English landscape those rules will not match, which is expected — run once,
then:

```bash
saptest catalog review runs/run_<id>
```

That prints draft rules keyed on message class and number, which work in **every**
language. Put region-specific rules in `catalog/<region>.yaml` and list it after the
base file in the profile; later files override earlier ones by name.
