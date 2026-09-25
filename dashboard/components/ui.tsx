import type { ReactNode } from "react";

import { AlertIcon, CheckIcon, ClockIcon, SkipIcon, XIcon } from "./icons";

export function Card({
  title,
  sub,
  action,
  foot,
  className = "",
  children,
}: {
  title?: ReactNode;
  sub?: ReactNode;
  action?: ReactNode;
  foot?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <div className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {sub && <div className="card-sub">{sub}</div>}
          </div>
          {action}
        </div>
      )}
      {children}
      {foot && <div className="card-foot">{foot}</div>}
    </section>
  );
}

export function Tile({
  label,
  value,
  unit,
  meta,
  badge,
}: {
  label: ReactNode;
  value: ReactNode;
  unit?: string;
  meta?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <section className="card">
      <div className="card-head" style={{ marginBottom: 0 }}>
        <div className="tile-label">{label}</div>
        {badge}
      </div>
      <div className="tile-value">
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {meta && <div className="tile-meta">{meta}</div>}
    </section>
  );
}

export type Tone = "good" | "warning" | "critical" | "neutral";

/** Estado: siempre ícono + texto, nunca solo color. */
export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  const Icon = tone === "good" ? CheckIcon : tone === "warning" ? AlertIcon : tone === "critical" ? XIcon : SkipIcon;
  return (
    <span className={`badge badge-${tone}`}>
      <Icon size={13} />
      {children}
    </span>
  );
}

export function RunStatusBadge({ status }: { status: string }) {
  switch (status) {
    case "success":
      return <Badge tone="good">OK</Badge>;
    case "failed":
      return <Badge tone="critical">Falló</Badge>;
    case "skipped":
      return <Badge tone="neutral">Omitida</Badge>;
    case "running":
      return (
        <span className="badge">
          <ClockIcon size={13} />
          En curso
        </span>
      );
    default:
      return <Badge tone="neutral">{status}</Badge>;
  }
}

export function Delta({ value, digits = 2, suffix = "" }: { value: number | null; digits?: number; suffix?: string }) {
  if (value == null || Number.isNaN(value)) return null;
  const up = value >= 0;
  const text = `${up ? "▲" : "▼"} ${Math.abs(value).toLocaleString("es-CO", { maximumFractionDigits: digits, minimumFractionDigits: digits })}${suffix}`;
  return <span className={up ? "delta-up" : "delta-down"}>{text}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
