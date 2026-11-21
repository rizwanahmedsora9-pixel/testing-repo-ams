-- Item Definitions: the generic business-vocabulary registry.
-- Enables user-managed definitions (account classification, sale categories,
-- GRN charge types, payment methods) without code changes.
CREATE TABLE IF NOT EXISTS item_definition (
    id INTEGER PRIMARY KEY,
    group_key VARCHAR(60) NOT NULL,
    code VARCHAR(80) NOT NULL,
    name VARCHAR(120) NOT NULL,
    parent_id INTEGER REFERENCES item_definition(id),
    level VARCHAR(20),
    config JSON,
    is_active BOOLEAN DEFAULT 1 NOT NULL,
    is_system BOOLEAN DEFAULT 0 NOT NULL,
    sort_order INTEGER DEFAULT 0 NOT NULL,
    notes VARCHAR(300),
    created_at DATETIME,
    updated_at DATETIME
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_item_definition_group_level_code
    ON item_definition(group_key, level, code);
CREATE INDEX IF NOT EXISTS ix_item_definition_group_active
    ON item_definition(group_key, is_active);
CREATE INDEX IF NOT EXISTS ix_item_definition_parent_id
    ON item_definition(parent_id);
