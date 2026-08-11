# Contributing to jackery_home_cloud-ha

This document collects the conventions that came out of the review rounds
on PR #4 (MQTT live polling, MQTT-primary sensors, reverse-engineered
controls). Most of it is about two things this project is unusually
exposed to: **reverse-engineered protocol data** and **rename discipline**
across a codebase with no automated test suite and no CI import check. Read
it before touching `coordinator.py`, `const.py`, or any entity platform
file.

## 1. Documenting reverse-engineered meter IDs

Every `MQTT_EMS_*`/`MQTT_PCS_*`/`MQTT_BMS1_*` meter ID constant in
`const.py` maps a numeric ID to a device value with no official
documentation behind it. When you add or touch one:

- **State how it was identified.** Two sources are considered reliable:
  the `PROPERTY_MAP` extracted from the Jackery Home Android APK's
  `DIY-MQTT.js`-style files, and direct MQTT traffic tracing (multiple
  device states — charging, discharging, with/without AC load — compared
  against the REST API and the app's own displayed values). A comment like
  `# confirmed via PROPERTY_MAP: "21548033":"HB-EMS-MODEL_systemSoc"` is
  worth more than no comment.
- **Don't assert what you haven't confirmed.** If a mapping is a guess,
  say so explicitly in the comment (e.g. "candidate, unconfirmed") rather
  than writing it as fact. Once confirmed, remove the hedge — a stale
  "unverified" comment on a value that's since been double-checked is as
  misleading as an unverified one stated as fact.
- **Multi-device values** (e.g. a system with more than one battery pack,
  `bms1_...`/`bms2_...`) should default to tracking only the primary/master
  device unless there's a concrete reason to expose per-pack data. Leave a
  comment explaining the limitation and where per-pack data would need to
  be added if it becomes necessary — don't silently pick one device node
  with no explanation.

## 2. Parsing device values

- Numeric MQTT meter values go through `extract_ems_meter_value()`
  (float). **Fixed-width encoded strings do not** — anything like the
  schedule windows' `HHMMHHMM` format must go through
  `extract_ems_meter_raw_value()` (string, no numeric round-trip) and be
  explicitly padded (`.zfill(8)`) rather than assumed to arrive at full
  width. The device itself omits leading zeros on the wire (confirmed:
  `["23146497","6150715"]`, 7 digits) — a naive "just don't convert to
  float" fix would still be wrong without the explicit pad.
- `extract_ems_meter_value()`/`extract_ems_meter_raw_value()` must accept
  `data_report`, `data_get`, and `data_set` `cmd` values. A parser that
  only accepts `data_report` silently drops every solicited response
  (`data_get` replies, `data_set` write confirmations) for whichever meter
  it wasn't specifically hand-tuned for.

## 3. Polling and MQTT/REST freshness

- `MQTT_POLL_INTERVAL_MAX_SECONDS` must stay comfortably under
  `MQTT_LIVE_POWER_VALUE_MAX_AGE_SECONDS`. If the user-configurable poll
  interval can exceed the freshness window used to decide MQTT-vs-REST
  fallback, power/SOC sensors will flap between sources every cycle.
  Treat this as a hard invariant, not something to leave to chance if
  either constant changes later.
- Group meters by how often they actually change, and poll each group at
  its own cadence: fast (power/SOC/output-state) on the user-configurable
  interval, cumulative totals on a fixed slower cadence, and
  configuration/schedule values **not periodically at all** — only on MQTT
  reconnect and right after a write targeting that group (see
  `refresh_group` on `async_set_meter_value`).
- Keep the documented minimum interval (README, `strings.json`,
  `translations/*.json`) in sync with the code's actual minimum. These
  drifted apart once already (docs said 15s, code allowed 5s, across 8
  separate strings).

## 4. Write verification

Every control write goes through
`JackeryHomeCloudCoordinator.async_set_meter_value()`. The contract it
enforces, and that any new caller must respect:

- `timestamp_key` is a **required** argument, not derived by convention
  from `bundle_key`. A write is confirmed only if the bundle's tracked
  value matches the target **and** its companion timestamp is at or after
  the moment this specific write attempt was published. There is no
  value-only fallback — a missing or stale timestamp means "not
  confirmed," full stop. (An MQTT PUBACK only proves broker delivery, not
  that the device applied the value; a value match alone can be a stale
  cached value that already happened to equal the target before you wrote
  anything.)
- Build the `data_set` payload (including its timestamp) fresh **inside**
  the retry loop, not once before it. Reused payloads make retries
  indistinguishable at the protocol level and are harder to correlate in
  logs.
- Writes are serialized per `(system_id, meter_id)` via
  `_meter_write_locks`, not per-system. Only two writes to the *same*
  meter can actually interfere with each other's verification; a
  system-wide lock would serialize unrelated writes (e.g. configuring a
  full week of schedule slots) for no benefit.

## 5. Entity attribute conventions

- Set `_attr_native_min_value`/`_attr_native_max_value` from values
  actually observed in the Jackery app or by testing the device's real
  limits, not from a round-number guess. Note in a comment or PR
  description how the bound was established.
