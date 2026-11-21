# Account Types — how they are made today, and the plan

> **Status: Phases 1–3 are now implemented — see `ACCOUNT_TYPES.md`.**
> This file is kept as the original analysis (the gaps it lists are what was fixed).
> Phase 4 (duplicate type+party guard) is still open.

Status: **plan only, nothing changed yet.** Branch `arena/046fa654-testing-repo-ams`
(base `7d15ad2`). Baseline verified: `tests/test_item_definitions.py` → 24 passed
(venv at `.venv`, `python -m pytest tests/test_item_definitions.py -q`).

---

## 1. How an "account type" is made today (as-built)

There are **two layers**, and the DB layer wins.

### Layer A — the seed registry (code)

`blueprints/accounts/classification.py`

* `CLASSIFICATION_SEED` (line 88) is a nested dict: **Category → Subcategory → Account Type → leaf**.
  6 categories (`Assets`, `Liabilities`, `Equity / Own Funds`, `Income`, `Expenses`,
  `External / Clearing`), 50 leaves today.
* Each leaf is built by `_node(channel, entity)` (line 71) and holds exactly three things:
  * `channels` — allowed mediums (`cash`, `bank`, `digital_wallet`, `ledger_only`, `other`)
  * `default_channel`
  * `entity` — required linked party (`none`, `client`, `supplier`, `partner`, `worker`, `vehicle`, `party`)
* `_load_classification()` (line 322) reads the **table first**, falls back to the seed dict,
  and caches for 2 s per process.
* Everything else in this file is derived from that dict: `is_valid_triple` (399),
  `allowed_channels`, `required_entity`, `registry_json` (578),
  plus the legacy-column translators `legacy_group_for` (485) and `legacy_account_type_for` (511).

### Layer B — the `item_definition` table (data)

`app/services/item_definitions.py` + `models/item_definition.py`

* Table `item_definition`: `group_key`, `code`, `name`, `parent_id`, `level`
  (`category|subcategory|account_type|item`), `config` (JSON), `is_active`, `is_system`,
  unique on `(group_key, level, code)`.
* `ensure_item_definitions()` (line 248) seeds it at boot: system rows insert-if-missing,
  user rows only on a brand-new table.
* `build_classification()` (line 358) turns the flat rows back into the nested dict
  shape; leaf `config` supplies `channels` / `default_channel` / `entity`
  (lines 414–419), **defaulting to `['ledger_only']` / `none`** when absent.

### The UI that creates them

`GET /item-definitions` → `templates/item_definitions.html` (System sidebar, line 469 of
`app/services/sidebar_nav.py`), routes in `app/blueprints/system/item_definitions.py`:

| Route | What it does |
| --- | --- |
| `POST /item-definitions/add` (line 158) | `kind` = `subcategory` or `account_type` + `parent_code` + `name`. Parent level is checked strictly (subcategory must sit under a *category*, account type under a *subcategory*). `code = name` (raw display name **is** the machine key), `config = {'label': name}` only. |
| `POST /item-definitions/<id>/rename` (265) | Changes `name` only; `code` stays. Refused for `is_system` rows. |
| `POST /item-definitions/<id>/toggle` (311) | Activate / deactivate. Refused for system rows. |
| `POST /item-definitions/<id>/delete` (341) | Refused if any account uses it (`_usage_count`, line 55) or if a category/subcategory still has active children. |

### How a type lands on an actual account

1. `GET/POST /accounts/add` → `blueprints/accounts/accounts_crud.py:61`, form partial
   `templates/accounts/_account_form_body.html`, cascade engine `static/account_form.js`.
2. The three selects (`class_category`, `class_subcategory`, `class_account_type`) are filled
   from `registry_json()` at render time; channel + linked-entity inputs are driven from the
   selected leaf, so an invalid combination is not offerable in the browser.
3. Submit → `validate_account_form()` (`blueprints/accounts/account_form.py:150`) re-checks
   everything **server-side**: valid triple, channel allowed, bank/wallet detail fields when
   the channel needs them, linked entity resolved (`_resolve_linked_entity`).
4. It returns the new `class_*` columns **plus** the legacy columns kept in sync
   (`category` cash/bank, `source_category`, `account_type`, `type`) — lines 226–245.
