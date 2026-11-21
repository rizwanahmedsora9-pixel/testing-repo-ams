"""Item Definitions — manage the user-editable business vocabulary.

Routes
------
``GET  /item-definitions``               the management page
``POST /item-definitions/add``           add a definition (non-system groups)
``POST /item-definitions/<int:def_id>/rename``
``POST /item-definitions/<int:def_id>/toggle``  deactivate / reactivate
``POST /item-definitions/<int:def_id>/delete``  delete (only if unused)

Permission: admin or ``can_access_settings``.  Deleting or renaming a *system*
seed row is blocked — seeds are locked business definitions that code may
special-case.  A *custom* row (is_system=False) may be renamed, deactivated
and deleted (delete only when no live record references it).
"""
from ._common import *  # noqa

from app.services.item_definitions import (
    GROUP_ACCOUNT_CLASSIFICATION,
    GROUP_SALE_CATEGORY,
    GROUP_CHARGE_TYPE,
    GROUP_PAYMENT_METHOD,
    GROUP_LABELS,
    GROUP_HELP,
    LOCKED_GROUPS,
    get_group_rows,
    get_group_row,
    group_label,
    normalize_group,
)


def _can_manage_definitions():
    if not current_user.is_authenticated:
        return False
    if current_user.role in ('admin', 'root'):
        return True
    return _user_can('can_access_settings')


# ---------------------------------------------------------------------------
# Usage counts (rows referencing a definition)
# ---------------------------------------------------------------------------
def _usage_count(group, row):
    """Number of live records referencing this definition."""
    code = (row.code or '').strip()
    if not code:
        return 0
    if group == GROUP_SALE_CATEGORY:
        return DirectSale.query.filter(DirectSale.is_void == False,
                                       DirectSale.category == code).count()
    if group == GROUP_CHARGE_TYPE:
        return GRNCharge.query.filter(GRNCharge.is_void == False,
                                      GRNCharge.charge_type == code).count()
    if group == GROUP_PAYMENT_METHOD:
        p1 = Payment.query.filter(Payment.method == code).count()
        p2 = DirectSale.query.filter(DirectSale.is_void == False,
                                     DirectSale.payment_method == code).count()
        return p1 + p2
    if group == GROUP_ACCOUNT_CLASSIFICATION:
        if row.level == 'category':
            return Account.query.filter_by(class_category=code).count()
        if row.level == 'subcategory':
            return Account.query.filter_by(class_subcategory=code).count()
        if row.level == 'account_type':
            return Account.query.filter_by(class_account_type=code).count()
    return 0


def _classification_tree(rows):
    """Organise flat rows into the nested category/subcategory/leaf shape."""
    by_id = {r.id: r for r in rows}
    categories = []
    subs_by_cat = {}
    leaves_by_sub = {}

    for r in rows:
        if r.parent_id is None:
            categories.append(r)
            continue
        parent = by_id.get(r.parent_id)
        if parent is None:
            continue
        if parent.parent_id is None:
            subs_by_cat.setdefault(parent.code, []).append(r)
            continue
        grandparent = by_id.get(parent.parent_id)
        if grandparent is None:
            continue
        leaves_by_sub.setdefault(parent.code, []).append(r)

    tree = []
    for cat in categories:
        tree.append({
            'category': cat,
            'subcategories': [
                {'subcategory': sub,
                 'account_types': leaves_by_sub.get(sub.code, [])}
                for sub in subs_by_cat.get(cat.code, [])
            ],
        })
    return tree


