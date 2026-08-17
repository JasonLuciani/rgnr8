/**
 * DDL for the persistent chart of accounts.
 *
 * The COA was the last in-memory-only piece of the core: accounts were passed in
 * as a `ChartOfAccounts` built at startup, never stored. This table makes it
 * durable and per-tenant, mirroring the ledger tables' conventions (text ids,
 * tenant-scoped primary key, RLS as a second isolation layer). Subtype and
 * parent are nullable so an account can be a simple leaf or part of a hierarchy;
 * `active` supports soft-deactivation without losing history.
 */
export const ACCOUNT_DDL = `
CREATE TABLE IF NOT EXISTS account (
  tenant_id     text    NOT NULL,
  id            text    NOT NULL,
  code          text    NOT NULL,
  name          text    NOT NULL,
  type          text    NOT NULL,
  currency_code text    NOT NULL,
  subtype       text,
  parent_id     text,
  active        boolean NOT NULL DEFAULT true,
  CONSTRAINT account_pk PRIMARY KEY (tenant_id, id),
  CONSTRAINT account_code_uq UNIQUE (tenant_id, code)
);

CREATE INDEX IF NOT EXISTS account_parent_idx ON account (tenant_id, parent_id);
`;

export const ACCOUNT_RLS_DDL = `
ALTER TABLE account ENABLE ROW LEVEL SECURITY;
ALTER TABLE account FORCE ROW LEVEL SECURITY;
CREATE POLICY account_tenant_isolation ON account
  USING (tenant_id = current_setting('app.tenant_id', true));
`;
