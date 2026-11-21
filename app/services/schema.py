"""Domain service module — extracted from legacy ERP core."""
from __future__ import annotations

import os
import io
import secrets
import json
import calendar
import threading
import time
import smtplib
import shutil
import sqlite3
import zipfile
import urllib.request
import urllib.error
import re
import logging
import importlib
from itertools import zip_longest
from urllib.parse import unquote
from contextlib import redirect_stderr
from email.message import EmailMessage
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlalchemy import func, case, text, or_, and_, exists, not_
from sqlalchemy.orm import selectinload
from types import SimpleNamespace
from flask import (
    current_app as app,
    render_template, request, redirect, url_for, flash, jsonify,
    send_file, Response, make_response, send_from_directory,
    got_request_exception, abort, session, g,
)
from flask_login import login_user, login_required, logout_user, current_user

from models import *
from utils.audit import audit_log
from utils.reconciliation import run_auto_reconcile
from cash_flow_reconciliation_helpers import (
    create_reconciliation, update_reconciliation, delete_reconciliation,
    get_reconciliation_history, migrate_legacy_record,
)
from app.services import constants as C
from app.services import state

# === explicit service imports ===
from app.services.health import (
    _db_debug_counts,
)
from app.services.lookups import (
    get_client_by_input,
    get_or_create_delivery_person,
)
from app.services.time_money import (
    pk_now,
)


# Rebind constants used as bare names
OPEN_KHATA_CODE = C.OPEN_KHATA_CODE
OPEN_KHATA_NAME = C.OPEN_KHATA_NAME
PK_TZ = C.PK_TZ
SALE_CATEGORY_CHOICES = C.SALE_CATEGORY_CHOICES
_SALE_CATEGORY_ALIASES = C._SALE_CATEGORY_ALIASES
DOMAIN_WIPE_REGISTRY = C.DOMAIN_WIPE_REGISTRY
USER_PERMISSION_DEFAULTS = C.USER_PERMISSION_DEFAULTS
PERMISSION_LEGACY_FALLBACKS = C.PERMISSION_LEGACY_FALLBACKS
ENDPOINT_PERMISSION_MAP = C.ENDPOINT_PERMISSION_MAP
AUTO_BILL_NS_DEFAULT = C.AUTO_BILL_NS_DEFAULT
AUTO_BILL_NAMESPACES = C.AUTO_BILL_NAMESPACES
EDITABLE_USER_PERMISSION_FIELDS = C.EDITABLE_USER_PERMISSION_FIELDS
basedir = C.basedir
legacy_instance_dir = C.legacy_instance_dir
legacy_db_path = C.legacy_db_path
db_path = C.db_path
_DB_HEALTH_SNAPSHOT_PATH = C._DB_HEALTH_SNAPSHOT_PATH
_max_upload_mb = C._max_upload_mb
_AUTO_BACKUP_ENABLED = C._AUTO_BACKUP_ENABLED
_WIPE_BACKUP_ENABLED = C._WIPE_BACKUP_ENABLED
_AUTO_RECONCILE_ENABLED = C._AUTO_RECONCILE_ENABLED
_AUTO_RECONCILE_FIX = C._AUTO_RECONCILE_FIX
_AUTO_RECONCILE_INTERVAL_SEC = C._AUTO_RECONCILE_INTERVAL_SEC
_AUTO_RECONCILE_TOL = C._AUTO_RECONCILE_TOL
_ALLOW_EMPTY_DB = C._ALLOW_EMPTY_DB
_ALLOW_DB_DROP = C._ALLOW_DB_DROP
_DB_HEALTH_DROP_RATIO = C._DB_HEALTH_DROP_RATIO
_DB_HEALTH_DROP_MIN = C._DB_HEALTH_DROP_MIN
_DB_HEALTH_MIN_BYTES = C._DB_HEALTH_MIN_BYTES