5. Stored on the single `Account` row (`models/cash.py:28–79`): `class_category`,
   `class_subcategory`, `class_account_type`, `channel`, `opening_balance*`,
   `account_status`, `linked_*`. Edit path is the same code: `accounts_crud.py:328`.
6. Old rows are backfilled once at schema upgrade via `legacy_to_classification()`
   (`app/services/schema.py:1239`).

So: **an account type is made once (registry/table) and then *chosen* by each account.**
The account row is what "carries" the type — there is no separate join table.

---

## 2. Gaps and bugs found while reading the code

These are the things the plan has to fix, not cosmetic polish.

| # | Issue | Evidence |
| --- | --- | --- |
| G1 | **A custom account type can only ever be `ledger_only` with no linked party.** The add route writes `config = {'label': name}`, and `build_classification` then falls back to `channels=['ledger_only']`, `entity='none'`. So an admin cannot create e.g. "Meezan Bank – Gujrat" as a *bank* type; the Channel dropdown collapses to "Ledger Only". | `item_definitions.py:220` + `app/services/item_definitions.py:414–419` |
| G2 | **There is no edit surface for channel / entity on a leaf at all** — not even for admin. Rename touches `name` only; nothing writes `config.channels` / `config.entity`. Seeded leaves are `is_system` = locked, custom leaves are created without the fields. The classification is therefore effectively frozen at seed time. | `app/blueprints/system/item_definitions.py` (routes list above) |
| G3 | **Renaming an account type has no visible effect in the Account form.** `build_classification` keys leaves by `code` and drops `config['label']`; `registry_json` emits `"name": at_name` (the code); `account_form.js:93` renders `t.name` as both value and label. Rename therefore changes the Item Definitions screen only. | `classification.py:600–605`, `item_definitions.py:414–419`, `static/account_form.js:86–95` |
| G4 | **`code = name`, with uniqueness only per `(level, code)`.** A custom account type whose name equals an existing *subcategory* code is legal, and `get_group_row(group, code)` looks up by code without a level → parent resolution and `_insert_rows`' `parent_rows[code]` map become ambiguous as soon as codes collide across levels. | `item_definitions.py:206–208`, `app/services/item_definitions.py:317–330`, `get_group_row` |
| G5 | **Nothing prevents two accounts sharing the same leaf + same linked party** (e.g. two "Client Ledger" accounts for one client). No uniqueness, no warning; the only feedback is `usage_count` on the definitions page. | `models/cash.py:28–79`, `_usage_count` |
| G6 | Leaf names are `NOT NULL` display strings used as keys, so a rename is a "code stays, label changes" operation that the *dropdown* ignores (G3) and the *legacy* columns never see (`legacy_account_type_for` derives from category/subcategory only). Cheap to fix, but it must be fixed on both sides at once. | `classification.py:511–536` |
| G7 | Minor: `validate_account_form` rejects `class_subcategory == 'loans payable'` case-insensitively, so a user-created subcategory with that name gets a message about the Loan Register with no explanation of why. Also `legacy_group_for` maps the whole **Expenses** category to legacy group `External`, which surprises anyone reading `source_category` on an expense account. | `account_form.py:174`, `classification.py:503–504` |

---

## 3. Plan

### Phase 0 — decide scope (needs your answer, see §5)
"Added to one account" can mean three different products; they lead to different work.

### Phase 1 — make an account type fully definable (fixes G1, G2)
1. `app/blueprints/system/item_definitions.py`
   * `add` route: accept `channels` (multi-checkbox), `default_channel`, `entity` for
     `kind='account_type'`; validate against `CHANNELS` / `ENTITY_TYPES` from
     `classification.py`; persist into `config` together with `label`.
   * New route `POST /item-definitions/<id>/config` to edit `name`, `channels`,
     `default_channel`, `entity` on a **custom** row (system rows stay locked, but see 3).
   * Reject `entity` values that need a party the account form cannot resolve (already guarded
     by `_resolve_linked_entity`).
2. `app/services/item_definitions.py`: no schema change needed — `config` is JSON; make
   `build_classification` explicit about defaults (keep `ledger_only/none` as fallback so old
   rows still load) and write the missing keys on edit.
3. `templates/item_definitions.html`: extend the add form for `account_type` with channel
   checkboxes + entity select, and show the resulting "Cash / Bank / Wallet / Ledger only •
   linked to X" line per leaf; add an Edit modal reusing the rename modal.

