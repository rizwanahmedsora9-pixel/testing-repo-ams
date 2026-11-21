"""Account Types master — creating / editing the leaves of the classification.

Contract under test
-------------------
* an admin can create an account type under a subcategory **with a channel and a
  linked party** (previously every custom leaf fell back to ``ledger_only`` /
  ``none``, which is why "cash / bank" types could not be added at all);
* the new type is immediately visible to the Account form (``registry_json``)
  and a real account can be saved with it;
* a rename changes the *label* the form shows but never the ``code`` an existing
  account stores, so saved accounts keep resolving;
* built-in (``is_system``) leaves are locked; a used leaf cannot be deleted;
* non-admins cannot mutate anything.
"""
import pytest

from app import create_app
from models import Account, ItemDefinition, User, db
from app.services import account_types as svc
from app.services.item_definitions import GROUP_ACCOUNT_CLASSIFICATION

BANK = 'Bank'           # seeded subcategory under Assets
LEAF_CODE = 'meezan_bank_gujrat'


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_DB_PATH', str(tmp_path / 'account_types.db'))
    monkeypatch.setenv('AMS_SCHEMA_VERSION', 'v44')
    monkeypatch.setenv('DB_HEALTH_SNAPSHOT_PATH', str(tmp_path / 'health.json'))
    monkeypatch.setenv('SECRET_KEY', 'isolated-account-types-test')
    monkeypatch.setenv('DEFAULT_ADMIN_USER', 'TypesAdmin')
    monkeypatch.setenv('DEFAULT_ADMIN_PASSWORD', 'types-test-password')
    app = create_app({'TESTING': True, 'INSTANCE_PATH': tmp_path / 'instance',
                      'ENABLE_BACKGROUND_WORKERS': False, 'DAILY_BACKUP_SCHEDULER': False,
                      'SESSION_COOKIE_SECURE': False})
    assert not app.config.get('AMS_BOOTSTRAP_ERROR')
    web = app.test_client()
    assert web.post('/login', data={'username': 'TypesAdmin',
                                    'password': 'types-test-password'}).status_code == 302
    return app, web


def _leaf(app, code=LEAF_CODE):
    with app.app_context():
        return ItemDefinition.query.filter_by(
            group_key=GROUP_ACCOUNT_CLASSIFICATION, code=code).first()


def _create(web, name='Meezan Bank (Gujrat)', channels=None, **extra):
    payload = {'class_subcategory': BANK, 'name': name,
               'channels': channels if channels is not None else ['bank'],
               'default_channel': (channels or ['bank'])[0],
               'entity': 'none'}
    payload.update(extra)
    return web.post('/accounts/api/account-types/add', json=payload)


# ---------------------------------------------------------------------------
# Creating a type
# ---------------------------------------------------------------------------
def test_master_page_renders(desk):
    _, web = desk
    response = web.get('/accounts/account-types')
    assert response.status_code == 200
    assert b'Account Types' in response.data
    assert b'Operating Bank' in response.data      # seeded leaf listed


def test_create_type_with_bank_channel(desk):
    app, web = desk
    response = _create(web, channels=['bank'])
    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert body['account_type']['channels'] == ['bank']
    assert body['account_type']['default_channel'] == 'bank'
    assert body['account_type']['code'] == LEAF_CODE        # slugged code
    assert body['account_type']['label'] == 'Meezan Bank (Gujrat)'
    row = _leaf(app)
    assert row is not None and row.is_system is False


def test_create_type_with_entity_and_multiple_channels(desk):
    app, web = desk
    response = _create(web, name='Client Wallet Book', channels=['digital_wallet', 'ledger_only'],
                       entity='client')
    assert response.status_code == 200
    node = response.get_json()['account_type']
    assert node['channels'] == ['digital_wallet', 'ledger_only']
    assert node['entity'] == 'client'


