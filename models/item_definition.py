"""Item definitions — tables that hold user-managed business vocabulary.

A single table, bucketed by ``group_key``, holds the definitions that the
business may grow or reshape over time (expense subcategories, sale categories,
payment methods, charge types, loan / account types, …).  Each definition has:

  * ``code``      — stable machine key, unique within a group.  Code that
                     references a definition (e.g. a GRN charge) stores the
                     *code*, never the display name, so renaming is safe.
  * ``name``      — the human-facing label that plants may retitle.
  * ``parent_id`` — optional hierarchy (category → subcategory → account type).
  * ``config``    — free-form JSON carrying behavior flags (channels, entity
                     type, default channel, is_expense, resale_allowed, …).
                     (``metadata`` is reserved by the ORM, hence ``config``.)
  * ``is_system`` — locked seed rows: visible, not renamed / deleted /
                     deactivated.  Plain business vocabulary is *not* system.

The seed data for every group lives in ``app.services.item_definitions`` and is
written by an idempotent boot seeder.  Renaming/adding/deactivating rows is a
database change, never a code change — that is the whole point.
"""
from __future__ import annotations

from .__base import *  # noqa
from .helpers import *  # noqa


class ItemDefinition(db.Model):
    __tablename__ = 'item_definition'

    id = db.Column(db.Integer, primary_key=True)
    group_key = db.Column(db.String(60), nullable=False, index=True)   # e.g. 'account_classification'
    code = db.Column(db.String(80), nullable=False, index=True)        # stable machine key
    name = db.Column(db.String(120), nullable=False)                   # display label
    parent_id = db.Column(db.Integer, db.ForeignKey('item_definition.id'), nullable=True, index=True)
    level = db.Column(db.String(20), nullable=True, index=True)        # category|subcategory|account_type|item
    config = db.Column(db.JSON, nullable=True)                          # {channels: [...], entity: ..., ...}
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    is_system = db.Column(db.Boolean, default=False, nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False, index=True)
    notes = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=pk_model_now)
    updated_at = db.Column(db.DateTime, default=pk_model_now, onupdate=pk_model_now)

    __table_args__ = (
        UniqueConstraint('group_key', 'level', 'code', name='uq_item_definition_group_level_code'),
    )

    def __repr__(self):
        return f'<ItemDefinition {self.group_key}.{self.code}>'
