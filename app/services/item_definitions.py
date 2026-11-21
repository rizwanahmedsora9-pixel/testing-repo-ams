"""Item Definitions — the generic business-vocabulary registry.

Why this module exists
----------------------
The AMS started life with every business list hard-coded (``SALE_CATEGORY_CHOICES``,
``CHARGE_TYPES``, the account classification hierarchy in
``blueprints/accounts/classification.py`` …).  Those lists only change when the
code changes — which is daylight-over-hours work for a growing business.  This
module turns each list into a *user-managed* definition row stored in the
``item_definition`` table: add / rename / deactivate definitions at any time,
without a deploy.

The contract
------------
* The code keeps the *seed* value of each definition (the values the app shipped
  with) so a fresh install renders the same dropdowns it always did.
* On fresh databases the boot seeder writes the seed rows into the table
  (idempotent).  Thereafter the table is the single source of truth.
* Code references a definition by its stable *code*, never by its display name.
  Display labels (``name``) are free to change; the code is not.
* ``is_system`` rows are locked: visible, not renamed / deleted / deactivated.
  Everything else is a normal row.

Groups stored in the table
--------------------------
``account_classification`` — full hierarchy (category → subcategory → account
                            type).  Seeded from ``CLASSIFICATION`` in
                            ``blueprints/accounts/classification.py``;
                            managed through the management UI page.
``sale_category``          — sale categories.  Seeded from
                            ``constants.SALE_CATEGORY_CHOICES``.
``charge_type``           — GRN charge types.  Seeded from
                            ``grn_costing.CHARGE_TYPES``.
``payment_method``        — payment methods.  Seeded from the literal options
                            used in the booking/direct-sale forms.
"""
from __future__ import annotations

import logging

from models import db, ItemDefinition, pk_model_now

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Group catalogue
# ---------------------------------------------------------------------------
GROUP_ACCOUNT_CLASSIFICATION = 'account_classification'
GROUP_SALE_CATEGORY = 'sale_category'
GROUP_CHARGE_TYPE = 'charge_type'
GROUP_PAYMENT_METHOD = 'payment_method'

GROUP_LABELS = {
    GROUP_ACCOUNT_CLASSIFICATION: 'Account Classification',
    GROUP_SALE_CATEGORY: 'Sale Categories',
    GROUP_CHARGE_TYPE: 'GRN Charge Types',
    GROUP_PAYMENT_METHOD: 'Payment Methods',
}

GROUP_HELP = {
    GROUP_ACCOUNT_CLASSIFICATION: (
        'The controlled Category → Subcategory → Account Type hierarchy used by '
        'the Account create/edit form.  Existing rows keep their classification; '
        'adding a subcategory or account type never changes existing data.'
    ),
    GROUP_SALE_CATEGORY: (
        'Categories used to group direct sales and booking-style transactions. '
        'The locked built-in categories are special-cased by the code; custom '
        'categories work like any other label.'
    ),
    GROUP_CHARGE_TYPE: (
        'Charge types used in GRN landed-cost allocation.  Code (report buckets, '
        'mirror columns, P&L roll-up) depends on these values, so they are '
        'locked — a new type is a code change, not a data change.'
    ),
    GROUP_PAYMENT_METHOD: (
        'Payment methods shown in payment forms.  The code maps each method to '
        'the expected account category (cash/bank), so the values are locked.'
    ),
}

# Groups whose rows are only shown here, never edited (locked).
LOCKED_GROUPS = {GROUP_CHARGE_TYPE, GROUP_PAYMENT_METHOD}

# Q1 business seeds (added to the classification dictionary): Staff Food & Fuel
# are ordinary operating expenses shipped as user-editable rows.
SEED_Q1_SUBCATEGORIES = ('Staff Food', 'Fuel')
SEED_Q1_LEAVES = ('Staff Food Expense', 'Fuel Expense')


def normalize_group(group):
    """Return the known group key, or '' for anything unknown."""
    if group in GROUP_LABELS:
        return group
    return ''