- If `_attr_native_step` implies whole numbers, `_raw_to_native()` must
  round/cast to `int` (otherwise the HA frontend shows a spurious decimal),
  and consider whether writes should be normalized to the step too (see
  `_normalize_native_value()` on `_JackeryMqttNumberEntity` — defaults to
  a no-op, override when a control has a non-1 step like the feed power
  limit's 10 W increments).
- Don't default new entities to `EntityCategory.DIAGNOSTIC` just because
  they're new or MQTT-only. Diagnostic is for entities that aren't a
  normal operational measurement/control a user would want on their main
  dashboard — decide per entity, not by historical inertia.

## 6. Renaming: the rule that matters most here

This project has no CI, no test suite, and (in most contributors'
environments) no installed `homeassistant` package to actually import and
run the integration against. That means **a broken rename can ship
silently**, and it has, twice, in this project's history:

1. Renaming a `const.py` constant without updating every file that does
   `from .const import THAT_NAME` → `ImportError` at integration load.
   Since `coordinator.py` is imported by every platform module, this
   broke setup for the *entire* integration, not just one entity.
2. Renaming an entity's `_bundle_key` (a plain string, not a Python
   identifier) without updating the coordinator code that populates that
   same string key → **no crash at all**. The entity just silently reads
   `None` forever (permanently `unknown` in HA) and every write fails
   verification, because producer and consumer quietly disagree on a
   string that nothing checks at import time.

Both happened from applying a reviewer's "suggestion" to one file (via
GitHub's inline "Apply suggestion" button, or an equivalent single-file
edit) without checking who else references the thing being renamed.

**When you rename anything — a constant, a bundle/dict key, a class, a
`unique_id` suffix — before you consider it done:**

- `grep -rn "<old_name>"` across the **whole repository**, not just the
  file you edited, and not just `.py` files — `services.yaml`,
  `strings.json`, `translations/*.json`, `README.md`, and `docs/` have all
  carried stale references to a renamed entity or option in this project.
- Confirm the **new** name doesn't already collide with something else in
  scope before running a blind substitution (`grep` for the new name
  first; if it's already used for something unrelated, the rename needs
  to be scoped more carefully than a plain find/replace).
- `python -m py_compile` is necessary but **not sufficient** — it only
  catches syntax errors, not `ImportError` (imports aren't resolved by the
  compiler) and never catches a plain-string key mismatch. Use a static
  AST check instead for import completeness:
  ```bash
  python3 -c "
  import ast
  defined = set()
  tree = ast.parse(open('custom_components/jackery_home_cloud/const.py').read())
  for node in tree.body:
      if isinstance(node, ast.Assign):
          defined.update(t.id for t in node.targets if isinstance(t, ast.Name))
      elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
          defined.add(node.target.id)
  for fname in ('coordinator.py','number.py','select.py','sensor.py','switch.py','button.py'):
      path = f'custom_components/jackery_home_cloud/{fname}'
      tree = ast.parse(open(path).read())
      missing = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and n.level == 1 and n.module == 'const' for a in n.names
                 if a.name not in defined]
      print(fname, 'MISSING' if missing else 'OK', missing or '')
  "
  ```
- For a bundle/dict-key rename specifically, grep for the key string on
  **both sides**: whoever writes it into the coordinator's data (usually
  `coordinator.py`) and whoever reads it (the entity platform file). They
  must match exactly; nothing enforces this at import time.
- `unique_id` suffix renames are only safe for entities that have **never
  shipped in a release** — check whether the entity predates the current
  unreleased branch before renaming its `unique_id`. Renaming a released
  entity's `unique_id` silently creates a new entity in users' HA
  instances and orphans dashboards/automations pointing at the old one.

## 7. Before pushing

1. Full-repo grep sweep for both the old and new name of anything you
   renamed (see §6).
2. `python -m py_compile` on every touched `.py` file.
3. The AST import-completeness check from §6 if you touched `const.py` or
   any file that imports from it.
4. Read the actual `git diff`, not just a summary — a `sed`-based rename
   touches the whole file; confirm every changed line is the substitution
   you intended and nothing else moved.

## 8. Investigating write-response correlation and result/reason fields

[Discussion #6](https://github.com/iLLixM/jackery_home_cloud-ha/discussions/6)'s
Phase 2 ("Write-path reliability") includes two items explicitly marked
"Dependent on test capability" / "Further protocol validation" in their
per-topic sections — §6 "Request and response correlation" and §7 "Process
explicit `data_set` result and reason fields" there. Unlike the rest of
Phase 2, these are **not implementation-ready**: nothing in this codebase
or its captures has yet confirmed the Jackery protocol actually carries
usable correlation or result/reason fields on `data_set` responses. Per §1
above ("don't assert what you haven't confirmed"), do not write correlation
or result/reason handling code speculatively. Investigate first, against
real hardware:

- With `CONF_MQTT_DEBUG_RAW` enabled, capture raw `data_set` request/
  response pairs for several distinct writes issued close together (so
  responses can be told apart from each other and from unrelated spontaneous
  traffic).
- For each response, check whether it consistently carries: a request
  token/id, the original request timestamp echoed back, `cmd`, the target
  `meter_id`, and any `result`/`reason`-shaped field — and whether these are
  stable across writes or occasionally missing/delayed.
- Document findings using the same comment discipline as §1: "confirmed via
  ..." for a field seen consistently across multiple captures, "candidate,
  unconfirmed" for one seen once or inconsistently, and say explicitly when
  a field is simply absent.
- **Decision gate:** only implement request/response correlation or
  result/reason parsing once the relevant fields are confirmed present and
  stable. A field that's present but unreliable (missing on some responses)
  should be documented as such rather than built into logic that silently
  assumes it's always there — a `data_set` write already has a
  timestamp/value-freshness-based confirmation path (§4) that does not
  depend on either of these fields, so there is no correctness pressure to
  implement on unconfirmed assumptions.

## 9. AI-assisted commits

Some commits and PRs in this repository are produced with AI coding
assistant help (currently Claude Code). Per-commit `Co-Authored-By`
trailers are not used to record this — this file is the record instead.
