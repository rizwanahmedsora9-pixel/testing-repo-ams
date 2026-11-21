"""Account-type master — create and edit the *leaves* of the classification tree.

Why this module exists
----------------------
The Account form has always let an admin add a **Category / Group**
(``AccountCategory`` rows, via the "Quick Add Group" panel).  There was no such
path for an **Account Type** — the leaves of the
``Category → Subcategory → Account Type`` hierarchy.  Leaves could only come
from ``CLASSIFICATION_SEED`` in ``blueprints/accounts/classification.py``, so a
business could never add "Meezan Bank (Gujrat)" as a *bank* type, and a custom
leaf fell back to ``channels=['ledger_only']`` / ``entity='none'`` (see
``app/services/item_definitions.build_classification``).

This module is the one place that writes leaves.  It is called by

* the Account create/edit form's "Quick Add Account Type" panel
  (``blueprints/accounts/account_type_master.py``), and
* the Item Definitions page (``app/blueprints/system/item_definitions.py``),
* and the dedicated Account Types master page.

Storage
-------
A leaf is an ``ItemDefinition`` row with
``group_key='account_classification'``, ``level='account_type'``, ``parent_id`` =
the subcategory row, and ``config`` = ::

    {'label': 'Meezan Bank (Gujrat)',
     'channels': ['bank'], 'default_channel': 'bank', 'entity': 'none'}

``code`` is a stable slug derived from the label once, on creation; renaming
changes ``label`` only, so existing accounts (which store the *code* in
``class_account_type``) keep resolving.  The cache in
``blueprints/accounts/classification`` is cleared on every write.

Rules enforced here (server-side, never trusted from the browser)
-----------------------------------------------------------------
* parent must be an active *subcategory*;
* name required, <=120 chars, unique (case-insensitive) within that subcategory;
* at least one channel, all channels known, ``default_channel`` in the set;
* ``entity`` in the known vocabulary;
* ``is_system`` leaves are locked: rename/config may not be written;
* a leaf that is in use cannot be deleted (usage counted per code).
"""
from __future__ import annotations

import re

from sqlalchemy import func

from models import Account, ItemDefinition, db, pk_model_now

GROUP_ACCOUNT_CLASSIFICATION = 'account_classification'
LEVEL_ACCOUNT_TYPE = 'account_type'
LEVEL_SUBCATEGORY = 'subcategory'

MAX_NAME = 120
_SLUG_STRIP = re.compile(r'[^a-z0-9]+')
_LEGACY_CODE_CHARS = re.compile(r'^[A-Za-z0-9 &/\-]+$')


class AccountTypeError(ValueError):
    """User-facing validation failure (message is safe to flash as-is)."""


# ---------------------------------------------------------------------------
# Vocabulary (delegated so there is exactly one definition)
# ---------------------------------------------------------------------------
def vocabulary():
    from blueprints.accounts.classification import CHANNELS, ENTITY_TYPES
    return list(CHANNELS), list(ENTITY_TYPES)


def _normalise_channels(raw):
    """Accept a list/tuple/set or a comma-separated string → ordered unique list."""
    channels, _ = vocabulary()
    if raw is None:
        items = []
    elif isinstance(raw, str):
        items = [p for p in raw.replace(';', ',').split(',')]
    else:
        items = list(raw)
    out = []
    for item in items:
        value = (item or '').strip().lower()
        if not value:
            continue
        if value not in channels:
            raise AccountTypeError(f'Unknown channel "{value}".')
        if value not in out:
            out.append(value)
    return out


def _normalise_entity(raw):
    _, entities = vocabulary()
    value = (raw or 'none').strip().lower() or 'none'
    if value not in entities:
        raise AccountTypeError(f'Unknown linked entity "{value}".')
    return value


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def get_row(def_id):
    row = db.session.get(ItemDefinition, int(def_id)) if def_id else None
    if row is None or row.group_key != GROUP_ACCOUNT_CLASSIFICATION:
        return None
    if row.level != LEVEL_ACCOUNT_TYPE:
        return None
    return row


def parent_subcategory(row_or_id):
    """The subcategory row that owns a leaf (or None)."""
    if isinstance(row_or_id, ItemDefinition):
        parent_id = row_or_id.parent_id
    else:
        parent_id = row_or_id
    if not parent_id:
        return None
    sub = db.session.get(ItemDefinition, int(parent_id))
    if sub is None or sub.group_key != GROUP_ACCOUNT_CLASSIFICATION:
        return None
    if sub.level != LEVEL_SUBCATEGORY:
        return None
    return sub