def group_label(group):
    group = normalize_group(group)
    return GROUP_LABELS.get(group, group or 'Unknown')


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------
def _classification_seed_rows():
    """Walk the CLASSIFICATION dict and yield seed rows.

    Rows are yielded as dicts:
        level, code, name, parent_code, config, is_system

    The dict is the fallback (see ``blueprints/accounts/classification.py``);
    the table is seeded from it on a fresh database.
    """
    from blueprints.accounts.classification import CLASSIFICATION

    rows = []
    for cat_code, cat in CLASSIFICATION.items():
        cat_label = cat.get('label', cat_code)
        rows.append({
            'level': 'category', 'code': cat_code, 'name': cat_label,
            'parent_code': None,
            'config': {'label': cat_label, 'description': cat.get('description', '')},
            'is_system': True,
        })
        for sub_code, sub in cat.get('subcategories', {}).items():
            sub_label = sub.get('label', sub_code)
            is_q1 = sub_code in SEED_Q1_SUBCATEGORIES
            rows.append({
                'level': 'subcategory', 'code': sub_code, 'name': sub_label,
                'parent_code': cat_code,
                'config': {'label': sub_label},
                'is_system': not is_q1,
            })
            for atype_code, node in (sub.get('account_types') or {}).items():
                rows.append({
                    'level': 'account_type', 'code': atype_code, 'name': atype_code,
                    'parent_code': sub_code,
                    'config': {
                        'label': atype_code,
                        'channels': list(node['channels']),
                        'default_channel': node['default_channel'],
                        'entity': node['entity'],
                    },
                    'is_system': not (is_q1 or atype_code in SEED_Q1_LEAVES),
                })
    return rows


def seed_sale_category_rows():
    """Sale categories shipped with the app (locked / system)."""
    return [
        {'level': 'item', 'code': 'Booking Delivery', 'name': 'Booking Delivery',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Mixed Transaction', 'name': 'Mixed Transaction',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Credit Customer', 'name': 'Credit Customer',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Open Khata', 'name': 'Open Khata',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Cash', 'name': 'Cash',
         'parent_code': None, 'config': {}, 'is_system': True},
    ]


def seed_charge_type_rows():
    """GRN charge types shipped with the app (locked / system)."""
    labels = {
        'VEHICLE_FARE': 'Vehicle Fare',
        'TRANSPORT': 'Transportation',
        'LOADING': 'Loading',
        'UNLOADING': 'Unloading',
        'OTHER': 'Other Expenses',
    }
    return [
        {'level': 'item', 'code': code, 'name': label,
         'parent_code': None, 'config': {'label': label}, 'is_system': True}
        for code, label in labels.items()
    ]


def seed_payment_method_rows():
    """Payment methods shipped with the app (locked / system)."""
    return [
        {'level': 'item', 'code': 'Cash', 'name': 'Cash',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Bank', 'name': 'Bank Transfer',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Check', 'name': 'Check',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Card', 'name': 'Card',
         'parent_code': None, 'config': {}, 'is_system': True},
        {'level': 'item', 'code': 'Online', 'name': 'Online',
         'parent_code': None, 'config': {}, 'is_system': True},
    ]


# ---------------------------------------------------------------------------
# Seeding (fresh DB only — never overwrite user data)
# ---------------------------------------------------------------------------
_DEPTH = {'category': 0, 'subcategory': 1, 'account_type': 2, 'item': 0}


def _insert_rows(group, seed_rows):
    """Insert seed rows whose (level, code) is not already present."""
    existing_rows = {
        (r.level, r.code): r
        for r in ItemDefinition.query.filter_by(group_key=group).all()
    }
    parent_rows = {}   # code -> inserted ItemDefinition (flushed)
    inserted = 0

    for r in sorted(seed_rows, key=lambda r: _DEPTH.get(r['level'], 0)):
        key = (r['level'], r['code'])
        if key in existing_rows:
            # Keep a reference for FK resolution.
            parent_rows[r['code']] = existing_rows[key]
            continue

        parent_id = None
        parent_code = r.get('parent_code')
        if parent_code:
            parent_row = parent_rows.get(parent_code)
            if parent_row is not None:
                parent_id = parent_row.id

        row = ItemDefinition(
            group_key=group,
            code=r['code'],
            name=r['name'],
            parent_id=parent_id,
            level=r['level'],
            config=r['config'],
            is_active=True,
            is_system=bool(r.get('is_system')),
            sort_order=10 + inserted,
            created_at=pk_model_now(),
            updated_at=pk_model_now(),
        )
        db.session.add(row)
        # Flush every root / sub / leaf so its id is usable as a parent FK.
        db.session.flush()
        parent_rows[r['code']] = row
        inserted += 1
    return inserted


