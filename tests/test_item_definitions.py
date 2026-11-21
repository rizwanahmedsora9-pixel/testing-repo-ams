"""Item Definitions — management page and table-backed readers.

These tests pin down the contract:

* a fresh database is seeded on boot (classification + flat groups),
* the management page renders for every group,
* a custom row can be added, renamed (label only, code stays), deactivated and
  re-activated, and deleted — but a locked/system row can never be touched,
* a definition that is used by a live record cannot be deleted,
* deactivating hides a row from the readers, and the readers are the
  single source of truth for forms (with seed fallback).
"""
import os

import pytest

from app import create_app
from models import Account, DirectSale, ItemDefinition, db
from app.services.item_definitions import (
    GROUP_ACCOUNT_CLASSIFICATION,
    GROUP_CHARGE_TYPE,
    GROUP_PAYMENT_METHOD,
    GROUP_SALE_CATEGORY,
    build_classification,
    get_group_row,
    get_group_rows,
    get_sale_categories,
    get_payment_methods,
    ensure_item_definitions,
)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB_PATH', str(tmp_path / 'definitions.db'))
    monkeypatch.setenv('AMS_SCHEMA_VERSION', 'v44')
    monkeypatch.setenv('DB_HEALTH_SNAPSHOT_PATH', str(tmp_path / 'health_snapshot.json'))
    monkeypatch.setenv('SECRET_KEY', 'isolated-item-definitions-test')
    monkeypatch.setenv('DEFAULT_ADMIN_USER', 'DefDefAdmin')
    monkeypatch.setenv('DEFAULT_ADMIN_PASSWORD', 'definitions-test-password')
    app = create_app({'TESTING': True, 'INSTANCE_PATH': tmp_path / 'instance',
                      'ENABLE_BACKGROUND_WORKERS': False, 'DAILY_BACKUP_SCHEDULER': False,
                      'SESSION_COOKIE_SECURE': False})
    assert not app.config.get('AMS_BOOTSTRAP_ERROR')
    web = app.test_client()
    assert web.post('/login', data={'username': 'DefDefAdmin',
                                    'password': 'definitions-test-password'}).status_code == 302
    return app, web


def _definition_count(app, group):
    with app.app_context():
        return ItemDefinition.query.filter_by(group_key=group).count()


def _rows(app, group, active_only=False):
    with app.app_context():
        rows = ItemDefinition.query.filter_by(group_key=group)
        if active_only:
            rows = rows.filter_by(is_active=True)
        return [r for r in rows.all()]


def _first(app, group, code):
    with app.app_context():
        return ItemDefinition.query.filter_by(group_key=group, code=code).one()


# ----------------------------------------------------------------------------
# Boot seed
# ----------------------------------------------------------------------------

def test_fresh_db_seeds_all_groups(desk):
    app, _ = desk
    assert _definition_count(app, GROUP_SALE_CATEGORY) == 5
    assert _definition_count(app, GROUP_CHARGE_TYPE) == 5
    assert _definition_count(app, GROUP_PAYMENT_METHOD) == 5
    # Classification: categories + subcategories + leaves from the seed dict.
    assert _definition_count(app, GROUP_ACCOUNT_CLASSIFICATION) >= 20


def test_boot_seed_is_idempotent(desk):
    app, _ = desk
    before = _definition_count(app, GROUP_SALE_CATEGORY)
    with app.app_context():
        ensure_item_definitions()
    assert _definition_count(app, GROUP_SALE_CATEGORY) == before


def test_seed_classification_matches_seed_dict(desk):
    from blueprints.accounts.classification import CLASSIFICATION_SEED

    app, _ = desk
    with app.app_context():
        tree = build_classification()
    assert tree is not None
    fallback = CLASSIFICATION_SEED
    assert set(fallback.keys()) <= set(tree.keys())
    for cat, nodes in fallback.items():
        if cat not in tree:
            continue
        assert set(nodes['subcategories'].keys()) <= set(tree[cat]['subcategories'].keys())


# ----------------------------------------------------------------------------
# Page rendering
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('group', ['account_classification', 'sale_category',
                                   'charge_type', 'payment_method'])
def test_page_renders_for_every_group(desk, group):
    _, web = desk
    response = web.get(f'/item-definitions?group={group}')
    assert response.status_code == 200
    assert b'Item Definitions' in response.data


def test_sale_category_page_is_editable(desk):
    _, web = desk
    response = web.get('/item-definitions?group=sale_category')
    assert b'/item-definitions/add' in response.data


def test_locked_group_page_shows_lock_notice(desk):
    _, web = desk
    response = web.get('/item-definitions?group=payment_method')
    assert b'Locked group' in response.data


# ----------------------------------------------------------------------------
# Flat-group CRUD
# ----------------------------------------------------------------------------

