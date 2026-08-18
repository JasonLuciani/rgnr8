import {
  ChartOfAccounts,
  InMemoryLedgerStore,
  InMemoryPeriodStore,
  templateAccounts,
  type Account,
  type BusinessCategory,
  type Currency,
  type LedgerStore,
  type PeriodStore,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import { PgAccountStore, PgLedgerStore, SqlPeriodStore, type Pool } from "@rgnr8/ledger-postgres";
import {
  InMemoryDocumentStore,
  PgDocumentStore,
  type DocumentStore,
} from "./documents.js";
import { InMemoryReconStore, PgReconStore, type ReconStore } from "./reconcile.js";
import { InMemoryFeedStore, PgFeedStore, type FeedStore } from "./feed.js";
import { InMemoryPayrollStore, PgPayrollStore, type PayrollStore } from "./payroll.js";
import { InMemoryBudgetStore, PgBudgetStore, type BudgetStore } from "./reporting.js";
import {
  InMemoryDimensionStore, PgDimensionStore, type DimensionStore,
} from "./dimensions.js";
import { rlsDdl } from "./security.js";

/**
 * The storage seam the ledger service runs on.
 *
 * Everything above this interface is the real accounting core; everything below
 * is where the books physically live. Two implementations ship: an in-memory one
 * for local dev and tests, and a PostgreSQL one for production. Both are
 * strictly per-tenant — a tenant's chart, journal, and period locks are only
 * ever reachable through its own `TenantId`, which is what makes multi-client
 * hosting safe (the SQL schema adds row-level security as a second layer).
 */
export interface LedgerBackend {
  /** The tenant's chart of accounts, loaded fresh (it is durable state). */
  chart(tenant: TenantId): Promise<ChartOfAccounts>;
  /** Insert or update one account. */
  saveAccount(tenant: TenantId, account: Account): Promise<void>;
  /** Seed a chart from a business-category template; returns what was written. */
  seedChart(tenant: TenantId, category: BusinessCategory, currency: Currency): Promise<Account[]>;
  /** The append-only journal store for a tenant. */
  store(tenant: TenantId): LedgerStore;
  /** The period-lock store for a tenant. */
  periods(tenant: TenantId): PeriodStore;
  /** AR/AP source documents (invoices, bills, parties, payments). */
  documents(): DocumentStore;
  /** Bank-reconciliation cleared/reconciled status, keyed by (account, entry). */
  recon(): ReconStore;
  /** The bank-feed review inbox and its categorization rules. */
  feed(): FeedStore;
  /** Employees and payroll runs. */
  payroll(): PayrollStore;
  /** Per-period, per-account budgets. */
  budgets(): BudgetStore;
  /** Reporting dimensions (classes, locations) and their allowed values. */
  dimensions(): DimensionStore;
  /** Create/verify schema. Safe to run repeatedly. */
  migrate(): Promise<void>;
}

/** In-memory backend — local dev and tests. Nothing survives a restart. */
export class InMemoryBackend implements LedgerBackend {
  private readonly docs = new InMemoryDocumentStore();
  private readonly reconStore = new InMemoryReconStore();
  private readonly feedStore = new InMemoryFeedStore();
  private readonly payrollStore = new InMemoryPayrollStore();
  private readonly budgetStore = new InMemoryBudgetStore();
  private readonly dimensionStore = new InMemoryDimensionStore();
  private readonly accounts = new Map<string, Map<string, Account>>();
  private readonly stores = new Map<string, InMemoryLedgerStore>();
  private readonly periodStores = new Map<string, InMemoryPeriodStore>();

  private accountMap(tenant: TenantId): Map<string, Account> {
    let m = this.accounts.get(tenant);
    if (!m) {
      m = new Map();
      this.accounts.set(tenant, m);
    }
    return m;
  }

  chart(tenant: TenantId): Promise<ChartOfAccounts> {
    return Promise.resolve(new ChartOfAccounts([...this.accountMap(tenant).values()]));
  }

  saveAccount(tenant: TenantId, account: Account): Promise<void> {
    this.accountMap(tenant).set(account.id, account);
    return Promise.resolve();
  }

  seedChart(tenant: TenantId, category: BusinessCategory, currency: Currency): Promise<Account[]> {
    const seeded = templateAccounts(category, currency);
    for (const a of seeded) this.accountMap(tenant).set(a.id, a);
    return Promise.resolve(seeded);
  }

  store(tenant: TenantId): LedgerStore {
    let s = this.stores.get(tenant);
    if (!s) {
      s = new InMemoryLedgerStore();
      this.stores.set(tenant, s);
    }
    return s;
  }

  periods(tenant: TenantId): PeriodStore {
    let p = this.periodStores.get(tenant);
    if (!p) {
      p = new InMemoryPeriodStore();
      this.periodStores.set(tenant, p);
    }
    return p;
  }

  documents(): DocumentStore {
    return this.docs;
  }

  recon(): ReconStore {
    return this.reconStore;
  }

  feed(): FeedStore {
    return this.feedStore;
  }

  payroll(): PayrollStore {
    return this.payrollStore;
  }

  budgets(): BudgetStore {
    return this.budgetStore;
  }

  dimensions(): DimensionStore {
    return this.dimensionStore;
  }

  async migrate(): Promise<void> {
    await this.docs.migrate();
    await this.reconStore.migrate();
    await this.feedStore.migrate();
    await this.payrollStore.migrate();
    await this.budgetStore.migrate();
    await this.dimensionStore.migrate();
  }
}

/**
 * PostgreSQL backend — the production one. The journal, chart, and period locks
 * are all durable and tenant-scoped; `PgLedgerStore` owns atomic, gap-free
 * per-tenant sequencing so concurrent posts can't race.
 */
export class PostgresBackend implements LedgerBackend {
  private readonly ledger: PgLedgerStore;
  private readonly accounts: PgAccountStore;
  private readonly periodStore: SqlPeriodStore;
  private readonly docs: PgDocumentStore;
  private readonly reconStore: PgReconStore;
  private readonly feedStore: PgFeedStore;
  private readonly payrollStore: PgPayrollStore;
  private readonly budgetStore: PgBudgetStore;
  private readonly dimensionStore: PgDimensionStore;

  /**
   * @param pool     a connection pool.
   * @param options  `enforceRls` defaults to true and should stay true in
   *                 production. It exists because `pg-mem` — the in-process
   *                 fake the fast tests run against — cannot execute the
   *                 plpgsql the policies are created with. Turning it off is a
   *                 statement that this database is a test double, not a
   *                 decision about security.
   */
  constructor(
    private readonly pool: Pool,
    private readonly options: { readonly enforceRls?: boolean } = {},
  ) {
    this.ledger = new PgLedgerStore(pool);
    this.accounts = new PgAccountStore(pool);
    this.periodStore = new SqlPeriodStore(pool);
    this.docs = new PgDocumentStore(pool);
    this.reconStore = new PgReconStore(pool);
    this.feedStore = new PgFeedStore(pool);
    this.payrollStore = new PgPayrollStore(pool);
    this.budgetStore = new PgBudgetStore(pool);
    this.dimensionStore = new PgDimensionStore(pool);
  }

  async migrate(): Promise<void> {
    await this.ledger.migrate();
    await this.accounts.migrate();
    await this.docs.migrate();
    await this.reconStore.migrate();
    await this.feedStore.migrate();
    await this.payrollStore.migrate();
    await this.budgetStore.migrate();
    await this.dimensionStore.migrate();
    // Last, once every table exists: make the database itself enforce tenant
    // isolation, so a query that forgets its tenant filter returns nothing
    // rather than everything.
    if (this.options.enforceRls !== false) {
      await this.pool.query(rlsDdl());
    }
  }

  documents(): DocumentStore {
    return this.docs;
  }

  recon(): ReconStore {
    return this.reconStore;
  }

  feed(): FeedStore {
    return this.feedStore;
  }

  payroll(): PayrollStore {
    return this.payrollStore;
  }

  budgets(): BudgetStore {
    return this.budgetStore;
  }

  dimensions(): DimensionStore {
    return this.dimensionStore;
  }

  chart(tenant: TenantId): Promise<ChartOfAccounts> {
    return this.accounts.loadChart(tenant);
  }

  saveAccount(tenant: TenantId, account: Account): Promise<void> {
    return this.accounts.upsert(tenant, account);
  }

  seedChart(tenant: TenantId, category: BusinessCategory, currency: Currency): Promise<Account[]> {
    return this.accounts.seedFromTemplate(tenant, category, currency);
  }

  store(_tenant: TenantId): LedgerStore {
    return this.ledger; // already tenant-scoped on every query
  }

  periods(_tenant: TenantId): PeriodStore {
    return this.periodStore;
  }
}
