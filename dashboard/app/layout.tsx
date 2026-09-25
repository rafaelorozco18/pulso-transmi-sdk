import type { Metadata } from "next";
import Link from "next/link";

import { AutoRefresh, Nav, ThemeToggle } from "@/components/shell";
import { GithubIcon, PulseIcon } from "@/components/icons";
import { getClock } from "@/lib/data";
import { fmtDateTime } from "@/lib/format";
import "leaflet/dist/leaflet.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Pulso TransMi · Monitoreo",
  description: "Monitoreo MLOps del pronóstico de demanda de TransMilenio: drift, desempeño, modelos y pipeline.",
};

// Aplica el tema elegido antes del primer pintado (evita el destello claro/oscuro).
const THEME_SCRIPT = `try{var t=localStorage.getItem("pulso-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

const REPO_URL = "https://github.com/rafaelorozco18/pulso-transmi-sdk";

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const clock = await getClock();
  const renderedAt = new Date(clock.now).toISOString();
  return (
    <html lang="es" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <header className="topbar">
          <div className="topbar-inner">
            <div className="brand-row">
              <Link className="brand" href="/">
                <span className="brand-mark">
                  <PulseIcon size={16} />
                </span>
                <span>
                  <span className="brand-title">Pulso TransMi · Monitoreo MLOps</span>
                  <br />
                  <span className="brand-sub">
                    Datos al corte virtual <b className="tabular">{fmtDateTime(clock.max_observed_at)}</b> · 12 estaciones · slots de
                    15 min
                  </span>
                </span>
              </Link>
              <div className="toolbar">
                <AutoRefresh renderedAt={renderedAt} />
                <a className="icon-btn" href={REPO_URL} target="_blank" rel="noreferrer" aria-label="Repositorio en GitHub">
                  <GithubIcon />
                </a>
                <ThemeToggle />
              </div>
            </div>
            <Nav />
          </div>
        </header>
        <main className="shell">
          {children}
          <footer className="footer">
            <span>
              Fuente: Supabase (esquema <code>pulso</code>, vistas de solo lectura <code>dashboard.*</code>) · escrito por el
              pipeline de GitHub Actions en cada ciclo.
            </span>
            <span>Horas en America/Bogotá. Las fechas de datos siguen el reloj virtual de la competencia.</span>
          </footer>
        </main>
      </body>
    </html>
  );
}
