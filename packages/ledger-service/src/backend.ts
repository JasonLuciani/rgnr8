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
  /** Create/verify schema. Safe to run repeatedly. */
  migrate(): Promise<void>;
}

/** In-memory backend — local dev and tests. Nothing survives a restart. */
export class InMemoryBackend implements LedgerBackend {
  private readonly docs = new InMemoryDocumentStore();
  private readonly reconStore = new InMemoryReconStore();
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

  async migrate(): Promise<void> {
    await this.docs.migrate();
    await this.reconStore.migrate();
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

  constructor(pool: Pool) {
    this.ledger = new PgLedgerStore(pool);
    this.accounts = new PgAccountStore(pool);
    this.periodStore = new SqlPeriodStore(pool);
    this.docs = new PgDocumentStore(pool);
    this.reconStore = new PgReconStore(pool);
  }

  async migrate(): Promise<void> {
    await this.ledger.migrate();
    await this.accounts.migrate();
    await this.docs.migrate();
    await this.reconStore.migrate();
  }

  documents(): DocumentStore {
    return this.docs;
  }

  recon(): ReconStore {
    return this.reconStore;
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