### Phase 2 — labels that actually work (fixes G3, G6)
1. `build_classification()`: carry `config['label']` through into the leaf dict
   (`{'label': …, 'channels': …, 'default_channel': …, 'entity': …}`).
2. `registry_json()`: emit `"label": leaf.get('label') or at_name` alongside `"name"` (code).
3. `static/account_form.js:86–95`: value = `t.name` (code), text = `t.label || t.name`;
   same for categories/subcategories (already `label || name` there).
4. Keep `is_valid_triple` / `validate_account_form` keyed on **code**, so a rename can never
   invalidate an existing account row.
5. Regression test: rename a custom leaf → account form HTML/registry shows the new label,
   an existing account keeps saving.

### Phase 3 — safe identity for new rows (fixes G4)
1. Slugify `code` from the name (`lower`, spaces → `_`, strip unsafe chars) instead of `code = name`,
   and check uniqueness on the **full triple** (category, subcategory, code), not just
   `(level, code)`.
2. Keep the display `name` free-form (that is what users see).
3. `_insert_rows` / parent map: key by `(level, code)` so seed data cannot collide.
4. Backwards compatibility: existing rows keep their current code untouched (no rewrite).

### Phase 4 — "one type ↔ accounts" integrity (fixes G5)
Depends on your answer to §5:
* either a **soft guard**: on create/edit, warn + require confirmation when another active
  account already uses the same leaf + the same `linked_client_id` / `linked_supplier_id` /
  `linked_party_name`;
* or a **hard rule** for specific leaves (e.g. a client-specific receivable may exist once),
  enforced in `validate_account_form` with an explicit message and a `force` flag.
Either way the check belongs in `account_form.py` (server-side), never only in the browser.

### Phase 5 — tests and docs
* Extend `tests/test_item_definitions.py`:
  * add an `account_type` with `channels=['bank']` + `entity='supplier'` → appears in
    `build_classification()` and in `registry_json()` with those values;
  * edit config on a custom leaf; system leaf still refuses;
  * rename → label changes, code stable, account form shows label, account still saves;
  * duplicate name under the same subcategory refused, same name under a different
    subcategory allowed (per decision in Phase 3);
  * duplicate accounts (Phase 4) blocked/warned per decision.
* New `tests/test_account_form_classification.py` for `validate_account_form` refusal paths
  (invalid triple, wrong channel, missing bank fields, missing linked party).
* Update `ACCOUNT_TYPES_PLAN.md` → `ACCOUNT_TYPES.md` (as-built) and add a CHANGELOG entry.

### Out of scope (deliberate)
* No schema migration: `config` JSON already holds what Phase 1–3 need.
* No change to how ledger balances, reconciliation counters or the `recon_counter`
  system accounts work; `is_reserved_account` / `is_legacy_loan_account` filters stay.
* No renaming of the legacy `category` / `account_type` / `type` columns — they remain the
  compatibility surface for payments, KPIs and transfers.

---

## 4. Files the plan touches

| File | Change |
| --- | --- |
| `blueprints/accounts/classification.py` | label passthrough in `build_classification` consumer / `registry_json`, slug helper |
| `app/services/item_definitions.py` | `build_classification` keeps `label`; safer parent keying |
| `app/blueprints/system/item_definitions.py` | add-with-channels/entity, edit-config route, full-triple uniqueness |
| `blueprints/accounts/account_form.py` | duplicate-account guard (Phase 4), clearer loan message (G7) |
| `static/account_form.js` | render `label || name` for leaves |
| `templates/item_definitions.html` | channel/entity inputs, leaf summary, edit modal |
| `tests/test_item_definitions.py`, `tests/test_account_form_classification.py` | new coverage |

## 5. Open questions (blocking Phase 1/4 scope)

1. **What does "added to one account" mean?**
   (a) a type may be used by many accounts (today's behaviour),
   (b) a type must map to exactly one account (enforced),
   (c) you want to attach an existing type to a specific account from the account screen.
2. **Should channel + linked party be editable on leaves** (including seeded ones, by copying a
   seed leaf into a custom row rather than unlocking the system row)?
3. **Implement after your answer, or ship Phase 1+2 first** (they are self-contained and fix the
   two bugs that make custom account types nearly useless today)?
