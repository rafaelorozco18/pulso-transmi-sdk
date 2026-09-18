"""Diagrama entidad-relación del esquema `pulso` tal como está desplegado en Supabase.

Lee tablas, columnas, llaves y dependencias de vistas desde la base remota
(Management API de Supabase, consulta de solo lectura) y renderiza con Graphviz.
Si `dot` no está instalado, usa un contenedor efímero de Alpine.

Uso (desde la raíz del repo, tras `supabase login` y `supabase link`):
    python supabase/diagrams/generate_schema_diagram.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent
SCHEMA = "pulso"

DOMAINS = {
    "Datos de la API": ("#2a78d6", "#e2edfa", ["stations", "calendar_days", "observations", "observation_revisions", "context"]),
    "Ingesta incremental": ("#eb6834", "#fce6db", ["dataset_snapshots", "ingestion_runs", "ingestion_cursors"]),
    "Perfil base y modelos": ("#1baf7a", "#d9f2e8", ["profile_versions", "station_profiles", "model_versions", "retraining_decisions"]),
    "Competencia": ("#c98500", "#fbefcf", ["forecast_cycles", "forecast_runs", "predictions", "submissions", "leaderboard_snapshots"]),
    "Monitoreo": ("#d55181", "#f9e3ec", ["drift_signals"]),
}
VIEW_COLOR, VIEW_TINT = "#7d8187", "#eeefec"
INK, MUTED, EDGE = "#15171a", "#7d8187", "#8a8f96"

TYPE_ALIASES = {
    "timestamp with time zone": "timestamptz",
    "timestamp with time zone[]": "timestamptz[]",
    "double precision": "float8",
    "character varying": "varchar",
}


def project_ref() -> str:
    ref = os.getenv("SUPABASE_PROJECT_REF")
    if ref:
        return ref
    return (ROOT / "supabase" / ".temp" / "project-ref").read_text().strip()


def access_token() -> str:
    token = os.getenv("SUPABASE_ACCESS_TOKEN")
    if token:
        return token
    return (Path.home() / ".supabase" / "access-token").read_text().strip()


def query(sql: str) -> list[dict]:
    request = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{project_ref()}/database/query",
        data=json.dumps({"query": sql, "read_only": True}).encode(),
        headers={"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def introspect() -> tuple[list[dict], list[dict], list[dict]]:
    columns = query(f"""
        select c.relname as table_name, a.attnum, a.attname as column_name,
               format_type(a.atttypid, a.atttypmod) as data_type, a.attnotnull as not_null,
               coalesce((select true from pg_constraint k
                         where k.conrelid = c.oid and k.contype = 'p' and a.attnum = any(k.conkey)), false) as is_pk,
               c.relkind::text as kind
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
        where n.nspname = '{SCHEMA}' and c.relkind in ('r', 'v')
        order by c.relname, a.attnum""")
    foreign_keys = query(f"""
        select src.relname as src_table,
               (select array_agg(att.attname order by k.ord) from unnest(con.conkey) with ordinality k(attnum, ord)
                join pg_attribute att on att.attrelid = con.conrelid and att.attnum = k.attnum) as src_cols,
               dst.relname as dst_table,
               (select array_agg(att.attname order by k.ord) from unnest(con.confkey) with ordinality k(attnum, ord)
                join pg_attribute att on att.attrelid = con.confrelid and att.attnum = k.attnum) as dst_cols
        from pg_constraint con
        join pg_class src on src.oid = con.conrelid
        join pg_class dst on dst.oid = con.confrelid
        join pg_namespace n on n.oid = src.relnamespace
        where con.contype = 'f' and n.nspname = '{SCHEMA}'
        order by 1, 3""")
    view_deps = query(f"""
        select distinct v.relname as view_name, t.relname as table_name
        from pg_depend d
        join pg_rewrite r on r.oid = d.objid
        join pg_class v on v.oid = r.ev_class
        join pg_class t on t.oid = d.refobjid
        join pg_namespace n on n.oid = v.relnamespace
        where n.nspname = '{SCHEMA}' and v.relkind = 'v' and t.oid <> v.oid
        order by 1, 2""")
    for fk in foreign_keys:
        for key in ("src_cols", "dst_cols"):
            if isinstance(fk[key], str):  # la API devuelve arrays de Postgres como texto '{a,b}'
                fk[key] = fk[key].strip("{}").split(",")
    return columns, foreign_keys, view_deps


def table_label(name: str, rows: list[dict], fk_cols: set[str], color: str, tint: str, is_view: bool) -> str:
    header = f"{'vista · ' if is_view else ''}{name}"
    html = [
        f'<<TABLE BORDER="1" COLOR="{color}" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" BGCOLOR="#ffffff"'
        f'{" STYLE=\"dashed\"" if is_view else ""}>',
        f'<TR><TD COLSPAN="3" BGCOLOR="{tint}" ALIGN="LEFT" CELLPADDING="6">'
        f'<FONT POINT-SIZE="13" COLOR="{INK}"><B>{escape(header)}</B></FONT></TD></TR>',
    ]
    for row in rows:
        column = row["column_name"]
        keys = [k for k, flag in (("PK", row["is_pk"]), ("FK", column in fk_cols)) if flag]
        key_text = f'<FONT POINT-SIZE="8" COLOR="{color}"><B>{"·".join(keys)}</B></FONT>' if keys else " "
        name_text = f"<B>{escape(column)}</B>" if row["is_pk"] else escape(column)
        data_type = TYPE_ALIASES.get(row["data_type"], row["data_type"])
        nullable = "" if row["not_null"] or is_view else "?"
        html.append(
            f'<TR><TD ALIGN="LEFT" PORT="{column}_in" WIDTH="28">{key_text}</TD>'
            f'<TD ALIGN="LEFT"><FONT POINT-SIZE="10.5" COLOR="{INK}">{name_text}</FONT></TD>'
            f'<TD ALIGN="LEFT" PORT="{column}_out"><FONT POINT-SIZE="9.5" COLOR="{MUTED}">{escape(data_type)}{nullable}</FONT></TD></TR>'
        )
    html.append("</TABLE>>")
    return "".join(html)


def build_dot(columns: list[dict], foreign_keys: list[dict], view_deps: list[dict]) -> str:
    by_table: dict[str, list[dict]] = {}
    for row in columns:
        by_table.setdefault(row["table_name"], []).append(row)
    fk_cols: dict[str, set[str]] = {}
    for fk in foreign_keys:
        fk_cols.setdefault(fk["src_table"], set()).update(fk["src_cols"])

    tables = {t for t, rows in by_table.items() if rows[0]["kind"] == "r"}
    views = sorted(t for t, rows in by_table.items() if rows[0]["kind"] == "v")
    placed = {t for _, _, members in DOMAINS.values() for t in members}
    missing = sorted(tables - placed)

    lines = [
        "digraph pulso {",
        '  graph [rankdir=LR, bgcolor="#fcfcfb", pad=0.5, nodesep=0.28, ranksep=1.0, newrank=true,',
        '         fontname="DejaVu Sans", labelloc=t, labeljust=l, fontsize=24, fontcolor="#15171a",',
        f'         label=<<B>Esquema {SCHEMA} · Supabase pulso-transmi</B><BR/>'
        f'<FONT POINT-SIZE="13" COLOR="{MUTED}">{len(tables)} tablas · {len(views)} vistas · {len(foreign_keys)} llaves foráneas'
        f' · PK = llave primaria · FK = llave foránea · ? = admite null · ─&lt; uno a muchos · - - - vista lee de</FONT>>];',
        '  node [shape=plain, fontname="DejaVu Sans"];',
        f'  edge [color="{EDGE}", penwidth=1.3, arrowsize=0.8, fontname="DejaVu Sans"];',
    ]
    for index, (domain, (color, tint, members)) in enumerate(list(DOMAINS.items()) + [("Otras", (MUTED, VIEW_TINT, missing))]):
        members = [m for m in members if m in tables]
        if not members:
            continue
        lines += [
            f"  subgraph cluster_{index} {{",
            f'    label=<<B>{escape(domain)}</B>>; fontsize=16; fontcolor="{color}"; color="{color}"; style="rounded,dashed"; penwidth=1.2; margin=16;',
        ]
        for table in members:
            lines.append(f"    {table} [label={table_label(table, by_table[table], fk_cols.get(table, set()), color, tint, False)}];")
        lines.append("  }")

    lines += [
        "  subgraph cluster_views {",
        f'    label=<<B>Vistas para métricas y dashboard</B>>; fontsize=16; fontcolor="{VIEW_COLOR}"; color="{VIEW_COLOR}"; style="rounded,dashed"; margin=16;',
    ]
    for view in views:
        lines.append(f"    {view} [label={table_label(view, by_table[view], set(), VIEW_COLOR, VIEW_TINT, True)}];")
    lines.append("  }")

    for fk in foreign_keys:
        parent, child = fk["dst_table"], fk["src_table"]
        lines.append(
            f'  {parent}:{fk["dst_cols"][0]}_out:e -> {child}:{fk["src_cols"][0]}_in:w '
            f'[dir=both, arrowtail=tee, arrowhead=crow];'
        )
    for dep in view_deps:
        lines.append(
            f'  {dep["table_name"]} -> {dep["view_name"]} [style=dashed, color="#b9bcb6", penwidth=1.0, arrowhead=vee, arrowsize=0.6];'
        )
    lines.append("}")
    return "\n".join(lines)


def render(dot_source: str) -> None:
    (OUT_DIR / "pulso_schema.dot").write_text(dot_source)
    outputs = {"png": ["-Tpng", "-Gdpi=110"], "svg": ["-Tsvg"]}
    for extension, flags in outputs.items():
        if shutil.which("dot"):
            command = ["dot", *flags]
        else:
            command = [
                "docker", "run", "--rm", "-i", "alpine:3.20", "sh", "-c",
                f"apk add -q graphviz font-dejavu >/dev/null && dot {' '.join(flags)}",
            ]
        result = subprocess.run(command, input=dot_source.encode(), capture_output=True, check=True)
        (OUT_DIR / f"pulso_schema.{extension}").write_bytes(result.stdout)
        print(f"escrito {OUT_DIR / f'pulso_schema.{extension}'}")


def main() -> None:
    columns, foreign_keys, view_deps = introspect()
    render(build_dot(columns, foreign_keys, view_deps))


if __name__ == "__main__":
    main()