def _ensure_user_password_column():
    """Ensure `password_hash` column exists on `user` table and copy legacy `password` values."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('user')")).fetchall()
        cols = [r[1] for r in rows]
        if 'password_hash' not in cols:
            db.session.execute(text("ALTER TABLE user ADD COLUMN password_hash VARCHAR(200);"))
            if 'password' in cols:
                db.session.execute(text("UPDATE user SET password_hash = password WHERE password_hash IS NULL;"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_model_columns():
    """Add any missing columns declared in models but missing in the DB."""
    from sqlalchemy import String, Integer, Float, Date, DateTime, Boolean, Text, BigInteger, Numeric

    try:
        for table in db.metadata.sorted_tables:
            rows = db.session.execute(text(f"PRAGMA table_info('{table.name}')")).fetchall()
            existing_cols = [r[1] for r in rows]
            for col in table.columns:
                if col.name not in existing_cols:
                    coltype = col.type
                    sqltype = 'VARCHAR(200)'
                    if isinstance(coltype, (String, Text)):
                        sqltype = 'VARCHAR(200)'
                    elif isinstance(coltype, BigInteger) or str(coltype).upper().startswith('BIGINT'):
                        sqltype = 'BIGINT'
                    elif isinstance(coltype, (Integer, Boolean)) or str(coltype) == 'BOOLEAN':
                        sqltype = 'INTEGER'
                    elif isinstance(coltype, (Float, Numeric)):
                        sqltype = 'REAL'
                    elif isinstance(coltype, Date):
                        sqltype = 'DATE'
                    elif isinstance(coltype, DateTime):
                        sqltype = 'DATETIME'

                    try:
                        db.session.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {sqltype};"))
                    except Exception:
                        db.session.rollback()
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_material_categories():
    try:
        default_cat = get_or_create_material_category('General')
        if not default_cat:
            return
        mats = Material.query.filter(Material.category_id.is_(None)).all()
        for m in mats:
            m.category_id = default_cat.id
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_discount_columns():
    """Ensure discount and discount_reason columns exist on relevant tables."""
    tables = {
        'direct_sale': ['discount', 'discount_reason'],
        'booking': ['discount', 'discount_reason'],
        'payment': ['discount', 'discount_reason']
    }
    try:
        for table, cols in tables.items():
            rows = db.session.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
            existing = [r[1] for r in rows]
            for col in cols:
                if col not in existing:
                    col_type = 'REAL DEFAULT 0' if col == 'discount' else 'VARCHAR(200)'
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_document_archive_columns():
    """Additive columns that keep cancelled/voided documents readable.

    * ``booking_item.cancelled_qty`` — a cancellation used to delete the line,
      so the ordered material, quantity and rate were lost.  The line now stays
      and the cancelled quantity is recorded here.
    * ``material_return.voided_with_sale`` — when a bill is voided its returns
      are reversed with it; this marks the ones reversed *because of the sale*
      so restoring the bill restores exactly those.

    Idempotent (PRAGMA check first), so it is safe on every boot.
    """
    tables = {
        'booking_item': [('cancelled_qty', 'REAL DEFAULT 0')],
        'material_return': [('voided_with_sale', 'BOOLEAN DEFAULT 0')],
    }
    try:
        for table, cols in tables.items():
            rows = db.session.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
            existing = {r[1] for r in rows}
            if not existing:
                continue
            for col, col_type in cols:
                if col not in existing:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_line_discount_columns():
    """Per-line discount storage (see ``app.services.pricing``).

    A bill keeps **one** discount, but the profit report needs revenue per line.
    The split used to be recomputed in the report with a plain division, which
    is where repeating decimals such as ``4,756.0975609756`` came from.  The
    split now happens once, at posting, and is stored on the document line:

    * ``direct_sale_item.discount`` / ``discount_kind`` / ``discount_reason``
    * ``booking_item.discount`` / ``discount_reason``
    * ``material_return_item.discount_reversed``

    ``db.create_all()`` adds these on a fresh database; this helper adds them to
    an existing one, exactly like ``_ensure_discount_columns`` does for the
    header columns.  It is idempotent (PRAGMA check first) so it is safe to run
    on every boot.
    """
    tables = {
        'direct_sale_item': [
            ('discount', 'REAL DEFAULT 0'),
            # Left NULL on purpose for rows that already exist: NULL means
            # "posted before per-line storage", and the report then derives the
            # share with the same largest-remainder rule.  New rows always set
            # ``manual``/``allocated``/``none`` explicitly.
            ('discount_kind', 'VARCHAR(20)'),
            ('discount_reason', 'VARCHAR(200)'),
        ],
        'booking_item': [
            ('discount', 'REAL DEFAULT 0'),
            ('discount_reason', 'VARCHAR(200)'),
        ],
        'material_return_item': [
            ('discount_reversed', 'REAL DEFAULT 0'),
        ],
    }
    try:
        for table, cols in tables.items():
            rows = db.session.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
            existing = {r[1] for r in rows}
            if not existing:
                # Table does not exist yet in this database; create_all owns it.
                continue
            for col, col_type in cols:
                if col not in existing:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_line_discounts():
    """Give legacy document lines their stored discount, once.

    Rows posted before per-line discount storage have a NULL ``discount_kind``.
    The profit report can derive those shares on the fly, but deriving on every
    page load is exactly what caused the original defect.  This fills the
    column once, using the same largest-remainder rule, so from then on every
    page reads a stored amount.

    Safe to run on every boot: it only selects rows whose ``discount_kind`` is
    still NULL, and a clean database has none.
    """
    from app.services import pricing
    try:
        from app.services.lookups import is_service_name
    except Exception:
        def is_service_name(_name):
            return False

    try:
        legacy_ids = [
            row[0]
            for row in db.session.execute(text(
                "SELECT id FROM direct_sale_item WHERE discount_kind IS NULL LIMIT 5000"
            )).fetchall()
        ]
    except Exception:
        db.session.rollback()
        return
    if not legacy_ids:
        return

    try:
        sale_ids = [
            row[0]
            for row in db.session.execute(text(
                "SELECT DISTINCT sale_id FROM direct_sale_item "
                "WHERE discount_kind IS NULL AND sale_id IS NOT NULL"
            )).fetchall()
        ]
        for sale_id in sale_ids:
            header = db.session.execute(text(
                "SELECT discount FROM direct_sale WHERE id = :sid"
            ), {'sid': sale_id}).fetchone()
            bill = pricing.round_money(header[0] if header else 0)
            lines = db.session.execute(text(
                "SELECT id, product_name, qty, price_at_time FROM direct_sale_item "
                "WHERE sale_id = :sid ORDER BY id ASC"
            ), {'sid': sale_id}).fetchall()
            rows = [
                {
                    'name': (line[1] or '').strip(),
                    'qty': float(line[2] or 0),
                    'rate': float(line[3] or 0),
                    'gross': pricing.cents_to_float(
                        pricing.line_gross_cents(line[2] or 0, line[3] or 0)
                    ),
                    # Same rule as the report: services (rent) keep their full
                    # value, so the backfill cannot move money onto them.
                    'is_service': bool(line[1] and is_service_name(line[1])),
                }
                for line in lines
            ]
            if bill <= 0 or not rows:
                db.session.execute(text(
                    "UPDATE direct_sale_item SET discount = 0, discount_kind = 'none' "
                    "WHERE sale_id = :sid AND discount_kind IS NULL"
                ), {'sid': sale_id})
                continue
            shares = pricing.cap_shares(pricing.allocate_document_discount(bill, rows), rows)
            for line, share in zip(lines, shares):
                db.session.execute(text(
                    "UPDATE direct_sale_item SET discount = :amount, discount_kind = :kind "
                    "WHERE id = :iid"
                ), {
                    'amount': float(share),
                    'kind': 'allocated' if share > 0 else 'none',
                    'iid': line[0],
                })
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_bill_counter_namespace_defaults():
    """Backfill namespace for legacy bill_counter rows after schema upgrades."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('bill_counter')")).fetchall()
        existing = {r[1] for r in rows}
        if 'namespace' not in existing:
            return
        db.session.execute(text(
            "UPDATE bill_counter SET namespace = 'GEN' "
            "WHERE namespace IS NULL OR TRIM(namespace) = ''"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_waive_off_table():
    """Ensure dedicated waive_off table exists for loss/write-off events."""
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS waive_off (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER,
                client_code VARCHAR(50),
                client_name VARCHAR(100),
                bill_no VARCHAR(50),
                amount REAL DEFAULT 0,
                reason VARCHAR(300),
                date_posted DATETIME,
                created_by VARCHAR(80),
                note VARCHAR(500),
                is_void INTEGER DEFAULT 0
            )
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_delivery_person_payments_table():
    try:
        db.session.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_person_payment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_person_id INTEGER NOT NULL,
                sale_id INTEGER,
                allocation_id INTEGER,
                amount_paid REAL DEFAULT 0,
                waive_off_amount REAL DEFAULT 0,
                note VARCHAR(500),
                date_posted DATETIME,
                created_by VARCHAR(80),
                is_void INTEGER DEFAULT 0
            )
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_person ON delivery_person_payment (delivery_person_id)
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_alloc ON delivery_person_payment (allocation_id)
        """))
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_dpp_date ON delivery_person_payment (date_posted)
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()
    _ensure_delivery_person_payment_accounting_columns()


def _ensure_delivery_person_payment_accounting_columns():
    """Additive upgrade linking driver payments to the authoritative ledger.

    Existing rows are preserved untouched: the new columns are nullable, and the
    minor-unit mirrors are backfilled from the legacy REAL values.  A partial
    unique index on ``idempotency_key`` makes retried submissions race-safe at
    the database level while leaving legacy NULL keys exempt.
    """
    additive = {
        'amount_paid_minor': 'BIGINT',
        'waive_off_minor': 'BIGINT',
        'payment_account_id': 'INTEGER',
        'method': 'VARCHAR(50)',
        'reference': 'VARCHAR(50)',
        'idempotency_key': 'VARCHAR(64)',
        'revision': 'INTEGER DEFAULT 1',
        'updated_by': 'VARCHAR(80)',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    }
    try:
        rows = db.session.execute(text("PRAGMA table_info('delivery_person_payment')")).fetchall()
        existing = {r[1] for r in rows}
        if not existing:
            return
        for column, sqltype in additive.items():
            if column in existing:
                continue
            try:
                db.session.execute(text(
                    f"ALTER TABLE delivery_person_payment ADD COLUMN {column} {sqltype}"
                ))
            except Exception:
                db.session.rollback()
        db.session.execute(text(
            "UPDATE delivery_person_payment "
            "SET amount_paid_minor = CAST(ROUND(COALESCE(amount_paid, 0) * 100) AS INTEGER) "
            "WHERE amount_paid_minor IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment "
            "SET waive_off_minor = CAST(ROUND(COALESCE(waive_off_amount, 0) * 100) AS INTEGER) "
            "WHERE waive_off_minor IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment SET revision = 1 WHERE revision IS NULL"
        ))
        db.session.execute(text(
            "UPDATE delivery_person_payment SET created_at = date_posted WHERE created_at IS NULL"
        ))
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_person_payment_idempotency_key "
            "ON delivery_person_payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_dpp_account "
            "ON delivery_person_payment (payment_account_id)"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_legacy_payment_discounts_to_waive_off():
    """
    Backfill legacy Payment.discount values into waive_off rows.
    Keep Payment.discount for compatibility; downstream logic avoids double counting.
    """
    try:
        existing_payment_ids = {
            r[0] for r in WaiveOff.query.filter(
                WaiveOff.payment_id.isnot(None),
                WaiveOff.is_void == False
            ).with_entities(WaiveOff.payment_id).distinct().all()
            if r and r[0] is not None
        }
        legacy_rows = Payment.query.filter(
            Payment.is_void == False,
            Payment.discount > 0
        ).all()

        for pay in legacy_rows:
            if pay.id in existing_payment_ids:
                continue
            client_obj = get_client_by_input(pay.client_name or '')
            bill_ref = (pay.manual_bill_no or pay.auto_bill_no or f"PAY-{pay.id}")
            db.session.add(WaiveOff(
                payment_id=pay.id,
                client_code=(client_obj.code if client_obj else None),
                client_name=(client_obj.name if client_obj else pay.client_name),
                bill_no=bill_ref,
                amount=float(pay.discount or 0),
                reason=(pay.discount_reason or 'Legacy waive-off migration'),
                date_posted=pay.date_posted or pk_now(),
                created_by=None,
                note=pay.note,
                is_void=bool(pay.is_void)
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _backfill_sale_delivery_persons_from_legacy():
    """Backfill legacy delivery rent rows into sale_delivery_persons for compatibility."""
    try:
        existing_sale_ids = {
            r[0] for r in db.session.query(SaleDeliveryPerson.sale_id).distinct().all()
            if r and r[0] is not None
        }
        legacy_rows = DeliveryRent.query.all()
        for dr in legacy_rows:
            if not dr.sale_id or dr.sale_id in existing_sale_ids:
                continue
            dp = get_or_create_delivery_person(dr.delivery_person_name)
            if not dp:
                continue
            db.session.add(SaleDeliveryPerson(
                sale_id=dr.sale_id,
                delivery_person_id=dp.id,
                bags_delivered=0,
                rent_amount=float(dr.amount or 0),
                created_at=dr.date_posted or pk_now(),
                is_void=bool(dr.is_void)
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_user_permission_defaults():
    """Backfill NULL permission values so newly added columns remain usable."""
    try:
        rows = db.session.execute(text("PRAGMA table_info('user')")).fetchall()
        existing = {r[1] for r in rows}
        for col, default_value in USER_PERMISSION_DEFAULTS.items():
            if col in existing:
                db.session.execute(
                    text(f'UPDATE "user" SET {col} = :v WHERE {col} IS NULL'),
                    {'v': 1 if default_value else 0}
                )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _accounting_backfill_needed() -> bool:
    """True when any row still lacks its exact-money/source metadata.

    The repair below loads ``payment``, ``supplier_payment``,
    ``account_transaction`` and ``account`` in full on every boot, which cost
    ~1 s of every worker start on a shop-sized database for nothing once the
    backfill had run.  One indexed probe per table makes the steady state free.
    """
    probes = (
        "SELECT 1 FROM payment WHERE amount_minor IS NULL OR discount_minor IS NULL"
        " OR client_id IS NULL OR payment_type IS NULL OR revision IS NULL LIMIT 1",
        "SELECT 1 FROM supplier_payment WHERE amount_minor IS NULL"
        " OR payment_type IS NULL OR revision IS NULL LIMIT 1",
        "SELECT 1 FROM account_transaction WHERE amount_minor IS NULL LIMIT 1",
        "SELECT 1 FROM account WHERE opening_balance IS NULL"
        " OR opening_balance_minor IS NULL OR balance_minor IS NULL OR revision IS NULL LIMIT 1",
    )
    try:
        for probe in probes:
            if db.session.execute(text(probe)).first() is not None:
                return True
    except Exception:
        # A missing table/column on an old database means there IS work to do.
        db.session.rollback()
        return True
    return False


def _backfill_accounting_integrity_columns():
    """Non-destructively initialise exact-money and source/audit metadata.

    Existing REAL values and historical names are preserved.  The explicit
    account opening baseline is inferred as ``stored current - active ledger
    net`` so enabling reproducible ledgers does not change any live balance.
    """
    from utils.money import from_minor, to_minor

    if not _accounting_backfill_needed():
        return

    try:
        # Versioned ORM rows require a non-NULL committed version before they
        # can be safely loaded and updated. Initialise it with raw SQL first so
        # legacy NULL rows do not trigger an autoflush/version predicate error.
        for table_name in ('account', 'payment', 'supplier_payment'):
            db.session.execute(text(
                f"UPDATE {table_name} SET revision = 1 WHERE revision IS NULL"
            ))
        db.session.commit()
        db.session.expire_all()

        # Stable payment party/source identities and exact minor-unit mirrors.
        clients_by_name = {
            (c.name or '').strip().lower(): c.id
            for c in Client.query.all() if (c.name or '').strip()
        }
        for p in Payment.query.all():
            if getattr(p, 'amount_minor', None) is None:
                p.amount_minor = to_minor(p.amount or 0)
            if getattr(p, 'discount_minor', None) is None:
                p.discount_minor = to_minor(p.discount or 0)
            if not getattr(p, 'client_id', None):
                p.client_id = clients_by_name.get((p.client_name or '').strip().lower())
            note = p.note or ''
            material_match = re.search(r'\[MATERIAL_RETURN:(\d+)\]', note, re.IGNORECASE)
            if material_match:
                p.payment_type = 'Material Return'
                p.source_type = p.source_type or 'MaterialReturn'
                p.source_id = p.source_id or int(material_match.group(1))
            elif float(p.amount or 0) < 0 or (p.method or '').strip().lower() == 'refund':
                p.payment_type = 'Refund'
            elif float(p.amount or 0) == 0 and float(p.discount or 0) > 0:
                p.payment_type = 'Waive-Off'
            else:
                p.payment_type = p.payment_type or 'Receipt'
            p.revision = p.revision or 1

        for p in SupplierPayment.query.all():
            if getattr(p, 'amount_minor', None) is None:
                p.amount_minor = to_minor(p.amount or 0)
            marker = re.search(r'\[AUTO_GRN_PAY:(\d+)\]', p.note or '', re.IGNORECASE)
            if marker:
                p.source_type = p.source_type or 'GRN'
                p.source_id = p.source_id or int(marker.group(1))
            p.payment_type = p.payment_type or 'Payment'
            p.revision = p.revision or 1

        # Add structured source identity to linked ledger rows while preserving
        # the human-readable legacy marker in ``note``.
        source_patterns = (
            ('Payment', r'\[SRC:Payment:(\d+)\]'),
            ('Payment', r'\[SRC:ClientRefund:(\d+)\]'),
            ('SupplierPayment', r'\[SRC:SupplierPayment:(\d+)\]'),
        )
        for tx in AccountTransaction.query.all():
            if getattr(tx, 'amount_minor', None) is None:
                tx.amount_minor = to_minor(tx.amount or 0)
            if not getattr(tx, 'source_type', None):
                for source_type, pattern in source_patterns:
                    match = re.search(pattern, tx.note or '', re.IGNORECASE)
                    if match:
                        tx.source_type = source_type
                        tx.source_id = int(match.group(1))
                        break

        # Infer a no-change opening baseline for every legacy account.
        for account in Account.query.all():
            account.balance_minor = to_minor(account.balance or 0)
            account.revision = account.revision or 1
            if account.opening_balance is None:
                incoming = sum(
                    (tx.amount_minor if tx.amount_minor is not None else to_minor(tx.amount or 0))
                    for tx in account.incoming_transactions if not tx.is_void
                )
                outgoing = sum(
                    (tx.amount_minor if tx.amount_minor is not None else to_minor(tx.amount or 0))
                    for tx in account.outgoing_transactions if not tx.is_void
                )
                opening_minor = int(account.balance_minor or 0) - incoming + outgoing
                account.opening_balance_minor = opening_minor
                account.opening_balance = float(from_minor(opening_minor))
                account.opening_balance_date = account.created_at
            elif account.opening_balance_minor is None:
                account.opening_balance_minor = to_minor(account.opening_balance or 0)

        # SQLite ALTER TABLE cannot add a UNIQUE constraint.  Partial unique
        # indexes make retried create requests race-safe while allowing NULLs.
        db.session.flush()
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_idempotency_key "
            "ON payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_payment_idempotency_key "
            "ON supplier_payment(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('Accounting integrity metadata backfill failed')


def _ensure_recon_counter_classification():
    """Keep the daily-reconciliation counter accounts out of the cash/bank flows.

    They are ordinary ``Account`` rows (that is what makes the paired posting a
    real ledger movement), but their ``category`` must stay ``recon`` — every
    money-account filter in the app is ``category in ('cash','bank')``, so the
    value is what keeps them off the daily board and out of every payment picker.
    A legacy ``account_type``/``type`` sync or an import could rewrite it.
    """
    try:
        db.session.execute(text(
            "UPDATE account SET category = 'recon', class_subcategory = 'Reconciliation Clearing' "
            "WHERE LOWER(TRIM(COALESCE(account_type, ''))) = 'recon_counter' "
            "OR LOWER(TRIM(COALESCE(type, ''))) = 'recon_counter'"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_account_type_compat():
    """Kept for compatibility; also re-pins the reconciliation counter category.

    Keep legacy `account.type` and newer `account.account_type` consistent.
    Some existing DBs enforce NOT NULL on `account.type`.
    """
    try:
        rows = db.session.execute(text("PRAGMA table_info('account')")).fetchall()
        existing = {r[1] for r in rows}
        if 'type' not in existing or 'account_type' not in existing:
            return
        db.session.execute(text(
            "UPDATE account SET account_type = type "
            "WHERE account_type IS NULL OR TRIM(account_type) = ''"
        ))
        db.session.execute(text(
            "UPDATE account SET type = account_type "
            "WHERE type IS NULL OR TRIM(type) = ''"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
    _ensure_recon_counter_classification()


def _ensure_direct_sale_idempotency_index():
    """DB-level duplicate-submission guard for direct sales.

    SQLite's ALTER TABLE cannot add a UNIQUE constraint, so a partial unique
    index is created instead (NULL keys for legacy rows are exempt). The
    application-level check in ``add_direct_sale`` is the primary guard; this
    index makes duplicate commits impossible even under a double-click race.
    """
    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_direct_sale_idempotency_key "
            "ON direct_sale(idempotency_key) WHERE idempotency_key IS NOT NULL"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_auto_bill_unique_indexes():
    """DB-level uniqueness for auto-generated bill numbers.

    The atomic counter allocator in ``billing.get_next_bill_no`` prevents two
    requests from ever receiving the same number, and these partial unique
    indexes make a duplicate commit impossible even if a future code path
    regresses.  A populated (legacy/imported) database can already contain
    duplicated auto bills: in that case the index for that table is skipped
    (logged) so bootstrapping a legacy DB never fails, and the counter stays
    ahead of the highest used sequence via ``_sync_bill_counter_with_db``.
    Note: 'entry' is excluded because multi-item GRNs legitimately create
    multiple Entry rows sharing the same GRN auto_bill_no.

    DDL policy (R9): create-if-missing; a rebuild happens only for the legacy
    NULL-only predicate (an index whose stored SQL lacks TRIM). No index is
    dropped merely to recreate an identical one on every boot.
    """
    tables = ("booking", "payment", "supplier_payment", "direct_sale",
              "material_return", "grn")
    for table in tables:
        try:
            dup = db.session.execute(text(
                f"SELECT COUNT(*) FROM (SELECT auto_bill_no FROM {table} "
                "WHERE auto_bill_no IS NOT NULL AND TRIM(auto_bill_no) <> '' "
                "GROUP BY auto_bill_no HAVING COUNT(*) > 1)"
            )).scalar()
            if dup:
                logging.getLogger(__name__).warning(
                    "Skipping unique auto-bill index for %s: %s duplicated bill numbers exist.",
                    table, dup,
                )
                continue
            # Idempotent (R9): create-if-missing, and only rebuild once if the
            # stored index still carries the legacy NULL-only predicate. No
            # DROP/recreate on every boot — on network/NFS filesystems this
            # removed and re-created six indexes per reload and churned the
            # data directory for zero benefit.
            existing_sql = db.session.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=:idx"
            ), {"idx": f"uq_{table}_auto_bill_no"}).scalar()
            if existing_sql and "TRIM" in existing_sql.upper():
                continue
            if existing_sql:
                db.session.execute(text(f"DROP INDEX IF EXISTS uq_{table}_auto_bill_no"))
            db.session.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_auto_bill_no "
                f"ON {table}(auto_bill_no) WHERE auto_bill_no IS NOT NULL "
                f"AND TRIM(auto_bill_no) <> ''"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.getLogger(__name__).warning(
                "Could not create unique auto-bill index for %s", table,
            )


def _ensure_audit_log_retention():
    """Opt-in retention for the ``audit_log`` table (AUDIT-115 / R15).

    The table grows once per mutating HTTP request.  On PythonAnywhere's free
    512 MB disk that eventually matters, so rows older than
    ``AMS_AUDIT_RETENTION_DAYS`` can be pruned — but ONLY when the operator
    explicitly sets it (default 0 = keep everything, current behaviour).
    Runs at boot; no cron/scheduler required.
    """
    try:
        days = int(os.environ.get('AMS_AUDIT_RETENTION_DAYS') or '0')
    except ValueError:
        days = 0
    if days <= 0:
        return
    try:
        from app.services.time_money import pk_now
        cutoff = pk_now() - timedelta(days=days)
        result = db.session.execute(text("DELETE FROM audit_log WHERE timestamp < :cutoff"), {"cutoff": cutoff})
        deleted = result.rowcount if hasattr(result, 'rowcount') else 0
        db.session.commit()
        if deleted:
            logging.getLogger('app').warning('AMS_AUDIT_RETENTION_DAYS=%d: pruned %d audit_log row(s) older than %s', days, deleted, cutoff)
    except Exception:
        db.session.rollback()


def _ensure_password_plain_purged():
    """One-time purge of legacy cleartext credentials (AUDIT-001 / R4).

    Legacy rows may still carry ``user.password_plain``.  Cleartext
    credentials must not survive inside backups or snapshots, so the column
    is NULLed — Werkzeug hashes (``password_hash``) remain authoritative and
    the login-upgrade path keeps working unchanged.

    Safety: this modifies live rows, so it is **dry-run by default**.  Set
    ``AMS_PURGE_PASSWORD_PLAIN=1`` in the environment to actually NULL the
    column (recommended once the pre-purge backup exists -- see DEPLOY_PA.md).
    ``AMS_PURGE_PASSWORD_PLAIN=0`` (or unset) only logs the row count.
    """
    try:
        rows = db.session.execute(text(
            "SELECT COUNT(*) FROM user WHERE password_plain IS NOT NULL "
            "AND TRIM(password_plain) <> ''"
        )).scalar()
        count = int(rows or 0)
        if count == 0:
            return
        enabled = (os.environ.get('AMS_PURGE_PASSWORD_PLAIN') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        if not enabled:
            logging.getLogger('app').warning(
                'AUDIT-001: %d user row(s) still carry legacy password_plain. '
                'Set AMS_PURGE_PASSWORD_PLAIN=1 (after a backup) to NULL them.', count,
            )
            return
        db.session.execute(text("UPDATE user SET password_plain = NULL WHERE password_plain IS NOT NULL"))
        db.session.commit()
        logging.getLogger('app').warning(
            'AUDIT-001: purged password_plain from %d user row(s). Hashes are authoritative; no passwords were logged.', count,
        )
    except Exception:
        db.session.rollback()


def _ensure_client_code_unique_index():
    """DB-level uniqueness for client codes.

    ``add_client`` does a check-then-insert, so two concurrent requests can
    both pass the "code already exists?" test and commit the same code.  The
    code also anchors the public ledger-QR URLs, so a duplicate breaks
    customer lookups.  A partial unique index (NULL/blank codes exempt, for
    legacy rows) makes a duplicate commit impossible, the same way
    ``_ensure_auto_bill_unique_indexes`` and the direct-sale idempotency
    index do.  A populated (legacy/imported) database can already contain
    duplicated codes: in that case the index is skipped (logged) so
    bootstrapping a legacy database never fails — the existing duplicate
    rows must be resolved by hand first; no data is modified here.
    """
    try:
        dup = db.session.execute(text(
            "SELECT COUNT(*) FROM (SELECT code FROM client "
            "WHERE code IS NOT NULL AND TRIM(code) <> '' "
            "GROUP BY code HAVING COUNT(*) > 1)"
        )).scalar()
        if dup:
            logging.getLogger(__name__).warning(
                "Skipping unique client-code index: %s duplicated client-code value(s) exist.",
                dup,
            )
            return
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_client_code "
            "ON client(code) WHERE code IS NOT NULL AND TRIM(code) <> ''"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).warning(
            "Could not create unique client-code index",
        )


def ensure_open_khata_client():
    """Materialise the shared Open-Khata walk-in client master row.

    Open-Khata sales are stored with ``client_code='OPEN-KHATA'`` and a
    free-text customer name, historically without any Client master row.
    That made the receivable invisible to the payables report/API/CSV and
    impossible to settle.  A single stable master row keyed by the reserved
    code anchors all such rows for every projection and payment path.
    """
    from app.services.constants import OPEN_KHATA_CODE, OPEN_KHATA_NAME
    if not OPEN_KHATA_CODE:
        return None
    client = Client.query.filter(
        func.lower(func.trim(Client.code)) == str(OPEN_KHATA_CODE).strip().lower()
    ).order_by(Client.id.asc()).first()
    if client:
        if not client.name or client.name == OPEN_KHATA_CODE:
            client.name = OPEN_KHATA_NAME or "Walk-in Customers (Open Khata)"
        return client
    client = Client(
        code=str(OPEN_KHATA_CODE).strip(),
        name=OPEN_KHATA_NAME or "Walk-in Customers (Open Khata)",
        category="Open Khata",
        is_active=True,
    )
    db.session.add(client)
    try:
        db.session.flush()
    except Exception:
        db.session.rollback()
        return None
    return client


# Indexes that make the hot transaction paths bounded instead of full-scan:
#   * bill-number lookups (find_bill_conflict, _max_used_auto_bill_seq, GRN
#     save, sale save, duplicate-manual-bill checks)
#   * stock movement aggregation (entry.date/material/type group-bys on the
#     dashboard, stock summary, client ledgers)
#   * pending-bill client/bill lookups
# All are additive CREATE INDEX IF NOT EXISTS — no data change, safe to add to
# a populated database, and reversible with DROP INDEX.
_PERFORMANCE_INDEXES = [
    # bill-number fast lookups (conflict detection + sequence sync)
    ("ix_grn_auto_bill_no",        "CREATE INDEX IF NOT EXISTS ix_grn_auto_bill_no        ON grn(auto_bill_no)"),
    ("ix_grn_manual_bill_no",      "CREATE INDEX IF NOT EXISTS ix_grn_manual_bill_no      ON grn(manual_bill_no)"),
    ("ix_direct_sale_manual_bill_no", "CREATE INDEX IF NOT EXISTS ix_direct_sale_manual_bill_no ON direct_sale(manual_bill_no)"),
    ("ix_direct_sale_auto_bill_no",   "CREATE INDEX IF NOT EXISTS ix_direct_sale_auto_bill_no   ON direct_sale(auto_bill_no)"),
    ("ix_booking_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_booking_manual_bill_no  ON booking(manual_bill_no)"),
    ("ix_booking_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_booking_auto_bill_no    ON booking(auto_bill_no)"),
    ("ix_payment_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_payment_manual_bill_no  ON payment(manual_bill_no)"),
    ("ix_payment_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_payment_auto_bill_no    ON payment(auto_bill_no)"),
    ("ix_supplier_payment_manual_bill_no", "CREATE INDEX IF NOT EXISTS ix_supplier_payment_manual_bill_no ON supplier_payment(manual_bill_no)"),
    ("ix_supplier_payment_auto_bill_no",   "CREATE INDEX IF NOT EXISTS ix_supplier_payment_auto_bill_no   ON supplier_payment(auto_bill_no)"),
    ("ix_material_return_manual_bill_no",  "CREATE INDEX IF NOT EXISTS ix_material_return_manual_bill_no  ON material_return(manual_bill_no)"),
    ("ix_material_return_auto_bill_no",    "CREATE INDEX IF NOT EXISTS ix_material_return_auto_bill_no    ON material_return(auto_bill_no)"),
    ("ix_entry_bill_no",           "CREATE INDEX IF NOT EXISTS ix_entry_bill_no           ON entry(bill_no)"),
    ("ix_entry_auto_bill_no",      "CREATE INDEX IF NOT EXISTS ix_entry_auto_bill_no      ON entry(auto_bill_no)"),
    ("ix_entry_material",          "CREATE INDEX IF NOT EXISTS ix_entry_material          ON entry(material)"),
    ("ix_entry_type",              "CREATE INDEX IF NOT EXISTS ix_entry_type              ON entry(type)"),
    ("ix_pending_bill_bill_no",    "CREATE INDEX IF NOT EXISTS ix_pending_bill_bill_no    ON pending_bill(bill_no)"),
    ("ix_pending_bill_client_code","CREATE INDEX IF NOT EXISTS ix_pending_bill_client_code ON pending_bill(client_code)"),
    # Expression indexes that match the EXACT predicate the receivables /
    # ledger / settlement queries use:
    #     lower(trim(coalesce(client_name, ''))) == ?
    # A plain index on client_name cannot be used by SQLite for that
    # predicate, so every settlement pass fell back to a full table SCAN of
    # pending_bill — the measured 608,741-object / 10,417-statement rebuild
    # (AUDIT-040).  Expression indexes are additive and cost nothing to read.
    ("ix_pending_bill_client_name_norm",
     "CREATE INDEX IF NOT EXISTS ix_pending_bill_client_name_norm "
     "ON pending_bill(lower(trim(coalesce(client_name, ''))))"),
    ("ix_direct_sale_client_name_norm",
     "CREATE INDEX IF NOT EXISTS ix_direct_sale_client_name_norm "
     "ON direct_sale(lower(trim(coalesce(client_name, ''))))"),
    ("ix_booking_client_name_norm",
     "CREATE INDEX IF NOT EXISTS ix_booking_client_name_norm "
     "ON booking(lower(trim(coalesce(client_name, ''))))"),
    ("ix_payment_client_name_norm",
     "CREATE INDEX IF NOT EXISTS ix_payment_client_name_norm "
     "ON payment(lower(trim(coalesce(client_name, ''))))"),
    # DB-level duplicate-submission guard for GRNs with a manual bill number
    # (double click / browser retry / refresh).  The app-level
    # find_bill_conflict check is the primary guard; this unique partial index
    # makes a second commit impossible even under a race.
    ("uq_grn_manual_bill_no",      "CREATE UNIQUE INDEX IF NOT EXISTS uq_grn_manual_bill_no ON grn(manual_bill_no) WHERE manual_bill_no IS NOT NULL AND TRIM(manual_bill_no) <> ''"),
    # Foreign-key lookups ("the lines of this sale", "the GRNs of this supplier",
    # "the payments that hit this account").  Same list as migration 0013, so a
    # database reaches the same schema whichever entry point runs first;
    # CREATE INDEX IF NOT EXISTS makes the overlap free.
    ("ix_booking_receive_in_account_id", "CREATE INDEX IF NOT EXISTS ix_booking_receive_in_account_id ON booking(receive_in_account_id)"),
    ("ix_booking_item_booking_id", "CREATE INDEX IF NOT EXISTS ix_booking_item_booking_id ON booking_item(booking_id)"),
    ("ix_cash_flow_entry_account_tx_id", "CREATE INDEX IF NOT EXISTS ix_cash_flow_entry_account_tx_id ON cash_flow_entry(account_tx_id)"),
    ("ix_client_transferred_to_id", "CREATE INDEX IF NOT EXISTS ix_client_transferred_to_id ON client(transferred_to_id)"),
    ("ix_delivery_item_delivery_id", "CREATE INDEX IF NOT EXISTS ix_delivery_item_delivery_id ON delivery_item(delivery_id)"),
    ("ix_direct_sale_invoice_id", "CREATE INDEX IF NOT EXISTS ix_direct_sale_invoice_id ON direct_sale(invoice_id)"),
    ("ix_direct_sale_payment_account_id", "CREATE INDEX IF NOT EXISTS ix_direct_sale_payment_account_id ON direct_sale(payment_account_id)"),
    ("ix_direct_sale_item_grn_item_id", "CREATE INDEX IF NOT EXISTS ix_direct_sale_item_grn_item_id ON direct_sale_item(grn_item_id)"),
    ("ix_direct_sale_item_sale_id", "CREATE INDEX IF NOT EXISTS ix_direct_sale_item_sale_id ON direct_sale_item(sale_id)"),
    ("ix_entry_invoice_id", "CREATE INDEX IF NOT EXISTS ix_entry_invoice_id ON entry(invoice_id)"),
    ("ix_fbm_rental_client_id", "CREATE INDEX IF NOT EXISTS ix_fbm_rental_client_id ON fbm_rental(client_id)"),
    ("ix_fbm_rental_item_id", "CREATE INDEX IF NOT EXISTS ix_fbm_rental_item_id ON fbm_rental(item_id)"),
    ("ix_fbm_rental_payment_account_id", "CREATE INDEX IF NOT EXISTS ix_fbm_rental_payment_account_id ON fbm_rental(payment_account_id)"),
    ("ix_follow_up_contact_pending_bill_id", "CREATE INDEX IF NOT EXISTS ix_follow_up_contact_pending_bill_id ON follow_up_contact(pending_bill_id)"),
    ("ix_follow_up_contact_reminder_id", "CREATE INDEX IF NOT EXISTS ix_follow_up_contact_reminder_id ON follow_up_contact(reminder_id)"),
    ("ix_follow_up_reminder_pending_bill_id", "CREATE INDEX IF NOT EXISTS ix_follow_up_reminder_pending_bill_id ON follow_up_reminder(pending_bill_id)"),
    ("ix_grn_payment_account_id", "CREATE INDEX IF NOT EXISTS ix_grn_payment_account_id ON grn(payment_account_id)"),
    ("ix_grn_supplier_id", "CREATE INDEX IF NOT EXISTS ix_grn_supplier_id ON grn(supplier_id)"),
    ("ix_grn_item_grn_id", "CREATE INDEX IF NOT EXISTS ix_grn_item_grn_id ON grn_item(grn_id)"),
    ("ix_import_job_initiated_by", "CREATE INDEX IF NOT EXISTS ix_import_job_initiated_by ON import_job(initiated_by)"),
    ("ix_import_upload_uploaded_by", "CREATE INDEX IF NOT EXISTS ix_import_upload_uploaded_by ON import_upload(uploaded_by)"),
    ("ix_payment_payment_account_id", "CREATE INDEX IF NOT EXISTS ix_payment_payment_account_id ON payment(payment_account_id)"),
    ("ix_supplier_transferred_to_id", "CREATE INDEX IF NOT EXISTS ix_supplier_transferred_to_id ON supplier(transferred_to_id)"),
    ("ix_supplier_payment_payment_account_id", "CREATE INDEX IF NOT EXISTS ix_supplier_payment_payment_account_id ON supplier_payment(payment_account_id)"),
    ("ix_stock_adjustment_reference", "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_reference ON stock_adjustment(reference)"),
    ("ix_stock_adjustment_date", "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_date ON stock_adjustment(adjustment_date)"),
    ("ix_stock_adjustment_material", "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_material ON stock_adjustment(material)"),
    ("ix_stock_adjustment_entry_id", "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_entry_id ON stock_adjustment(entry_id)"),
    ("ix_stock_adjustment_is_void", "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_is_void ON stock_adjustment(is_void)"),
]


def _ensure_daydiff_posting_index():
    """DB-level duplicate guard for daily-reconciliation counter postings.

    Each posting leg carries a deterministic ``idempotency_key``
    (``daydiff:<day>:<account>:<row>:out|in``).  The application checks it
    first; this partial unique index makes a duplicate commit impossible even if
    two operators finalise the same day at the same moment.
    """
    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_tx_daydiff_key "
            "ON account_transaction(idempotency_key) "
            "WHERE idempotency_key LIKE 'daydiff:%'"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_loan_party_two_way_schema():
    """Rebuild legacy ``loan_party`` tables to drop ``ck_loan_party_opening_nonnegative``.

    Two-way Loan Parties allow negative (party owes us), zero, and positive
    (we owe party) opening balances.  SQLite cannot ``ALTER TABLE ... DROP
    CONSTRAINT``, so existing databases created with the legacy check
    constraint are safely rebuilt in place without losing any rows.
    """
    row = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='loan_party'")
    ).fetchone()
    if not row or 'ck_loan_party_opening_nonnegative' not in (row[0] or ''):
        return

    db_file = db.engine.url.database
    db.session.remove()
    if not db_file or db_file == ':memory:':
        return

    import sqlite3
    db.engine.dispose()
    con = sqlite3.connect(str(db_file), timeout=120, isolation_level=None)
    try:
        cols_info = con.execute("PRAGMA table_info('loan_party')").fetchall()
        existing_cols = [c[1] for c in cols_info]
        base_cols = [
            'id', 'name', 'note', 'opening_principal_minor',
            'opening_principal_date', 'is_active', 'created_at', 'created_by',
        ]
        copy_cols = [c for c in base_cols if c in existing_cols]
        extra_defs = []
        for cid, cname, ctype, notnull, dflt, pk in cols_info:
            if cname in base_cols:
                continue
            col_sql = f'"{cname}" {ctype or "TEXT"}'
            if notnull:
                col_sql += ' NOT NULL'
            if dflt is not None:
                col_sql += f' DEFAULT {dflt}'
            extra_defs.append(col_sql)
            copy_cols.append(cname)
        cols_csv = ', '.join(f'"{c}"' for c in copy_cols)
        extra_sql = (',\n                    ' + ',\n                    '.join(extra_defs)) if extra_defs else ''

        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DROP TABLE IF EXISTS loan_party__two_way")
            con.execute(
                f"""
                CREATE TABLE loan_party__two_way (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(160) NOT NULL,
                    note VARCHAR(500),
                    opening_principal_minor BIGINT NOT NULL,
                    opening_principal_date DATE,
                    is_active BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    created_by VARCHAR(80){extra_sql}
                )
                """
            )
            con.execute(
                f"INSERT INTO loan_party__two_way ({cols_csv}) SELECT {cols_csv} FROM loan_party"
            )
            con.execute("DROP TABLE loan_party")
            con.execute("ALTER TABLE loan_party__two_way RENAME TO loan_party")
            con.execute("CREATE INDEX IF NOT EXISTS ix_loan_party_name ON loan_party (name)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_loan_party_is_active ON loan_party (is_active)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_loan_party_created_at ON loan_party (created_at)")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            try:
                con.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
        fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key check failed after loan_party rebuild: {fk_errors[:5]}")
    finally:
        con.close()


def _ensure_performance_indexes():
    """Add the hot-path query indexes (idempotent, additive, reversible)."""
    try:
        for _name, ddl in _PERFORMANCE_INDEXES:
            db.session.execute(text(ddl))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_account_classification_columns():
    """Additive Account Create/Edit classification upgrade.

    Adds the controlled-hierarchy columns (Category/Subcategory/Account Type +
    Channel + channel-specific details + linked entity + status) and the
    adjustment traceability columns (reason + idempotency_key) on
    AccountTransaction.  All new columns are nullable so existing rows and the
    legacy ``category`` / ``source_category`` / ``account_type`` columns stay
    valid.  Existing accounts are backfilled with a confident legacy→new
    mapping; unmappable rows fall back to a valid generic classification.

    Nothing here changes a balance or deletes/renames a column.
    """
    from blueprints.accounts import classification as cls

    try:
        rows = db.session.execute(text("PRAGMA table_info('account')")).fetchall()
        existing = {r[1] for r in rows}
        if existing:
            additive = {
                "class_category": "VARCHAR(50)",
                "class_subcategory": "VARCHAR(80)",
                "class_account_type": "VARCHAR(100)",
                "channel": "VARCHAR(30)",
                "cash_location": "VARCHAR(120)",
                "cash_responsible": "VARCHAR(120)",
                "wallet_provider": "VARCHAR(100)",
                "wallet_number": "VARCHAR(80)",
                "wallet_holder": "VARCHAR(120)",
                "linked_entity_type": "VARCHAR(30)",
                "linked_client_id": "INTEGER",
                "linked_supplier_id": "INTEGER",
                "linked_party_name": "VARCHAR(160)",
                "account_status": "VARCHAR(20) DEFAULT 'active'",
            }
            for column, sqltype in additive.items():
                if column in existing:
                    continue
                try:
                    db.session.execute(text(
                        f"ALTER TABLE account ADD COLUMN {column} {sqltype}"
                    ))
                except Exception:
                    db.session.rollback()
            db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        rows = db.session.execute(text("PRAGMA table_info('account_transaction')")).fetchall()
        existing = {r[1] for r in rows}
        if existing:
            for column, sqltype in (("reason", "VARCHAR(300)",), ("idempotency_key", "VARCHAR(64)",)):
                if column in existing:
                    continue
                try:
                    db.session.execute(text(
                        f"ALTER TABLE account_transaction ADD COLUMN {column} {sqltype}"
                    ))
                except Exception:
                    db.session.rollback()
            # Partial unique index makes retried/double-clicked adjustments
            # race-safe while leaving legacy NULL keys exempt.
            db.session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_transaction_idempotency_key "
                "ON account_transaction(idempotency_key) WHERE idempotency_key IS NOT NULL"
            ))
            db.session.commit()
    except Exception:
        db.session.rollback()

    # Backfill new classification for any existing account that has none.  The
    # original columns are preserved untouched; only the empty new columns are
    # filled with a confident mapping (or a valid fallback).
    try:
        unmapped = Account.query.filter(
            or_(Account.class_category.is_(None), func.trim(Account.class_category) == "")
        ).all()
        for account in unmapped:
            cat, sub, atype = cls.legacy_to_classification(
                account.source_category, account.account_type, account.category
            )
            node = cls.resolve_node(cat, sub, atype) or cls.resolve_node(*cls.LEGACY_FALLBACK)
            account.class_category = cat
            account.class_subcategory = sub
            account.class_account_type = atype
            account.channel = node["default_channel"]
            if not account.account_status:
                account.account_status = "active" if account.is_active is not False else "inactive"
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception('Account classification backfill failed')


def _ensure_default_admin():
    """Create a first admin if the user table is empty (fresh / empty DB)."""
    if User.query.count() > 0:
        return
    username = (os.environ.get("DEFAULT_ADMIN_USER") or "Admin").strip() or "Admin"
    password = (os.environ.get("DEFAULT_ADMIN_PASSWORD") or "Admin@fbm12345").strip() or "Admin@fbm12345"
    user = User(
        username=username,
        role="admin",
        status="active",
        password_hash=generate_password_hash(password),
        password_plain=None,
        can_import_export=True,
        can_manage_directory=True,
        can_manage_clients=True,
        can_manage_suppliers=True,
        can_manage_materials=True,
        can_manage_delivery_persons=True,
        can_access_settings=True,
    )
    db.session.add(user)
    db.session.commit()
    logging.getLogger("app").info("Created default admin user %r", username)


def _release_stale_system_locks():
    """A previous crash must not leave wipe/import mutexes held forever."""
    try:
        db.session.execute(text(
            "UPDATE system_lock SET status='unlocked', owner=NULL, "
            "acquired_at=NULL, note='Released on startup' "
            "WHERE status='locked'"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()



def _bootstrap_database():
    db.create_all()
    try:
        _release_stale_system_locks()
    except Exception:
        db.session.rollback()
    try:
        _ensure_user_password_column()
    except Exception:
        pass
    try:
        _ensure_model_columns()
    except Exception:
        pass
    try:
        _ensure_loan_party_two_way_schema()
    except Exception:
        logging.getLogger('app').exception('Failed to upgrade loan_party schema for two-way balances')
        raise
    try:
        _ensure_default_admin()
    except Exception:
        # A populated/legacy database may intentionally manage users elsewhere;
        # schema bootstrap must remain non-destructive in that case.
        db.session.rollback()
    try:
        _ensure_account_type_compat()
    except Exception:
        pass
    try:
        _backfill_accounting_integrity_columns()
    except Exception:
        pass
    try:
        # Seed the user-managed business vocabulary (Item Definitions).  The
        # classification table is seeded before the account-classification
        # backfill so the table is the authoritative registry at boot.
        from app.services.item_definitions import ensure_item_definitions
        ensure_item_definitions()
    except Exception:
        logging.getLogger('app').exception('Item definition seeding failed')
    try:
        _ensure_account_classification_columns()
    except Exception:
        pass
    try:
        _ensure_material_categories()
    except Exception:
        pass
    try:
        _ensure_discount_columns()
    except Exception:
        pass
    try:
        _ensure_line_discount_columns()
    except Exception:
        pass
    try:
        _ensure_document_archive_columns()
    except Exception:
        pass
    try:
        _backfill_line_discounts()
    except Exception:
        pass
    try:
        _ensure_bill_counter_namespace_defaults()
    except Exception:
        pass
    try:
        _ensure_waive_off_table()
    except Exception:
        pass
    try:
        _ensure_delivery_person_payments_table()
    except Exception:
        pass
    try:
        _backfill_legacy_payment_discounts_to_waive_off()
    except Exception:
        pass
    try:
        _backfill_sale_delivery_persons_from_legacy()
    except Exception:
        pass
    try:
        _ensure_user_permission_defaults()
    except Exception:
        pass
    try:
        _ensure_direct_sale_idempotency_index()
    except Exception:
        pass
    try:
        _ensure_auto_bill_unique_indexes()
    except Exception:
        pass
    try:
        _ensure_client_code_unique_index()
    except Exception:
        pass
    try:
        _ensure_password_plain_purged()
    except Exception:
        pass
    try:
        _ensure_audit_log_retention()
    except Exception:
        pass
    try:
        ensure_open_khata_client()
    except Exception:
        pass
    try:
        _ensure_performance_indexes()
        _ensure_daydiff_posting_index()
    except Exception:
        pass
    try:
        for sale in DirectSale.query.filter(
            or_(DirectSale.client_code.is_(None), func.trim(DirectSale.client_code) == '')
        ).all():
            cli = get_client_by_input(sale.client_name or '')
            if cli:
                sale.client_code = cli.code
        db.session.commit()
    except Exception:
        db.session.rollback()
    # bootstrap_tenancy() removed 2026-11-16: the function was never defined,
    # so this call always raised NameError and was swallowed here. No behavior
    # change. (Audit finding #7.)
    try:
        # Deletes are immediate (2026-11-16): rows never linger for a restart
        # to purge.  Rather than delete at boot — which dropped ≥ DB_HEALTH_DROP_MIN
        # rows at once and tripped the health guard on every restart after a big
        # cleanup — legacy voided rows inherited from older builds are left
        # inert (filtered from reports) and reported once, never purged here.
        from app.services.void_purge import count_voided_rows
        counts = count_voided_rows(db_path)
        legacy_total = sum(
            int(n or 0)
            for v in (counts or {}).values() if isinstance(v, dict)
            for n in v.values()
        )
        if legacy_total:
            logging.getLogger('app').warning(
                'Found %s legacy voided/cancelled rows from older builds. They stay '
                'inert (hidden from reports). To remove them, take a backup and run '
                'the offline void purge tool.', legacy_total)
    except Exception:
        pass
    try:
        logging.getLogger('app').info('DB loaded: %s | counts=%s', db_path, _db_debug_counts())
    except Exception:
        pass