def test_new_type_appears_in_account_form_registry(desk):
    app, web = desk
    _create(web, channels=['bank'])
    with app.app_context():
        from blueprints.accounts.classification import registry_json, is_valid_triple
        registry = registry_json()
        assets = [c for c in registry['categories'] if c['name'] == 'Assets'][0]
        bank = [s for s in assets['subcategories'] if s['name'] == BANK][0]
        leaf = [t for t in bank['account_types'] if t['name'] == LEAF_CODE]
        assert leaf, 'new type missing from the form registry'
        assert leaf[0]['label'] == 'Meezan Bank (Gujrat)'
        assert leaf[0]['channels'] == ['bank']
        assert is_valid_triple('Assets', BANK, LEAF_CODE)


def test_account_can_be_saved_with_the_new_type(desk):
    app, web = desk
    _create(web, channels=['bank'])
    response = web.post('/accounts/accounts/add', data={
        'name': 'Meezan Gujrat', 'source_category': 'Company',
        'class_category': 'Assets', 'class_subcategory': BANK,
        'class_account_type': LEAF_CODE, 'channel': 'bank',
        'bank_name': 'Meezan', 'account_holder_name': 'AMS',
        'account_number': 'PK00TEST', 'opening_amount': '1000',
        'opening_position': 'debit', 'opening_effective_date': '2026-01-01',
        'account_status': 'active',
    }, follow_redirects=True)
    assert response.status_code == 200
    with app.app_context():
        account = Account.query.filter_by(class_account_type=LEAF_CODE).first()
        assert account is not None
        assert account.channel == 'bank'
        assert account.category == 'bank'          # legacy column kept in sync
        assert account.bank_name == 'Meezan'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_duplicate_name_under_same_subcategory_is_refused(desk):
    app, web = desk
    assert _create(web).status_code == 200
    second = _create(web)
    assert second.status_code == 400
    assert 'already exists' in second.get_json()['error']


def test_same_name_under_different_subcategory_is_allowed(desk):
    app, web = desk
    assert _create(web).status_code == 200
    other = web.post('/accounts/api/account-types/add', json={
        'class_subcategory': 'Cash', 'name': 'Meezan Bank (Gujrat)',
        'channels': ['cash']})
    assert other.status_code == 200
    assert other.get_json()['account_type']['subcategory'] == 'Cash'


def test_channels_are_required_and_validated(desk):
    app, web = desk
    assert _create(web, channels=[]).status_code == 400
    bad = web.post('/accounts/api/account-types/add', json={
        'class_subcategory': BANK, 'name': 'Space Money', 'channels': ['telepathy']})
    assert bad.status_code == 400
    assert 'telepathy' in bad.get_json()['error']


def test_unknown_subcategory_is_refused(desk):
    app, web = desk
    response = web.post('/accounts/api/account-types/add', json={
        'class_subcategory': 'No Such Sub', 'name': 'X', 'channels': ['cash']})
    assert response.status_code == 400
    assert 'valid subcategory' in response.get_json()['error']


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------
def test_edit_label_keeps_code_and_saved_account(desk):
    app, web = desk
    _create(web)
    row = _leaf(app)
    web.post('/accounts/accounts/add', data={
        'name': 'Meezan Gujrat', 'source_category': 'Company',
        'class_category': 'Assets', 'class_subcategory': BANK,
        'class_account_type': LEAF_CODE, 'channel': 'bank',
        'bank_name': 'Meezan', 'account_holder_name': 'AMS',
        'account_number': 'PK01', 'opening_amount': '0',
        'opening_position': 'debit', 'opening_effective_date': '2026-01-01',
        'account_status': 'active',
    }, follow_redirects=True)

    response = web.post(f'/accounts/api/account-types/{row.id}/edit',
                        json={'name': 'Meezan Gujrat Current'})
    assert response.status_code == 200
    node = response.get_json()['account_type']
    assert node['label'] == 'Meezan Gujrat Current'
    assert node['code'] == LEAF_CODE                 # stable key
    with app.app_context():
        fresh = ItemDefinition.query.filter_by(code=LEAF_CODE).one()
        assert fresh.name == 'Meezan Gujrat Current'
        assert Account.query.filter_by(class_account_type=LEAF_CODE).count() == 1

    # The Account form now shows the new label, and the account still loads.
    page = web.get('/accounts/accounts/add')
    assert b'Meezan Gujrat Current' in page.data
    with app.app_context():
        account = Account.query.filter_by(class_account_type=LEAF_CODE).one()
        assert web.get(f'/accounts/{account.id}/edit').status_code == 200


