/**
 * DDL for the durable subledger master-data tables.
 *
 * Customers, vendors, and items become persistent, per-tenant rows (mirroring
 * the ledger tables' conventions: text ids, tenant-scoped primary key, RLS as a
 * second isolation layer). Money is stored as NUMERIC(38,0) minor units plus a
 * currency code — never a float. Addresses and other nested structures are
 * JSONB. This is the durable counterpart to the in-memory `MasterDataStore`.
 */
export const MASTER_DATA_DDL = `
CREATE TABLE IF NOT EXISTS md_customer (
  tenant_id   text NOT NULL,
  id          text NOT NULL,
  name        text NOT NULL,
  email       text,
  phone       text,
  billing_address jsonb,
  terms_days  int,
  tax_exempt  boolean,
  notes       text,
  active      boolean NOT NULL DEFAULT true,
  CONSTRAINT md_customer_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS md_vendor (
  tenant_id   text NOT NULL,
  id          text NOT NULL,
  name        text NOT NULL,
  email       text,
  phone       text,
  address     jsonb,
  terms_days  int,
  is_1099     boolean,
  tax_id      text,
  default_expense_account_id text,
  active      boolean NOT NULL DEFAULT true,
  CONSTRAINT md_vendor_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS md_item (
  tenant_id   text NOT NULL,
  id          text NOT NULL,
  name        text NOT NULL,
  sku         text,
  type        text NOT NULL,
  unit_price_minor numeric(38,0),
  unit_price_currency text,
  income_account_id  text,
  expense_account_id text,
  asset_account_id   text,
  taxable     boolean,
  active      boolean NOT NULL DEFAULT true,
  CONSTRAINT md_item_pk PRIMARY KEY (tenant_id, id)
);
`;

export const MASTER_DATA_RLS_DDL = `
ALTER TABLE md_customer ENABLE ROW LEVEL SECURITY;
ALTER TABLE md_customer FORCE ROW LEVEL SECURITY;
CREATE POLICY md_customer_tenant ON md_customer USING (tenant_id = current_setting('app.tenant_id', true));
ALTER TABLE md_vendor ENABLE ROW LEVEL SECURITY;
ALTER TABLE md_vendor FORCE ROW LEVEL SECURITY;
CREATE POLICY md_vendor_tenant ON md_vendor USING (tenant_id = current_setting('app.tenant_id', true));
ALTER TABLE md_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE md_item FORCE ROW LEVEL SECURITY;
CREATE POLICY md_item_tenant ON md_item USING (tenant_id = current_setting('app.tenant_id', true));
`;
