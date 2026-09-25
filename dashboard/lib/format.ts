// Formato compartido por servidor y cliente. Todo se muestra en hora de Bogotá.

const TZ = "America/Bogota";

const dayTime = new Intl.DateTimeFormat("es-CO", {
  timeZone: TZ,
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dayOnly = new Intl.DateTimeFormat("es-CO", { timeZone: TZ, day: "numeric", month: "short" });
const timeOnly = new Intl.DateTimeFormat("es-CO", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });

function clean(text: string): string {
  return text.replace(/\./g, "").replace(",", "");
}

/** "13 sep 10:30" */
export function fmtDateTime(value: string | number | Date | null | undefined): string {
  if (value == null) return "—";
  return clean(dayTime.format(new Date(value)));
}

/** "13 sep" */
export function fmtDay(value: string | number | Date): string {
  return clean(dayOnly.format(new Date(value)));
}

/** "10:30" */
export function fmtTime(value: string | number | Date): string {
  return timeOnly.format(new Date(value));
}

/** Etiqueta de eje: hora, y el día cuando cae a medianoche. */
export function fmtTick(value: string | number | Date): string {
  const time = fmtTime(value);
  return time === "00:00" ? fmtDay(value) : time;
}

export function fmtNumber(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("es-CO", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtSigned(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  const text = fmtNumber(Math.abs(value), digits);
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

/** log-ratio → variación porcentual de la demanda frente al perfil: 0,18 → "+19,7 %". */
export function fmtBias(logRatio: number | null | undefined): string {
  if (logRatio == null) return "—";
  return `${fmtSigned((Math.exp(logRatio) - 1) * 100, 1)} %`;
}

export function fmtAgo(value: string | null | undefined, now: number): string {
  if (!value) return "—";
  const seconds = Math.max(0, Math.round((now - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `hace ${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `hace ${hours} h ${minutes % 60} min`;
  return `hace ${Math.round(hours / 24)} días`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${fmtNumber(seconds, 1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

export const STAGE_LABEL: Record<string, string> = {
  collector: "Colector",
  inference: "Inferencia",
  performance: "Desempeño y drift",
  retraining: "Reentrenamiento",
};

export const TRIGGER_LABEL: Record<string, string> = {
  initial: "Inicial",
  scheduled: "Programado",
  drift: "Drift",
  manual: "Manual",
  performance: "Desempeño",
};

export const ARCHETYPE_LABEL: Record<string, string> = {
  portal_origen_am: "Portal (pico AM)",
  intercambio_doble_pico: "Intercambio (doble pico)",
  oficinas_destino_am: "Oficinas (destino AM)",
  universitaria_mediodia: "Universitaria (mediodía)",
  ocio_nocturno: "Ocio nocturno",
};

/** Nombre corto para ejes: "Portal Américas", "Calle 100". */
export function shortStation(name: string): string {
  return name.split(/ [–-] /)[0].trim();
}

export const HORIZON_LABEL = (step: number | string) => `${Number(step) * 15} min`;

/** Versión legible: "hl14 · 24 sep 03:51". */
export function shortVersion(version: string | null | undefined): string {
  if (!version) return "—";
  const match = version.match(/^[a-z-]+?-(hl\d+|win\d+|default)-(\d{8}T\d{6}Z)/);
  if (match) {
    const stamp = match[2];
    const date = `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}T${stamp.slice(9, 11)}:${stamp.slice(11, 13)}:00Z`;
    return `${match[1]} · ${fmtDateTime(date)}`;
  }
  const legacy = version.match(/^(log-linear-profile)-(\d{8}T\d{6}Z)/);
  if (legacy) return `log-lineal · ${legacy[2].slice(6, 8)}/${legacy[2].slice(4, 6)}`;
  return version;
}