def test_edit_channels_of_existing_type(desk):
    app, web = desk
    _create(web, channels=['bank'])
    row = _leaf(app)
    response = web.post(f'/accounts/api/account-types/{row.id}/edit',
                        json={'channels': ['bank', 'digital_wallet'], 'default_channel': 'bank'})
    assert response.status_code == 200
    assert response.get_json()['account_type']['channels'] == ['bank', 'digital_wallet']


def test_system_leaf_is_locked(desk):
    app, web = desk
    with app.app_context():
        system = ItemDefinition.query.filter_by(
            group_key=GROUP_ACCOUNT_CLASSIFICATION, code='Main Cash').one()
        system_id = system.id
    edit = web.post(f'/accounts/api/account-types/{system_id}/edit', json={'name': 'Hacked'})
    assert edit.status_code == 400
    assert 'locked' in edit.get_json()['error']
    toggle = web.post(f'/accounts/api/account-types/{system_id}/toggle', json={'active': False})
    assert toggle.status_code == 400
    delete = web.post(f'/accounts/api/account-types/{system_id}/delete')
    assert delete.status_code == 400


# ---------------------------------------------------------------------------
# Activate / delete
# ---------------------------------------------------------------------------
def test_toggle_hides_type_from_the_form(desk):
    app, web = desk
    _create(web)
    row = _leaf(app)
    response = web.post(f'/accounts/api/account-types/{row.id}/toggle', json={'active': False})
    assert response.status_code == 200
    assert response.get_json()['account_type']['is_active'] is False
    with app.app_context():
        from blueprints.accounts.classification import is_valid_triple
        assert not is_valid_triple('Assets', BANK, LEAF_CODE)


def test_used_type_cannot_be_deleted_but_unused_can(desk):
    app, web = desk
    _create(web)
    unused = _leaf(app)
    assert web.post(f'/accounts/api/account-types/{unused.id}/delete').status_code == 200
    assert _leaf(app) is None

    _create(web)
    row = _leaf(app)
    web.post('/accounts/accounts/add', data={
        'name': 'Meezan Gujrat', 'source_category': 'Company',
        'class_category': 'Assets', 'class_subcategory': BANK,
        'class_account_type': LEAF_CODE, 'channel': 'bank',
        'bank_name': 'Meezan', 'account_holder_name': 'AMS',
        'account_number': 'PK02', 'opening_amount': '0',
        'opening_position': 'debit', 'opening_effective_date': '2026-01-01',
        'account_status': 'active',
    }, follow_redirects=True)
    blocked = web.post(f'/accounts/api/account-types/{row.id}/delete')
    assert blocked.status_code == 400
    assert 'cannot be deleted' in blocked.get_json()['error']


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def test_non_admin_cannot_manage_types(desk):
    app, web = desk
    with app.app_context():
        from werkzeug.security import generate_password_hash
        user = User(username='PlainUser', role='user',
                    password_hash=generate_password_hash('plain-password'))
        user.can_manage_accounts = True
        db.session.add(user)
        db.session.commit()
    web.post('/logout')
    web.post('/login', data={'username': 'PlainUser', 'password': 'plain-password'})

    assert web.post('/accounts/api/account-types/add', json={
        'class_subcategory': BANK, 'name': 'Sneaky', 'channels': ['cash']}).status_code == 403
    page = web.get('/accounts/account-types')
    assert page.status_code == 302          # redirected to Manage Accounts


# ---------------------------------------------------------------------------
# Service layer (no HTTP)
# ---------------------------------------------------------------------------
def test_service_serialise_and_usage(desk):
    app, web = desk
    _create(web)
    with app.app_context():
        row = ItemDefinition.query.filter_by(code=LEAF_CODE).one()
        node = svc.serialise(row)
        assert node['category'] == 'Assets'
        assert node['subcategory'] == BANK
        assert node['usage_count'] == 0
        with pytest.raises(svc.AccountTypeError):
            svc.create('Broken', BANK, [])
