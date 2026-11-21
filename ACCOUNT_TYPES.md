# Account Types — how they work now

Status: **implemented** on branch `arena/046fa654-testing-repo-ams`.
Test suite: `pytest tests/` → **67 passed, 5 skipped** (40 of them cover the
classification and account-type surface).

---

## 1. The shape of the data

```
AccountCategory  (the legacy "Category / Group" picker on the form)
      │  free-form group used by cash-flow / receive-pay flows
      ▼
Category → Subcategory → Account Type        (the classification tree)
   Assets  →   Bank     →   Operating Bank / Meezan Bank (Gujrat)
      │                        │
      │                        └── decides: channels + required linked party
      ▼
Account row  →  class_category, class_subcategory, class_account_type (code),
                channel, linked_entity_type / linked_client_id / ..., opening_balance
```

* The tree lives in `item_definition` (`group_key='account_classification'`,
  levels `category` / `subcategory` / `account_type`).
* A **leaf** (account type) is:

  ```python
  {'label': 'Meezan Bank (Gujrat)',      # display name, renameable
   'channels': ['bank'],                 # what the type allows
   'default_channel': 'bank',
   'entity': 'none'}                     # required linked party
  ```
* `code` is a slug derived once from the name (`meezan_bank_gujrat`) and never
  changes afterwards. Accounts store the **code** in `class_account_type`, so a
  rename changes what users see and nothing else.

## 2. Where you create / edit an account type

| Where | What you can do |
| --- | --- |
| **Account form → Financial Classification → “New Account Type”** (next to the Account Type dropdown) | Create a type under the subcategory you already picked, choosing channels + linked party. It is saved to the DB and selected in the dropdown immediately — you never leave the form. |
| **Account form → “Manage”** or sidebar **Accounts → Account Types** (`/accounts/account-types`) | Full master page: every category/subcategory with its types, channels, linked party and how many accounts use each. Create, edit, activate/deactivate, delete. |
| **System → Item Definitions → Account Classification** | Same tree; the “Account Type” level now also asks for channels / default channel / required linked party. |

All three go through **one service**: `app/services/account_types.py`.
The JSON endpoints live in `blueprints/accounts/account_type_master.py`:

```
GET  /accounts/account-types
POST /accounts/api/account-types/add
POST /accounts/api/account-types/<id>/edit
POST /accounts/api/account-types/<id>/toggle
POST /accounts/api/account-types/<id>/delete
```

## 3. Rules (enforced server-side, never in the browser only)

* Name required, ≤120 chars, unique (case-insensitive) **within its subcategory**.
  The same name under a different subcategory is allowed.
* At least one channel; every channel must be one of
  `cash / bank / digital_wallet / ledger_only / other`; `default_channel` must be
  in the chosen set (otherwise the first channel is used).
* `entity` must be one of `none / client / supplier / partner / worker / vehicle / party`.
  When it is not `none`, the Account form requires the linked party
  (`_resolve_linked_entity` in `blueprints/accounts/account_form.py`).
* **Built-in (`is_system`) types are locked** — no rename, no channel change, no
  deactivate, no delete. Add a custom type under the same subcategory instead.
* A type in use cannot be deleted (message names the account count); deactivate it.
* Only `admin` / `root` may mutate (same rule as the rest of the account master
  data, `_deny_account_master_mutation`). Everything is written to the audit log
  (`account.type.create|edit|toggle|delete`).
* Renaming never rewrites an account row: the account keeps its `code`, so its
  ledger, balances and reports are untouched.
* Custom leaves are seeded **inactive-safe**: deactivating hides a type from the
  Account form (`is_valid_triple` returns false) but existing accounts keep it.

## 4. What changed in the code

| File | Change |
| --- | --- |
| `app/services/account_types.py` **(new)** | The single writer for leaves: slugged codes, channel/entity config, validation, usage counts, cache clearing, serialisation. |
| `blueprints/accounts/account_type_master.py` **(new)** | Master page + 4 JSON endpoints; admin-only, audited. |
| `templates/accounts/account_types.html` **(new)** | Master page UI (grouped table, create row per subcategory, edit/toggle/delete). |
| `templates/accounts/_account_form_body.html` | “New Account Type” panel + “Manage” link in the Financial Classification section (admin only). |
| `static/account_form.js` | Dropdown now renders `label` and keeps `code` as the value; new `window.AMSAccountForm` hook so a type created inline appears with its real channels immediately. |
| `blueprints/accounts/classification.py` | `registry_json()` passes the leaf **label** through to the form. |
| `app/services/item_definitions.py` | `build_classification()` keeps `config['label']` (so a rename is visible everywhere). |
| `app/blueprints/system/item_definitions.py` | The Account Type level of Item Definitions now creates through the shared service (channels + entity) and renames update `config['label']`. |
| `app/services/sidebar_nav.py` | New **Accounts → Account Types** link. |
| `tests/test_account_types.py` **(new)** | 16 tests: create with bank channel, entity, registry visibility, saving a real account, duplicates, validation, rename-stability, locks, permissions. |

No schema migration: everything rides on the existing `item_definition.config`
JSON column and the existing `Account.class_*` columns.

## 5. Typical flows

**Add a bank the shop just opened**
1. Accounts → Add Account → Category `Assets`, Subcategory `Bank`.
2. Click **New Account Type** → name `Meezan Bank (Gujrat)`, tick **Bank**, Save.
3. The type is selected; type the bank details and opening balance → Create Account.

**Give a type a linked client (client khata)**
1. Accounts → Account Types → find the subcategory (e.g. `Cash → Client Receivables`).
2. Add a type with channel **Ledger Only** and Linked party **Client**.
3. Every account of that type now requires picking the client in the form.

**Retire a type**
Deactivate it (hidden from new accounts, history preserved). Delete only works
while no account uses it.

## 6. Known limits

* Built-in leaves cannot be edited — by design (reports and the reconciliation
  counter depend on some of them). Create a custom sibling instead.
* A type still cannot be pinned to exactly one account; several accounts may use
  the same type + linked party. That duplicate guard is the remaining item from
  `ACCOUNT_TYPES_PLAN.md` (Phase 4) if it is wanted.
* Legacy columns (`category`, `source_category`, `account_type`, `type`) are still
  written on every save; they remain the compatibility surface for
  payments / KPIs / transfers.