def test_add_sale_category_visible_in_readers(desk):
    app, web = desk
    response = web.post('/item-definitions/add', data={'group': GROUP_SALE_CATEGORY,
                                                       'name': 'Retail'})
    assert response.status_code in (302, 303)
    row = _first(app, GROUP_SALE_CATEGORY, 'Retail')
    assert row is not None
    assert row.is_system is False
    with app.app_context():
        codes = [code for code, _name in get_sale_categories()]
    assert 'Retail' in codes


def test_rename_changes_label_not_code(desk):
    app, web = desk
    row = _first(app, GROUP_SALE_CATEGORY, 'Cash')
    with app.app_context():
        # Seed (system) row — rename must be refused.
        pass
    response = web.post(f'/item-definitions/{row.id}/rename', data={'name': 'Cash Real'})
    assert response.status_code in (302, 303)
    with app.app_context():
        fresh = db.session.get(ItemDefinition, row.id)
        assert fresh.name == 'Cash'  # locked seed row unchanged
        assert fresh.code == 'Cash'


def test_rename_custom_row_keeps_code(desk):
    app, web = desk
    web.post('/item-definitions/add', data={'group': GROUP_SALE_CATEGORY, 'name': 'Temp Cat'})
    row = _first(app, GROUP_SALE_CATEGORY, 'Temp Cat')
    response = web.post(f'/item-definitions/{row.id}/rename', data={'name': 'Renamed Cat'})
    assert response.status_code in (302, 303)
    with app.app_context():
        fresh = db.session.get(ItemDefinition, row.id)
        assert fresh.code == 'Temp Cat'
        assert fresh.name == 'Renamed Cat'


def test_deactivate_hides_from_readers_and_reactivate_restores(desk):
    app, web = desk
    web.post('/item-definitions/add', data={'group': GROUP_SALE_CATEGORY, 'name': 'Hide Me'})
    row = _first(app, GROUP_SALE_CATEGORY, 'Hide Me')
    response = web.post(f'/item-definitions/{row.id}/toggle')
    assert response.status_code in (302, 303)
    with app.app_context():
        fresh = db.session.get(ItemDefinition, row.id)
        assert fresh.is_active is False
        codes = [code for code, _name in get_sale_categories()]
    assert 'Hide Me' not in codes
    response = web.post(f'/item-definitions/{row.id}/toggle')
    assert response.status_code in (302, 303)
    with app.app_context():
        fresh = db.session.get(ItemDefinition, row.id)
        assert fresh.is_active is True
        codes = [code for code, _name in get_sale_categories()]
    assert 'Hide Me' in codes


def test_unused_custom_row_can_be_deleted(desk):
    app, web = desk
    web.post('/item-definitions/add', data={'group': GROUP_SALE_CATEGORY, 'name': 'Doomed'})
    row = _first(app, GROUP_SALE_CATEGORY, 'Doomed')
    response = web.post(f'/item-definitions/{row.id}/delete')
    assert response.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(ItemDefinition, row.id) is None


def test_used_custom_row_cannot_be_deleted(desk):
    app, web = desk
    web.post('/item-definitions/add', data={'group': GROUP_SALE_CATEGORY, 'name': 'In Use'})
    row = _first(app, GROUP_SALE_CATEGORY, 'In Use')
    with app.app_context():
        sale = DirectSale(client_name='Test Client', category='In Use', amount=100)
        db.session.add(sale)
        db.session.commit()
    response = web.post(f'/item-definitions/{row.id}/delete')
    assert response.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(ItemDefinition, row.id) is not None


def test_system_row_cannot_be_deleted_or_deactivated(desk):
    app, web = desk
    row = _first(app, GROUP_SALE_CATEGORY, 'Cash')
    response = web.post(f'/item-definitions/{row.id}/delete')
    assert response.status_code in (302, 303)
    response = web.post(f'/item-definitions/{row.id}/toggle')
    assert response.status_code in (302, 303)
    with app.app_context():
        fresh = db.session.get(ItemDefinition, row.id)
        assert fresh is not None
        assert fresh.is_active is True


def test_payment_method_locked_group_not_editable(desk):
    app, web = desk
    response = web.post('/item-definitions/add', data={'group': GROUP_PAYMENT_METHOD,
                                                        'name': 'Petty Cash'})
    assert response.status_code in (302, 303)
    assert _definition_count(app, GROUP_PAYMENT_METHOD) == 5


# ----------------------------------------------------------------------------
# Classification CRUD
# ----------------------------------------------------------------------------

