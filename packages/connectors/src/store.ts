import type { Connection } from "./types.js";

export interface ConnectionStore {
  get(id: string): Connection | undefined;
  put(conn: Connection): void;
  list(tenantId?: string): readonly Connection[];
}

export class InMemoryConnectionStore implements ConnectionStore {
  private readonly conns = new Map<string, Connection>();

  get(id: string): Connection | undefined {
    return this.conns.get(id);
  }
  put(conn: Connection): void {
    this.conns.set(conn.id, conn);
  }
  list(tenantId?: string): readonly Connection[] {
    const all = [...this.conns.values()];
    return tenantId ? all.filter((c) => c.tenantId === tenantId) : all;
  }
}
