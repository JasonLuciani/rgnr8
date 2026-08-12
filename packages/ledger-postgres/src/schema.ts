/**
 * DDL for the PostgreSQL ledger store.
 *
 * Design notes:
 * - Money is stored as NUMERIC(38,0) minor units — never floating point.
 * - A per-tenant counter row (`ledger_tenant_seq`) makes sequence assignment
 *   atomic and gap-free: appends for a tenant serialize on that row.
 * - Idempotency is a UNIQUE (tenant_id, idempotency_key) constraint.
 * - Posted rows are append-only; there are no UPDATE/DELETE paths in the adapter.
 */
export const CORE_DDL = `
CREATE TABLE IF NOT EXISTS ledger_tenant_seq (
  tenant_id  text PRIMARY KEY,
  last_seq   bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_entry (
  tenant_id                 text   NOT NULL,
  sequence                  bigint NOT NULL,
  id                        text   NOT NULL,
  idempotency_key           text   NOT NULL,
  period_key                text   NOT NULL,
  currency_code             text   NOT NULL,
  entry_date                text   NOT NULL,
  status                    text   NOT NULL,
  memo                      text,
  reversal_of               text,
  posted_at                 text   NOT NULL,
  prov_source_system        text   NOT NULL,
  prov_source_object        text   NOT NULL,
  prov_source_version       text   NOT NULL,
  prov_effective_date       text   NOT NULL,
  prov_posted_date          text   NOT NULL,
  prov_ingested_at          text   NOT NULL,
  prov_normalization_version text  NOT NULL,
  prov_mapping_version      text   NOT NULL,
  CONSTRAINT journal_entry_pk PRIMARY KEY (tenant_id, sequence),
  CONSTRAINT journal_entry_id_uq UNIQUE (tenant_id, id),
  CONSTRAINT journal_entry_idem_uq UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS journal_line (
  tenant_id       text    NOT NULL,
  entry_sequence  bigint  NOT NULL,
  line_index      int     NOT NULL,
  account_id      text    NOT NULL,
  side            text    NOT NULL,
  amount_minor    numeric(38,0) NOT NULL,
  currency_code   text    NOT NULL,
  memo            text,
  dimensions      jsonb,
  CONSTRAINT journal_line_pk PRIMARY KEY (tenant_id, entry_sequence, line_index),
  CONSTRAINT journal_line_amount_positive CHECK (amount_minor > 0),
  CONSTRAINT journal_line_entry_fk
    FOREIGN KEY (tenant_id, entry_sequence)
    REFERENCES journal_entry (tenant_id, sequence)
);

CREATE INDEX IF NOT EXISTS journal_line_account_idx
  ON journal_line (tenant_id, account_id);

-- Durable month-end period locks. A row's absence means OPEN; the posting engine
-- refuses writes into a period whose row has status = 'LOCKED'. Because this is
-- persisted, a sealed period stays sealed across a restart — it never silently
-- re-opens the way an in-memory lock would.
CREATE TABLE IF NOT EXISTS ledger_period (
  tenant_id  text NOT NULL,
  period     text NOT NULL,
  status     text NOT NULL,
  locked_at  text,
  CONSTRAINT ledger_period_pk PRIMARY KEY (tenant_id, period)
);
`;

/**
 * Row-level security: defense in depth on top of the adapter always filtering
 * by tenant_id. The application sets `SET app.tenant_id = '<tenant>'` per
 * request/transaction; policies then restrict every row to that tenant.
 *
 * Apply this in production with a role that is NOT the table owner (owners and
 * superusers bypass RLS). pg-mem does not implement RLS, so tests exercise the
 * adapter's explicit tenant filtering instead.
 */
export const RLS_DDL = `
ALTER TABLE journal_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE journal_line  ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_tenant_seq ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_period ENABLE ROW LEVEL SECURITY;

CREATE POLICY journal_entry_tenant_isolation ON journal_entry
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY journal_line_tenant_isolation ON journal_line
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY ledger_tenant_seq_isolation ON ledger_tenant_seq
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY ledger_period_tenant_isolation ON ledger_period
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
`;