@bp.route('/item-definitions', methods=['GET'])
@login_required
def item_definitions_page():
    if not _can_manage_definitions():
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    selected_group = normalize_group(request.args.get('group') or GROUP_ACCOUNT_CLASSIFICATION)
    if not selected_group:
        selected_group = GROUP_ACCOUNT_CLASSIFICATION

    rows = get_group_rows(selected_group, active_only=False)

    is_classification = selected_group == GROUP_ACCOUNT_CLASSIFICATION
    can_edit = selected_group not in LOCKED_GROUPS

    if is_classification:
        tree = _classification_tree(rows)
        parent_options_sub = [r for r in rows if r.level == 'subcategory' and r.is_active]
        parent_options_cat = [r for r in rows if r.level == 'category' and r.is_active]
    else:
        tree = None
        parent_options_sub = []
        parent_options_cat = []

    # flat group: parent options reduce to the active codes
    def _parent_for(row):
        if not row.parent_id:
            return None
        return next((p for p in rows if p.id == row.parent_id), None)

    for row in rows:
        row.usage_count = _usage_count(selected_group, row)
        row.parent = _parent_for(row)

    return render_template(
        'item_definitions.html',
        groups=GROUP_LABELS,
        group_help=GROUP_HELP,
        selected_group=selected_group,
        selected_label=group_label(selected_group),
        is_classification=is_classification,
        can_edit=can_edit,
        tree=tree,
        rows=rows,
        parent_options_sub=parent_options_sub,
        parent_options_cat=parent_options_cat,
        locked_groups=LOCKED_GROUPS,
    )


@bp.route('/item-definitions/add', methods=['POST'])
@login_required
def add_item_definition():
    if not _can_manage_definitions():
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    group = normalize_group(request.form.get('group'))
    if not group:
        flash('Unknown definition group.', 'danger')
        return redirect(url_for('item_definitions_page'))
    if group in LOCKED_GROUPS:
        flash((
            f'{group_label(group)} values are locked by the code — '
            'a new value is a code change, not a data change.'
        ), 'warning')
        return redirect(url_for('item_definitions_page', group=group))

    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Definition name is required.', 'danger')
        return redirect(url_for('item_definitions_page', group=group))
    if len(name) > 120:
        flash('Definition name must be 120 characters or fewer.', 'danger')
        return redirect(url_for('item_definitions_page', group=group))

    is_classification = group == GROUP_ACCOUNT_CLASSIFICATION
    kind = (request.form.get('kind') or '').strip()
    parent_code = (request.form.get('parent_code') or '').strip()

    if is_classification:
        if kind not in ('subcategory', 'account_type'):
            flash('Choose what you are adding: a subcategory or an account type.', 'danger')
            return redirect(url_for('item_definitions_page', group=group))
        # Resolve parent
        parent = None
        if kind == 'subcategory':
            # Parent = a category root
            parent = get_group_row(group, parent_code) if parent_code else None
            if parent and (parent.level != 'category' or not parent.is_active):
                parent = None
        else:
            parent = get_group_row(group, parent_code) if parent_code else None
            if parent and (parent.level != 'subcategory' or not parent.is_active):
                parent = None
        if parent is None:
            flash('Choose a valid parent first.', 'danger')
            return redirect(url_for('item_definitions_page', group=group))

        # Derive code: slug of name
        code = name
        # Check uniqueness at (level, code)
        exists = ItemDefinition.query.filter_by(group_key=group, level=kind, code=code).first()
        if exists:
            flash(f'A {kind} named "{code}" already exists.', 'warning')
            return redirect(url_for('item_definitions_page', group=group))

        parent_row = parent
        row = ItemDefinition(
            group_key=group,
            code=code,
            name=name,
            parent_id=parent_row.id,
            level=kind,
            config={'label': name},
            is_active=True,
            is_system=False,
            sort_order=10 + ItemDefinition.query.filter_by(group_key=group).count(),
        )
        db.session.add(row)
        db.session.commit()
        try:
            from blueprints.accounts import classification as cls
            cls.clear_cache()
        except Exception:
            pass
        audit_log(current_user, 'item_definition.add', f'{group}/{kind} name={name}')
        flash(f'{kind.capitalize()} "{name}" added.', 'success')
        return redirect(url_for('item_definitions_page', group=group))

    # Flat groups
    code = name
    exists = ItemDefinition.query.filter_by(group_key=group, level='item', code=code).first()
    if exists:
        if not exists.is_active:
            exists.is_active = True
            db.session.commit()
            flash(f'"{exists.name}" was re-activated.', 'success')
        else:
            flash(f'"{name}" already exists.', 'warning')
        return redirect(url_for('item_definitions_page', group=group))

    row = ItemDefinition(
        group_key=group,
        code=code,
        name=name,
        level='item',
        config={'label': name},
        is_active=True,
        is_system=False,
        sort_order=10 + ItemDefinition.query.filter_by(group_key=group).count(),
    )
    db.session.add(row)
    db.session.commit()
    audit_log(current_user, 'item_definition.add', f'{group} name={name}')
    flash(f'"{name}" added.', 'success')
    return redirect(url_for('item_definitions_page', group=group))