def category_of(row) -> str:
    sub = parent_subcategory(row)
    if sub is None or not sub.parent_id:
        return ''
    cat = db.session.get(ItemDefinition, sub.parent_id)
    return (cat.code if cat else '') or ''


def leaves_for_subcategory(sub_code, *, include_inactive=False):
    rows = ItemDefinition.query.filter_by(
        group_key=GROUP_ACCOUNT_CLASSIFICATION, level=LEVEL_ACCOUNT_TYPE,
    ).order_by(ItemDefinition.sort_order.asc(), ItemDefinition.id.asc()).all()
    out = []
    for row in rows:
        sub = parent_subcategory(row)
        if sub is None or sub.code != sub_code:
            continue
        if not include_inactive and not row.is_active:
            continue
        out.append(row)
    return out


def subcategories(active_only=True):
    q = ItemDefinition.query.filter_by(
        group_key=GROUP_ACCOUNT_CLASSIFICATION, level=LEVEL_SUBCATEGORY,
    )
    if active_only:
        q = q.filter_by(is_active=True)
    rows = q.order_by(ItemDefinition.sort_order.asc(), ItemDefinition.id.asc()).all()
    return [{'row': r, 'category': category_of(r)} for r in rows
            if active_only is False or (category_of(r) and _parent_active(r))]


def _parent_active(sub_row):
    if not sub_row.parent_id:
        return False
    cat = db.session.get(ItemDefinition, sub_row.parent_id)
    return bool(cat and cat.is_active)


def usage_count(code):
    """Number of account rows classified as this leaf."""
    if not code:
        return 0
    return Account.query.filter_by(class_account_type=code).count()


