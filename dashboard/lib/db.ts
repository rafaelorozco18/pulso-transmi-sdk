import "server-only";

import { connection } from "next/server";
import { Pool, types } from "pg";

// bigint (count) y numeric llegan como texto por defecto; aquí son números pequeños.
types.setTypeParser(types.builtins.INT8, (value) => Number(value));
types.setTypeParser(types.builtins.NUMERIC, (value) => Number(value));

declare global {
  var dashboardPool: Pool | undefined;
}

function createPool(): Pool {
  const raw = process.env.DASHBOARD_DATABASE_URL;
  if (!raw) throw new Error("Falta DASHBOARD_DATABASE_URL (rol de solo lectura dashboard_reader)");
  // El pooler de Supabase usa su propia CA; el canal va cifrado igual.
  const url = new URL(raw);
  url.searchParams.delete("sslmode");
  return new Pool({
    connectionString: url.toString(),
    ssl: { rejectUnauthorized: false },
    max: 3,
    idleTimeoutMillis: 10_000,
    connectionTimeoutMillis: 10_000,
  });
}

const pool = globalThis.dashboardPool ?? createPool();
if (process.env.NODE_ENV !== "production") globalThis.dashboardPool = pool;

/** Consulta de solo lectura. Siempre en tiempo de request: los datos cambian cada ciclo. */
export async function query<T>(text: string, params: unknown[] = []): Promise<T[]> {
  await connection();
  const result = await pool.query(text, params);
  return result.rows as T[];
}

export function iso(value: Date | string | null | undefined): string | null {
  if (value == null) return null;
  return value instanceof Date ? value.toISOString() : new Date(value).toISOString();
}
