"""Account Types master — the missing "add / edit account type" surface.

The Account form already had a "Quick Add Group" panel for categories.  This
blueprint gives the same treatment to **account types** (the leaves of
``Category → Subcategory → Account Type``), which previously could only come
from the code seed.

Routes
------
``GET  /accounts/account-types``                 master page (table + edit modals)
``POST /accounts/api/account-types/add``         JSON: create a custom type
``POST /accounts/api/account-types/<id>/edit``   JSON: label + channel + entity
``POST /accounts/api/account-types/<id>/toggle`` JSON: activate / deactivate
``POST /accounts/api/account-types/<id>/delete`` JSON: delete (only if unused)

Everything is validated in ``app.services.account_types`` — the browser is
never trusted.  Mutations are admin/root only (same rule as the rest of the
account master data, ``_deny_account_master_mutation``).
"""
from __future__ import annotations

from flask import jsonify, render_template, request, url_for
from flask_login import login_required

from ._common import *  # noqa  (accounts_bp, db, current_user, audit_log, ...)
from app.services import account_types as svc


def _payload():
    """Merge JSON body and form body so both callers work."""
    data = {}
    if request.is_json:
        data.update(request.get_json(silent=True) or {})
    data.update(request.form.to_dict(flat=True))
    # multi-select from a normal form post arrives as repeated keys
    multi = request.form.getlist('channels')
    if multi:
        existing = data.get('channels')
        if not existing or isinstance(existing, str):
            data['channels'] = multi
    return data


def _channels_from(data):
    raw = data.get('channels')
    if isinstance(raw, str):
        return [p for p in raw.replace(';', ',').split(',') if p.strip()]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _can_manage():
    return (getattr(current_user, 'role', '') or '').strip().lower() in ('admin', 'root')


def _deny():
    return jsonify({'ok': False, 'error': 'Only an admin can manage account types.'}), 403


@accounts_bp.route('/account-types')
@login_required
def account_types_page():
    """Master list of account types, grouped by category → subcategory."""
    if not _can_manage():
        from flask import flash, redirect
        flash('Unauthorized: admin access required to manage account types.', 'danger')
        return redirect(url_for('accounts.manage_accounts'))

    from blueprints.accounts.classification import CHANNEL_LABELS, ENTITY_LABELS, registry_json
    rows = svc.subcategories(active_only=False)
    tree = []
    for entry in rows:
        sub = entry['row']
        leaves = [svc.serialise(r) for r in
                  svc.leaves_for_subcategory(sub.code, include_inactive=True)]
        tree.append({
            'category': entry['category'],
            'subcategory': sub.code,
            'subcategory_label': sub.name,
            'is_active': bool(sub.is_active),
            'account_types': leaves,
        })
    tree.sort(key=lambda n: (n['category'].lower(), n['subcategory_label'].lower()))

    return render_template(
        'accounts/account_types.html',
        tree=tree,
        channel_labels=CHANNEL_LABELS,
        entity_labels=ENTITY_LABELS,
        registry=registry_json(),
        can_manage_master=True,
    )


@accounts_bp.route('/api/account-types/add', methods=['POST'])
@login_required
def api_add_account_type():
    """Create a custom account type under a subcategory."""
    if not _can_manage():
        return _deny()
    data = _payload()
    try:
        row = svc.create(
            name=data.get('name'),
            subcategory_code=data.get('class_subcategory') or data.get('subcategory'),
            channels=_channels_from(data),
            default_channel=data.get('default_channel'),
            entity=data.get('entity') or 'none',
        )
    except svc.AccountTypeError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        logger.exception('Add account type failed')
        return jsonify({'ok': False, 'error': f'Unable to add account type: {exc}'}), 400

    audit_log(current_user, 'account.type.create',
              f'{row.code} under {data.get("class_subcategory") or data.get("subcategory")}')
    return jsonify({'ok': True, 'account_type': svc.serialise(row)})


@accounts_bp.route('/api/account-types/<int:def_id>/edit', methods=['POST'])
@login_required
def api_edit_account_type(def_id):
    """Edit a custom account type: label, channels, default channel, entity."""
    if not _can_manage():
        return _deny()
    data = _payload()
    channels = _channels_from(data) if ('channels' in data) else None
    try:
        row = svc.update(
            def_id,
            name=data.get('name'),
            channels=channels,
            default_channel=data.get('default_channel'),
            entity=data.get('entity'),
        )
    except svc.AccountTypeError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive
        db.session.rollback()
        logger.exception('Edit account type failed')
        return jsonify({'ok': False, 'error': f'Unable to edit account type: {exc}'}), 400

    audit_log(current_user, 'account.type.edit',
              f'{row.code} name={row.name} channels={",".join((row.config or {}).get("channels") or [])}')
    return jsonify({'ok': True, 'account_type': svc.serialise(row)})


@accounts_bp.route('/api/account-types/<int:def_id>/toggle', methods=['POST'])
@login_required
def api_toggle_account_type(def_id):
    if not _can_manage():
        return _deny()
    data = _payload()
    active = str(data.get('active', '')).strip().lower() in ('1', 'true', 'yes', 'on')
    if 'active' not in data:
        row = svc.get_row(def_id)
        active = not bool(row and row.is_active)
    try:
        row = svc.set_active(def_id, active)
    except svc.AccountTypeError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    audit_log(current_user, 'account.type.toggle', f'{row.code} is_active={row.is_active}')
    return jsonify({'ok': True, 'account_type': svc.serialise(row)})


@accounts_bp.route('/api/account-types/<int:def_id>/delete', methods=['POST'])
@login_required
def api_delete_account_type(def_id):
    if not _can_manage():
        return _deny()
    try:
        name = svc.delete(def_id)
    except svc.AccountTypeError as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 400
    audit_log(current_user, 'account.type.delete', f'id={def_id} name={name}')
    return jsonify({'ok': True, 'deleted': name})


# Package auto-wiring (utils.pkg_wire) only copies names that exist at import
# time of the package, so make the routes module importable for discovery.
__all__ = [
    'account_types_page', 'api_add_account_type', 'api_edit_account_type',
    'api_toggle_account_type', 'api_delete_account_type',
]
