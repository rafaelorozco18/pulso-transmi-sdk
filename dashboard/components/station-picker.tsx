"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

export function StationPicker({ stations, value }: { stations: { id: string; label: string }[]; value: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  return (
    <label className="controls">
      <span className="sr-only">Estación</span>
      <select
        className="select"
        value={value}
        onChange={(event) => {
          const next = new URLSearchParams(params.toString());
          next.set("estacion", event.target.value);
          router.replace(`${pathname}?${next.toString()}`, { scroll: false });
        }}
      >
        {stations.map((station) => (
          <option key={station.id} value={station.id}>
            {station.label}
          </option>
        ))}
      </select>
    </label>
  );
}