@bp.route('/item-definitions/<int:def_id>/rename', methods=['POST'])
@login_required
def rename_item_definition(def_id):
    if not _can_manage_definitions():
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    row = db.session.get(ItemDefinition, def_id)
    if not row:
        flash('Definition not found.', 'danger')
        return redirect(url_for('item_definitions_page'))

    if row.is_system:
        flash('System definitions are locked and cannot be renamed.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))

    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Definition name is required.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))
    if len(name) > 120:
        flash('Definition name must be 120 characters or fewer.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))
    if name.casefold() == str(row.name or '').strip().casefold():
        flash('Name is unchanged.', 'info')
        return redirect(url_for('item_definitions_page', group=row.group_key))

    old = row.name
    group = row.group_key
    row.name = name
    if group == GROUP_CHARGE_TYPE:
        # charge labels come from metadata label
        meta = row.config or {}
        meta['label'] = name
        row.config = meta
    db.session.commit()
    try:
        from blueprints.accounts import classification as cls
        cls.clear_cache()
    except Exception:
        pass
    audit_log(current_user, 'item_definition.rename', f'{group}/{row.code}: "{old}" -> "{name}"')
    flash(f'"{old}" renamed to "{name}".', 'success')
    return redirect(url_for('item_definitions_page', group=group))


@bp.route('/item-definitions/<int:def_id>/toggle', methods=['POST'])
@login_required
def toggle_item_definition(def_id):
    if not _can_manage_definitions():
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    row = db.session.get(ItemDefinition, def_id)
    if not row:
        flash('Definition not found.', 'danger')
        return redirect(url_for('item_definitions_page'))

    if row.is_system:
        flash('System definitions are locked and cannot be deactivated.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))

    group = row.group_key
    row.is_active = not bool(row.is_active)
    db.session.commit()
    try:
        from blueprints.accounts import classification as cls
        cls.clear_cache()
    except Exception:
        pass
    audit_log(current_user, 'item_definition.toggle',
              f'{group}/{row.code} is_active={row.is_active}')
    flash(f'"{row.name}" ' + ('activated' if row.is_active else 'deactivated') + '.', 'success')
    return redirect(url_for('item_definitions_page', group=group))


@bp.route('/item-definitions/<int:def_id>/delete', methods=['POST'])
@login_required
def delete_item_definition(def_id):
    if not _can_manage_definitions():
        flash('Unauthorized: Admin access required.', 'danger')
        return redirect(url_for('index'))

    row = db.session.get(ItemDefinition, def_id)
    if not row:
        flash('Definition not found.', 'danger')
        return redirect(url_for('item_definitions_page'))

    if row.is_system:
        flash('System definitions are locked and cannot be deleted.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))

    usage = _usage_count(row.group_key, row)
    if usage:
        flash(f'"{row.name}" is used by {usage} record(s) and cannot be deleted. Rename or deactivate it instead.', 'danger')
        return redirect(url_for('item_definitions_page', group=row.group_key))

    # Classification: also block delete while child rows reference it
    if row.level in ('category', 'subcategory'):
        kids = ItemDefinition.query.filter_by(group_key=row.group_key, parent_id=row.id).all()
        kids_active = [k for k in kids if k.is_active]
        if kids_active:
            flash(
                f'"{row.name}" is used by {len(kids_active)} child definition(s) ({", ".join(k.name for k in kids_active[:5])}). Deactivate or delete the child definitions first.',
                'danger',
            )
            return redirect(url_for('item_definitions_page', group=row.group_key))

    code = row.code
    name = row.name
    group = row.group_key
    db.session.delete(row)
    db.session.commit()
    try:
        from blueprints.accounts import classification as cls
        cls.clear_cache()
    except Exception:
        pass
    audit_log(current_user, 'item_definition.delete', f'{group}/{code} name={name}')
    flash(f'"{name}" deleted.', 'success')
    return redirect(url_for('item_definitions_page', group=group))