def serialise(row):
    sub = parent_subcategory(row)
    cfg = row.config or {}
    channels = list(cfg.get('channels') or ['ledger_only'])
    return {
        'id': row.id,
        'code': row.code,
        'name': row.name,
        'label': cfg.get('label') or row.name,
        'category': category_of(row),
        'subcategory': (sub.code if sub else ''),
        'channels': channels,
        'default_channel': cfg.get('default_channel') or channels[0],
        'entity': cfg.get('entity') or 'none',
        'is_active': bool(row.is_active),
        'is_system': bool(row.is_system),
        'usage_count': usage_count(row.code),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate(name, subcategory_code, channels, default_channel=None,
             entity='none', *, allow_system_parent=True):
    """Validate a leaf payload and return a cleaned dict (no DB writes)."""
    name = (name or '').strip()
    if not name:
        raise AccountTypeError('Account type name is required.')
    if len(name) > MAX_NAME:
        raise AccountTypeError(f'Account type name must be {MAX_NAME} characters or fewer.')

    sub = ItemDefinition.query.filter_by(
        group_key=GROUP_ACCOUNT_CLASSIFICATION, level=LEVEL_SUBCATEGORY,
        code=(subcategory_code or '').strip(),
    ).first()
    if sub is None:
        raise AccountTypeError('Choose a valid subcategory for this account type.')
    if not sub.is_active:
        raise AccountTypeError('That subcategory is inactive; activate it or pick another one.')
    if allow_system_parent and not _parent_active(sub):
        raise AccountTypeError('The parent category of that subcategory is inactive.')

    chan = _normalise_channels(channels)
    if not chan:
        raise AccountTypeError('Choose at least one channel (cash, bank, wallet or ledger only).')
    default = (default_channel or '').strip().lower() or chan[0]
    if default not in chan:
        default = chan[0]

    return {
        'name': name,
        'subcategory': sub,
        'subcategory_code': sub.code,
        'channels': chan,
        'default_channel': default,
        'entity': _normalise_entity(entity),
    }


def _slug(name):
    """Stable machine code derived from a display name."""
    base = _SLUG_STRIP.sub('_', (name or '').strip().lower()).strip('_')
    if not base:
        base = 'account_type'
    return base[:120]


def _unique_code(name):
    """Slug of ``name``, suffixed only when the base slug already exists."""
    base = _slug(name)
    code = base
    n = 2
    while ItemDefinition.query.filter_by(
        group_key=GROUP_ACCOUNT_CLASSIFICATION, level=LEVEL_ACCOUNT_TYPE, code=code,
    ).first() is not None:
        code = f'{base}_{n}'
        n += 1
    return code


def _name_taken(name, subcategory_code, *, exclude_id=None):
    like = (name or '').strip().lower()
    for row in leaves_for_subcategory(subcategory_code, include_inactive=True):
        if exclude_id and row.id == exclude_id:
            continue
        label = ((row.config or {}).get('label') or row.name or '').strip().lower()
        if label == like or (row.name or '').strip().lower() == like:
            return row
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def create(name, subcategory_code, channels, default_channel=None, entity='none'):
    """Create a new custom account type under a subcategory.

    Returns the created ``ItemDefinition`` row.  Raises :class:`AccountTypeError`.
    """
    cleaned = validate(name, subcategory_code, channels, default_channel, entity)
    if _name_taken(cleaned['name'], cleaned['subcategory_code']) is not None:
        raise AccountTypeError(
            f'An account type "{cleaned["name"]}" already exists under this subcategory.'
        )

    row = ItemDefinition(
        group_key=GROUP_ACCOUNT_CLASSIFICATION,
        code=_unique_code(cleaned['name']),
        name=cleaned['name'],
        parent_id=cleaned['subcategory'].id,
        level=LEVEL_ACCOUNT_TYPE,
        config={
            'label': cleaned['name'],
            'channels': cleaned['channels'],
            'default_channel': cleaned['default_channel'],
            'entity': cleaned['entity'],
        },
        is_active=True,
        is_system=False,
        sort_order=10 + ItemDefinition.query.filter_by(
            group_key=GROUP_ACCOUNT_CLASSIFICATION).count(),
        created_at=pk_model_now(),
        updated_at=pk_model_now(),
    )
    db.session.add(row)
    db.session.commit()
    clear_cache()
    return row


def update(def_id, *, name=None, channels=None, default_channel=None, entity=None):
    """Edit a custom account type (label + channel/entity behaviour).

    ``code`` is never rewritten — existing accounts keep resolving.
    """
    row = get_row(def_id)
    if row is None:
        raise AccountTypeError('Account type not found.')
    if row.is_system:
        raise AccountTypeError(
            'Built-in account types are locked. Add a custom type instead of editing this one.'
        )

    sub = parent_subcategory(row)
    if sub is None:
        raise AccountTypeError('This account type has no valid parent subcategory.')

    cfg = dict(row.config or {})
    new_name = row.name if name is None else name
    cleaned = validate(
        new_name, sub.code,
        cfg.get('channels') if channels is None else channels,
        cfg.get('default_channel') if default_channel is None else default_channel,
        cfg.get('entity') if entity is None else entity,
    )

    clash = _name_taken(cleaned['name'], sub.code, exclude_id=row.id)
    if clash is not None:
        raise AccountTypeError(
            f'An account type "{cleaned["name"]}" already exists under this subcategory.'
        )

    row.name = cleaned['name']
    cfg['label'] = cleaned['name']
    cfg['channels'] = cleaned['channels']
    cfg['default_channel'] = cleaned['default_channel']
    cfg['entity'] = cleaned['entity']
    row.config = cfg
    row.updated_at = pk_model_now()
    db.session.commit()
    clear_cache()
    return row


def set_active(def_id, active):
    row = get_row(def_id)
    if row is None:
        raise AccountTypeError('Account type not found.')
    if row.is_system:
        raise AccountTypeError('Built-in account types are locked and cannot be deactivated.')
    row.is_active = bool(active)
    row.updated_at = pk_model_now()
    db.session.commit()
    clear_cache()
    return row


def delete(def_id):
    row = get_row(def_id)
    if row is None:
        raise AccountTypeError('Account type not found.')
    if row.is_system:
        raise AccountTypeError('Built-in account types are locked and cannot be deleted.')
    used = usage_count(row.code)
    if used:
        raise AccountTypeError(
            f'"{row.name}" is used by {used} account(s) and cannot be deleted. '
            'Deactivate it instead.'
        )
    name = row.name
    db.session.delete(row)
    db.session.commit()
    clear_cache()
    return name


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def clear_cache():
    try:
        from blueprints.accounts import classification as cls
        cls.clear_cache()
    except Exception:  # pragma: no cover - cache is an optimisation only
        pass


# ---------------------------------------------------------------------------
# Label passthrough (so a rename is visible in the Account form)
# ---------------------------------------------------------------------------
def apply_labels(node, row):
    """Copy a leaf row's display label into a built classification node."""
    if isinstance(node, dict) and row is not None:
        node['label'] = (row.config or {}).get('label') or row.name
    return node
