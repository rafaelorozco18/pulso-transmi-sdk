"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore, useTransition } from "react";

import { fmtAgo } from "@/lib/format";
import { MoonIcon, RefreshIcon, SunIcon } from "./icons";

const LINKS = [
  { href: "/", label: "Resumen" },
  { href: "/drift", label: "Drift" },
  { href: "/desempeno", label: "Desempeño" },
  { href: "/modelos", label: "Modelos y pipeline" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="nav" aria-label="Secciones">
      {LINKS.map((link) => (
        <Link key={link.href} href={link.href} aria-current={pathname === link.href ? "page" : undefined}>
          {link.label}
        </Link>
      ))}
    </nav>
  );
}

const REFRESH_SECONDS = 60;

/** Vuelve a pedir los datos al servidor cada minuto (el pipeline escribe ~1 vez por hora). */
export function AutoRefresh({ renderedAt }: { renderedAt: string }) {
  const router = useRouter();
  const [now, setNow] = useState(() => Date.now());
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 5_000);
    const refresh = setInterval(() => {
      if (document.visibilityState === "visible") startTransition(() => router.refresh());
    }, REFRESH_SECONDS * 1000);
    return () => {
      clearInterval(tick);
      clearInterval(refresh);
    };
  }, [router]);

  return (
    <button
      type="button"
      className="icon-btn"
      onClick={() => startTransition(() => router.refresh())}
      title="Actualizar ahora (se actualiza solo cada minuto)"
    >
      <RefreshIcon size={13} style={pending ? { animation: "spin 1s linear infinite" } : undefined} />
      <span className="hide-sm" suppressHydrationWarning>
        {pending ? "Actualizando…" : `Actualizado ${fmtAgo(renderedAt, now)}`}
      </span>
      <style>{"@keyframes spin{to{transform:rotate(360deg)}}"}</style>
    </button>
  );
}

type Theme = "light" | "dark";

function currentTheme(): Theme {
  const stamped = document.documentElement.dataset.theme;
  if (stamped === "light" || stamped === "dark") return stamped;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Tema efectivo (elegido o del sistema) como store externo. */
function subscribeTheme(onChange: () => void): () => void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  media.addEventListener("change", onChange);
  window.addEventListener("pulso-theme", onChange);
  return () => {
    media.removeEventListener("change", onChange);
    window.removeEventListener("pulso-theme", onChange);
  };
}

function useTheme(): Theme | null {
  return useSyncExternalStore<Theme | null>(subscribeTheme, currentTheme, () => null);
}

export function ThemeToggle() {
  const theme = useTheme();

  function toggle() {
    const next: Theme = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("pulso-theme", next);
    } catch {}
    window.dispatchEvent(new Event("pulso-theme"));
  }

  return (
    <button
      type="button"
      className="icon-btn"
      onClick={toggle}
      aria-label={theme === "dark" ? "Cambiar a modo claro" : "Cambiar a modo oscuro"}
      title="Cambiar tema"
    >
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}

/** Para piezas que no leen CSS, como el mapa base. */
export function useIsDark(): boolean {
  return useTheme() === "dark";
}
