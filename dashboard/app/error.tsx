"use client";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="empty" style={{ minHeight: 320, gap: 12 }}>
      <div>
        <b>No se pudieron cargar los datos.</b>
        <div className="card-sub">{error.digest ? `Referencia ${error.digest}` : error.message}</div>
      </div>
      <button type="button" className="icon-btn" onClick={reset}>
        Reintentar
      </button>
    </div>
  );
}