def ensure_item_definitions():
    """Insert the seed definitions on a fresh database; no-op on later boots.

    System rows are ensured on every boot (insert-if-missing).  Non-system
    business rows are only inserted on a brand-new table, so a user's deletion
    is never silently resurrected.
    """
    try:
        # ---- account_classification ----
        rows = _classification_seed_rows()
        if not ItemDefinition.query.filter_by(
            group_key=GROUP_ACCOUNT_CLASSIFICATION
        ).count():
            _insert_rows(GROUP_ACCOUNT_CLASSIFICATION, rows)
        else:
            # Guarantee locked system rows exist; leave user rows alone.
            system_rows = [r for r in rows if r.get('is_system')]
            if system_rows:
                _insert_rows(GROUP_ACCOUNT_CLASSIFICATION, system_rows)

        # ---- flat groups ----
        for group, seeder in (
            (GROUP_SALE_CATEGORY, seed_sale_category_rows),
            (GROUP_CHARGE_TYPE, seed_charge_type_rows),
            (GROUP_PAYMENT_METHOD, seed_payment_method_rows),
        ):
            rows = seeder()
            if not ItemDefinition.query.filter_by(group_key=group).count():
                _insert_rows(group, rows)
            else:
                system_rows = [r for r in rows if r.get('is_system')]
                if system_rows:
                    _insert_rows(group, system_rows)
        db.session.commit()
        log.info('Item definition seeding complete')
        return True
    except Exception:
        db.session.rollback()
        raise


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def get_group_rows(group, *, active_only=True):
    """All rows for a group, ordered by sort_order, id."""
    group = normalize_group(group)
    if not group:
        return []
    q = ItemDefinition.query.filter_by(group_key=group)
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(ItemDefinition.sort_order.asc(), ItemDefinition.id.asc()).all()


def get_group_row(group, code):
    group = normalize_group(group)
    if not group:
        return None
    return ItemDefinition.query.filter_by(group_key=group, code=code).first()


def get_active_codes(group):
    return [r.code for r in get_group_rows(group, active_only=True)]


def get_active_names(group):
    return [r.name for r in get_group_rows(group, active_only=True)]


def get_sale_categories():
    """Active sale categories as (code, label) pairs, in display order."""
    rows = get_group_rows(GROUP_SALE_CATEGORY, active_only=True)
    if rows:
        return [(r.code, r.name) for r in rows]
    return [
        ('Booking Delivery', 'Booking Delivery'),
        ('Mixed Transaction', 'Mixed Transaction'),
        ('Credit Customer', 'Credit Customer'),
        ('Open Khata', 'Open Khata'),
        ('Cash', 'Cash'),
    ]


def get_charge_type_labels():
    """{code: display label} for active GRN charge types."""
    rows = get_group_rows(GROUP_CHARGE_TYPE, active_only=True)
    labels = {r.code: (r.config or {}).get('label') or r.name for r in rows}
    if labels:
        return labels
    return {
        'VEHICLE_FARE': 'Vehicle Fare', 'TRANSPORT': 'Transportation',
        'LOADING': 'Loading', 'UNLOADING': 'Unloading', 'OTHER': 'Other Expenses',
    }


def get_payment_methods():
    """Active payment methods as (code, name) pairs, in display order."""
    rows = get_group_rows(GROUP_PAYMENT_METHOD, active_only=True)
    if rows:
        return [(r.code, r.name) for r in rows]
    return [
        ('Cash', 'Cash'), ('Bank', 'Bank Transfer'), ('Check', 'Check'),
        ('Card', 'Card'), ('Online', 'Online'),
    ]


# ---------------------------------------------------------------------------
# Classification rebuild — turn the flat rows back into the nested dict
# ---------------------------------------------------------------------------
def build_classification():
    """Build the CLASSIFICATION-shaped nested dict from the table.

    Returns ``None`` when the table has no usable classification rows (fresh
    boot before the seeder ran, or table unavailable).  Callers fall back to
    the seed dict in that case.
    """
    rows = get_group_rows(GROUP_ACCOUNT_CLASSIFICATION, active_only=True)
    if not rows:
        return None

    by_id = {r.id: r for r in rows}

    def _chain_active(r):
        node, seen = r, set()
        while node is not None:
            if node.id in seen:
                return False
            seen.add(node.id)
            if not node.is_active:
                return False
            node = by_id.get(node.parent_id)
        return True

    rows = [r for r in rows if _chain_active(r)]

    cats = {}
    for r in rows:
        if r.parent_id is None:
            cats[r.code] = {
                'label': r.name,
                'description': (r.config or {}).get('description', ''),
                'subcategories': {},
            }
            continue

        parent = by_id.get(r.parent_id)
        if parent is None:
            continue

        if parent.parent_id is None:
            # subcategory — parent is a category root
            if parent.code not in cats:
                continue
            cats[parent.code]['subcategories'][r.code] = {
                'label': r.name,
                'account_types': {},
            }
            continue

        # account_type — parent is a subcategory
        grandparent = by_id.get(parent.parent_id)
        if grandparent is None or grandparent.code not in cats:
            continue
        sub = cats[grandparent.code]['subcategories'].get(parent.code)
        if sub is None:
            continue
        m = r.config or {}
        sub['account_types'][r.code] = {
            'channels': list(m.get('channels') or ['ledger_only']),
            'default_channel': m.get('default_channel') or (m.get('channels') or ['ledger_only'])[0],
            'entity': m.get('entity') or 'none',
        }

    if not cats:
        return None
    return cats