def test_add_subcategory_under_category_appears_in_classification(desk):
    app, web = desk
    with app.app_context():
        assets = ItemDefinition.query.filter_by(
            group_key=GROUP_ACCOUNT_CLASSIFICATION, level='category', code='Assets'
        ).first()
        assert assets is not None
    response = web.post('/item-definitions/add', data={
        'group': GROUP_ACCOUNT_CLASSIFICATION,
        'kind': 'subcategory',
        'parent_code': 'Assets',
        'name': 'Test Sub Category',
    })
    assert response.status_code in (302, 303)
    with app.app_context():
        row = ItemDefinition.query.filter_by(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                                             code='Test Sub Category').one()
        assert row.parent_id == assets.id
        assert row.level == 'subcategory'
        tree = build_classification()
    assert 'Test Sub Category' in tree['Assets']['subcategories']


def test_account_type_parent_must_be_subcategory(desk):
    app, web = desk
    # Passing a category code as parent must be refused.
    response = web.post('/item-definitions/add', data={
        'group': GROUP_ACCOUNT_CLASSIFICATION,
        'kind': 'account_type',
        'parent_code': 'Assets',
        'name': 'Bad Parent Type',
    })
    assert response.status_code in (302, 303)
    with app.app_context():
        assert ItemDefinition.query.filter_by(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                                              code='Bad Parent Type').first() is None


def test_classification_leafless_custom_subcategory_can_be_deleted(desk):
    app, web = desk
    web.post('/item-definitions/add', data={
        'group': GROUP_ACCOUNT_CLASSIFICATION,
        'kind': 'subcategory',
        'parent_code': 'Assets',
        'name': 'Kid Colony',
    })
    row = _first(app, GROUP_ACCOUNT_CLASSIFICATION, 'Kid Colony')
    response = web.post(f'/item-definitions/{row.id}/delete')
    assert response.status_code in (302, 303)
    # A leafless custom subcategory may be deleted.
    with app.app_context():
        assert db.session.get(ItemDefinition, row.id) is None


def test_classification_subcategory_with_children_cannot_be_deleted(desk):
    app, web = desk
    web.post('/item-definitions/add', data={
        'group': GROUP_ACCOUNT_CLASSIFICATION,
        'kind': 'subcategory',
        'parent_code': 'Assets',
        'name': 'Child House',
    })
    sub = _first(app, GROUP_ACCOUNT_CLASSIFICATION, 'Child House')
    with app.app_context():
        leaf = ItemDefinition(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                              code='Child Leaf', name='Child Leaf',
                              parent_id=sub.id, level='account_type',
                              config={'label': 'Child Leaf'},
                              is_system=False, sort_order=100)
        db.session.add(leaf)
        db.session.commit()
    response = web.post(f'/item-definitions/{sub.id}/delete')
    assert response.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(ItemDefinition, sub.id) is not None


def test_classification_account_type_usage_blocks_delete(desk):
    app, web = desk
    web.post('/item-definitions/add', data={
        'group': GROUP_ACCOUNT_CLASSIFICATION,
        'kind': 'subcategory',
        'parent_code': 'Assets',
        'name': 'Bank Branches',
    })
    with app.app_context():
        sub = ItemDefinition.query.filter_by(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                                              code='Bank Branches').one()
        leaf_id = ItemDefinition.query.filter_by(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                                                  code='Test Branch Leaf').first()
        if leaf_id is None:
            leaf = ItemDefinition(group_key=GROUP_ACCOUNT_CLASSIFICATION,
                                code='Test Branch Leaf', name='Test Branch Leaf',
                                parent_id=sub.id, level='account_type',
                                config={'label': 'Test Branch Leaf'},
                                is_system=False, sort_order=99)
            db.session.add(leaf)
            db.session.flush()
            leaf_id = leaf.id
        db.session.commit()
        # Create an account that uses the leaf, then try to delete it.
        account = Account(name='Branch Test', type='RECON',
                          account_type='Test', category='bank', channel='bank',
                          class_category='Assets', class_subcategory='Bank Branches',
                          class_account_type='Test Branch Leaf')
        db.session.add(account)
        db.session.commit()
    response = web.post(f'/item-definitions/{leaf_id}/delete')
    assert response.status_code in (302, 303)
    with app.app_context():
        assert db.session.get(ItemDefinition, leaf_id) is not None


# ----------------------------------------------------------------------------
# Readers keep the forms consistent
# ----------------------------------------------------------------------------

def test_payment_methods_reader_returns_pairs(desk):
    app, _ = desk
    with app.app_context():
        methods = get_payment_methods()
    assert ('Cash', 'Cash') in methods
    assert all(isinstance(code, str) and isinstance(label, str) for code, label in methods)


def test_seed_fallback_when_table_empty(desk):
    app, _ = desk
    with app.app_context():
        ItemDefinition.query.filter_by(group_key=GROUP_SALE_CATEGORY).delete()
        db.session.commit()
        categories = get_sale_categories()
    assert ('Cash', 'Cash') in categories
