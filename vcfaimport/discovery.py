"""Selecting VMs from a discovered vCenter inventory, and staging them for import."""

from __future__ import annotations

import csv
import fnmatch
import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .inventory import Nic, VmRecord


class SelectionError(Exception):
    pass


@dataclass
class Filters:
    """Every filter is optional; those given are ANDed together."""
    name: List[str] = field(default_factory=list)          # glob, case-insensitive
    exclude_name: List[str] = field(default_factory=list)
    cluster: List[str] = field(default_factory=list)
    folder: List[str] = field(default_factory=list)
    network: List[str] = field(default_factory=list)
    guest_os: List[str] = field(default_factory=list)
    datacenter: List[str] = field(default_factory=list)
    folder_exact: bool = False     # --folder matches subfolders unless this is set
    powered_on: bool = False
    powered_off: bool = False
    tools_running: bool = False
    with_nics: bool = False
    morefs: List[str] = field(default_factory=list)
    regex: Optional[str] = None

    def is_empty(self) -> bool:
        return not any([
            self.name, self.exclude_name, self.cluster, self.folder, self.network,
            self.guest_os, self.datacenter, self.powered_on, self.powered_off,
            self.tools_running, self.with_nics, self.morefs, self.regex,
        ])


def _glob_any(value: str, patterns: Sequence[str]) -> bool:
    low = (value or "").lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


from .folders import folder_matches, norm_folder as _norm_folder  # noqa: E402


def apply_filters(rows: Sequence[sqlite3.Row], f: Filters) -> List[sqlite3.Row]:
    out: List[sqlite3.Row] = []
    rx = re.compile(f.regex, re.IGNORECASE) if f.regex else None
    moref_set = {m.strip() for m in f.morefs if m.strip()}
    for row in rows:
        if moref_set and row["moref"] not in moref_set:
            continue
        if f.name and not _glob_any(row["name"], f.name):
            continue
        if f.exclude_name and _glob_any(row["name"], f.exclude_name):
            continue
        if rx and not rx.search(row["name"] or ""):
            continue
        if f.cluster and not _glob_any(row["cluster"], f.cluster):
            continue
        if f.folder and not folder_matches(row["folder"], f.folder, exact=f.folder_exact):
            continue
        if f.datacenter and not _glob_any(row["datacenter"], f.datacenter):
            continue
        if f.guest_os and not _glob_any(row["guest_os"], f.guest_os):
            continue
        if f.network:
            nets = [n for n in (row["networks"] or "").split(",") if n]
            if not any(_glob_any(net, f.network) for net in nets):
                continue
        if f.powered_on and (row["power_state"] or "").upper() != "POWERED_ON":
            continue
        if f.powered_off and (row["power_state"] or "").upper() == "POWERED_ON":
            continue
        if f.tools_running and "running" not in (row["tools_status"] or "").lower():
            continue
        if f.with_nics and not json.loads(row["nics_json"] or "[]"):
            continue
        out.append(row)
    return out


def read_selection_file(path: str) -> Tuple[List[str], Dict[str, str], Dict[str, int]]:
    """Read a selection list: CSV with a moref (or name) column, or one id per line.

    Returns (identifiers, namespace_by_id, wave_by_id).
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise SelectionError("selection file not found: {}".format(p))
    text = p.read_text(encoding="utf-8-sig")
    first = text.splitlines()[0] if text.splitlines() else ""

    if "," in first or ";" in first or "\t" in first:
        idents: List[str] = []
        namespaces: Dict[str, str] = {}
        waves: Dict[str, int] = {}
        reader = csv.DictReader(text.splitlines())
        fields = {(k or "").strip().lower(): k for k in (reader.fieldnames or [])}
        id_col = next((fields[k] for k in ("moref", "moid", "vm_id", "virtualmachineid", "id")
                       if k in fields), None)
        name_col = next((fields[k] for k in ("vm_name", "name") if k in fields), None)
        ns_col = fields.get("namespace")
        wave_col = fields.get("wave")
        if not id_col and not name_col:
            raise SelectionError(
                "{}: needs a moref or vm_name column".format(p))
        for row in reader:
            ident = (row.get(id_col) or "").strip() if id_col else ""
            if not ident and name_col:
                ident = (row.get(name_col) or "").strip()
            if not ident:
                continue
            idents.append(ident)
            if ns_col and (row.get(ns_col) or "").strip():
                namespaces[ident] = row[ns_col].strip()
            if wave_col and (row.get(wave_col) or "").strip():
                try:
                    waves[ident] = int(float(row[wave_col].strip()))
                except ValueError:
                    pass
        return idents, namespaces, waves

    idents = [line.strip() for line in text.splitlines()
              if line.strip() and not line.strip().startswith("#")]
    return idents, {}, {}


def resolve_identifiers(
    rows: Sequence[sqlite3.Row],
    identifiers: Sequence[str],
) -> Tuple[List[str], List[str], List[str]]:
    """Map morefs or VM names onto discovered morefs.

    Returns (morefs, unmatched, ambiguous_names).
    """
    by_moref = {r["moref"]: r["moref"] for r in rows}
    by_name: Dict[str, List[str]] = {}
    for r in rows:
        by_name.setdefault((r["name"] or "").lower(), []).append(r["moref"])

    morefs: List[str] = []
    unmatched: List[str] = []
    ambiguous: List[str] = []
    for ident in identifiers:
        key = ident.strip()
        if key in by_moref:
            morefs.append(key)
            continue
        matches = by_name.get(key.lower(), [])
        if len(matches) == 1:
            morefs.append(matches[0])
        elif len(matches) > 1:
            ambiguous.append(key)
        else:
            unmatched.append(key)
    return morefs, unmatched, ambiguous


# ------------------------------------------------------------------ mapping
@dataclass
class NetworkMapping:
    namespace: str
    subnet: str
    wave: Optional[int] = None
    device_key: Optional[int] = None
    subnet_kind: Optional[str] = None
    subnet_api_group: Optional[str] = None


def load_network_map(path: str) -> Dict[str, NetworkMapping]:
    """Read portgroup -> namespace/subnet mappings.

    Columns: portgroup,namespace,subnet[,wave][,device_key][,subnet_kind][,subnet_api_group]
    The portgroup column may contain a glob.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise SelectionError("network map not found: {}".format(p))
    out: Dict[str, NetworkMapping] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = {(k or "").strip().lower(): k for k in (reader.fieldnames or [])}
        pg_col = next((fields[k] for k in ("portgroup", "network", "network_name")
                       if k in fields), None)
        if not pg_col or "namespace" not in fields:
            raise SelectionError(
                "{}: needs at least 'portgroup' and 'namespace' columns".format(p))
        for row in reader:
            pg = (row.get(pg_col) or "").strip()
            if not pg:
                continue
            wave_raw = (row.get(fields.get("wave", ""), "") or "").strip()
            dk_raw = (row.get(fields.get("device_key", ""), "") or "").strip()
            out[pg] = NetworkMapping(
                namespace=(row.get(fields["namespace"]) or "").strip(),
                subnet=(row.get(fields.get("subnet", ""), "") or "").strip(),
                wave=int(float(wave_raw)) if wave_raw else None,
                device_key=int(dk_raw) if dk_raw else None,
                subnet_kind=(row.get(fields.get("subnet_kind", ""), "") or "").strip() or None,
                subnet_api_group=(row.get(fields.get("subnet_api_group", ""), "") or "").strip() or None,
            )
    return out


@dataclass
class FolderMapping:
    namespace: str
    wave: Optional[int] = None
    group: Optional[str] = None


def load_folder_map(path: str) -> Dict[str, FolderMapping]:
    """Read folder -> namespace[/wave/group] mappings.

    Columns: folder,namespace[,wave][,group]. A folder covers its whole subtree;
    the most specific (longest) matching folder wins. Globs are allowed.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise SelectionError("folder map not found: {}".format(p))
    out: Dict[str, FolderMapping] = {}
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = {(k or "").strip().lower(): k for k in (reader.fieldnames or [])}
        folder_col = next((fields[k] for k in ("folder", "vm_folder", "path", "folder_path")
                           if k in fields), None)
        if not folder_col or "namespace" not in fields:
            raise SelectionError("{}: needs 'folder' and 'namespace' columns".format(p))
        for row in reader:
            folder = (row.get(folder_col) or "").strip()
            if not folder:
                continue
            wave_raw = (row.get(fields.get("wave", ""), "") or "").strip()
            out[folder] = FolderMapping(
                namespace=(row.get(fields["namespace"]) or "").strip(),
                wave=int(float(wave_raw)) if wave_raw else None,
                group=(row.get(fields.get("group", ""), "") or "").strip() or None,
            )
    return out


def match_folder_map(path: str, mapping: Dict[str, FolderMapping]) -> Optional[FolderMapping]:
    """Most specific folder wins: Production/Web beats Production for a VM in Production/Web/x."""
    best: Optional[Tuple[int, FolderMapping]] = None
    for pattern, entry in mapping.items():
        if folder_matches(path, [pattern]):
            score = len(_norm_folder(pattern))
            if best is None or score > best[0]:
                best = (score, entry)
    return best[1] if best else None


def _match_mapping(network: str, mapping: Dict[str, NetworkMapping]) -> Optional[NetworkMapping]:
    if network in mapping:
        return mapping[network]
    low = (network or "").lower()
    for pattern, entry in mapping.items():
        if fnmatch.fnmatch(low, pattern.lower()):
            return entry
    return None


@dataclass
class StageResult:
    records: List[VmRecord] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    unmapped_networks: Dict[str, int] = field(default_factory=dict)
    unmapped_folders: Dict[str, int] = field(default_factory=dict)


def stage(
    rows: Sequence[sqlite3.Row],
    cfg: Config,
    mapping: Optional[Dict[str, NetworkMapping]] = None,
    default_namespace: Optional[str] = None,
    default_wave: int = 1,
    folder_mapping: Optional[Dict[str, FolderMapping]] = None,
) -> StageResult:
    """Turn selected discovered VMs into importable inventory records.

    A VM's namespace comes from, in order: the namespace set on the discovered
    row (from the picker or `select --namespace`), the folder map, the network
    map, then --default-namespace. Wave follows the same order. A VM with no
    namespace is reported, never guessed.
    """
    mapping = mapping or {}
    folder_mapping = folder_mapping or {}
    result = StageResult()

    for row in rows:
        nics_raw = json.loads(row["nics_json"] or "[]")
        namespace = (row["namespace"] or "").strip()
        # A wave set on the row (picker export, `select --wave`) is explicit and
        # wins; 0 means nobody has chosen one, so the maps may supply it.
        explicit_wave = int(row["wave"] or 0)
        mapped_wave: Optional[int] = None
        group = ""
        nics: List[Nic] = []
        notes: List[str] = []

        # Batches are built per folder, so `run --folder`, `rollback --folder`
        # and the batch names all line up with the tree. An explicit group from
        # the folder map overrides this.
        group = _norm_folder(row["folder"] or "") or "root"

        by_folder = match_folder_map(row["folder"] or "", folder_mapping) if folder_mapping else None
        if by_folder:
            if not namespace and by_folder.namespace:
                namespace = by_folder.namespace
            if by_folder.wave:
                mapped_wave = by_folder.wave
            if by_folder.group:
                group = by_folder.group
        elif folder_mapping:
            result.unmapped_folders[row["folder"] or "(root)"] = \
                result.unmapped_folders.get(row["folder"] or "(root)", 0) + 1

        for nic in nics_raw:
            network = nic.get("network_name") or nic.get("network_id") or ""
            entry = _match_mapping(network, mapping)
            if entry is None:
                if mapping:
                    result.unmapped_networks[network] = result.unmapped_networks.get(network, 0) + 1
                    notes.append("unmapped network: {}".format(network))
                continue
            if not namespace and entry.namespace:
                namespace = entry.namespace
            if entry.wave and mapped_wave is None:      # folder map wave already won if set
                mapped_wave = entry.wave
            nics.append(
                Nic(
                    device_key=entry.device_key or int(nic.get("device_key") or cfg.default_device_key),
                    subnet=entry.subnet or None,
                    subnet_kind=entry.subnet_kind or cfg.subnet_kind,
                    subnet_api_group=entry.subnet_api_group or cfg.subnet_api_group,
                )
            )

        if not namespace:
            namespace = (default_namespace or "").strip()
        if not namespace:
            result.problems.append(
                "{} ({}) in folder '{}': no target namespace -- map its folder or network, "
                "set one in the picker, or pass --default-namespace".format(
                    row["name"], row["moref"], row["folder"] or "(root)"))
            continue
        if not nics:
            notes.append("no mapped network interfaces")

        result.records.append(
            VmRecord(
                moref=row["moref"],
                vm_name=row["name"] or row["moref"],
                namespace=namespace,
                nics=nics,
                mode=cfg.mode,
                wave=explicit_wave or mapped_wave or default_wave,
                group=group,
                notes="; ".join(notes) or (row["notes"] or ""),
                row=0,
            )
        )
    return result


def write_selection_csv(rows: Sequence[sqlite3.Row], path: str) -> int:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["vm_name", "moref", "namespace", "wave", "power_state", "datacenter",
                         "cluster", "folder", "networks", "cpu", "memory_mb", "tools", "guest_os"])
        for r in rows:
            writer.writerow([r["name"], r["moref"], r["namespace"],
                             r["wave"] or "", r["power_state"], r["datacenter"],
                             r["cluster"], r["folder"], r["networks"], r["cpu_count"],
                             r["memory_mb"], r["tools_status"], r["guest_os"]])
    return len(rows)


# ------------------------------------------------------------------- picker
PICKER_CSS = """
:root{--bg:#f7f7f5;--fg:#1b1b19;--muted:#6b6b66;--card:#fff;--line:#e3e3df;--accent:#2f6fd0;
 --ok:#2f7d4f;--warn:#8a5a00;--sel:#e8f0fb}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececea;--muted:#9a9a95;--card:#1f1f24;
 --line:#33333a;--accent:#6fa8e8;--ok:#67c98d;--warn:#e0ab4a;--sel:#23304a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px}
h1{font-size:17px;margin:0 0 10px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select,button{font:inherit;padding:6px 9px;border:1px solid var(--line);border-radius:7px;
 background:var(--bg);color:var(--fg)}
input[type=search]{min-width:240px}
button{cursor:pointer;background:var(--card)}
button.primary{background:var(--accent);color:#fff;border-color:transparent;font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
.count{color:var(--muted);font-size:13px;margin-left:auto}
.count b{color:var(--fg)}
main{padding:0 20px 60px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:0;background:var(--bg);font-size:12px;color:var(--muted);cursor:pointer;
 border-bottom:2px solid var(--line)}
th:first-child,td:first-child{width:34px;cursor:default}
tr.on td{background:var(--sel)}
td.ns input{padding:3px 6px;font-size:12px;width:150px}
.tag{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.on-tag{color:var(--ok);border-color:currentColor}
.off-tag{color:var(--muted)}
.warn{color:var(--warn)}
footer{position:fixed;bottom:0;left:0;right:0;background:var(--card);border-top:1px solid var(--line);
 padding:10px 20px;display:flex;gap:10px;align-items:center}
.hint{color:var(--muted);font-size:12px}
"""

PICKER_JS = """
const rows = window.__VMS__;
const tbody = document.getElementById('rows');
const state = new Map(rows.map(v => [v.moref, {sel: !!v.selected, ns: v.namespace || ''}]));
let view = rows.slice();
let sortKey = 'name', sortDir = 1;

function facet(id, key) {
  const sel = document.getElementById(id);
  const vals = [...new Set(rows.map(r => r[key]).filter(Boolean))].sort();
  for (const v of vals) { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); }
}
facet('cluster', 'cluster'); facet('power', 'power_state');
// Folder options are the tree: every ancestor path, so choosing "Production"
// includes everything beneath it.
{
  const paths = new Set();
  for (const r of rows) {
    const parts = (r.folder || '').split('/').filter(Boolean);
    for (let i = 1; i <= parts.length; i++) paths.add(parts.slice(0, i).join('/'));
  }
  const sel = document.getElementById('folder');
  for (const p of [...paths].sort((a, b) => a.localeCompare(b))) {
    const o = document.createElement('option'); o.value = p;
    o.textContent = '\u00a0\u00a0'.repeat(p.split('/').length - 1) + p.split('/').pop() + '/';
    o.title = p; sel.appendChild(o);
  }
}
const nets = [...new Set(rows.flatMap(r => (r.networks || '').split(',').filter(Boolean)))].sort();
for (const v of nets) { const o = document.createElement('option'); o.value = v; o.textContent = v; document.getElementById('network').appendChild(o); }

function matches(r) {
  const q = document.getElementById('q').value.trim().toLowerCase();
  if (q) {
    const hay = [r.name, r.moref, r.cluster, r.folder, r.networks, r.guest_os].join(' ').toLowerCase();
    if (!q.split(/\\s+/).every(t => hay.includes(t))) return false;
  }
  for (const [id, key] of [['cluster','cluster'],['power','power_state']]) {
    const v = document.getElementById(id).value;
    if (v && r[key] !== v) return false;
  }
  const fv = document.getElementById('folder').value;
  if (fv && !(r.folder === fv || (r.folder || '').startsWith(fv + '/'))) return false;
  const net = document.getElementById('network').value;
  if (net && !(r.networks || '').split(',').includes(net)) return false;
  if (document.getElementById('onlySel').checked && !state.get(r.moref).sel) return false;
  return true;
}

function render() {
  view = rows.filter(matches).sort((a, b) => {
    const x = (a[sortKey] ?? '').toString().toLowerCase(), y = (b[sortKey] ?? '').toString().toLowerCase();
    return x < y ? -sortDir : x > y ? sortDir : 0;
  });
  tbody.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const r of view) {
    const s = state.get(r.moref);
    const tr = document.createElement('tr');
    if (s.sel) tr.className = 'on';
    tr.innerHTML = `<td><input type="checkbox" data-m="${r.moref}" ${s.sel ? 'checked' : ''}></td>
      <td>${esc(r.name)}</td><td><code>${esc(r.moref)}</code></td>
      <td><span class="tag ${r.power_state === 'POWERED_ON' ? 'on-tag' : 'off-tag'}">${esc((r.power_state||'').replace('POWERED_',''))}</span></td>
      <td>${esc(r.cluster)}</td><td>${esc(r.folder)}</td><td>${esc(r.networks)}</td>
      <td>${r.cpu_count || ''}</td><td>${r.memory_mb || ''}</td>
      <td class="${(r.tools_status||'').toLowerCase().includes('running') ? '' : 'warn'}">${esc(r.tools_status)}</td>
      <td class="ns"><input type="text" data-ns="${r.moref}" value="${esc(s.ns)}" placeholder="namespace"></td>`;
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
  updateCount();
}

function esc(s) { return (s ?? '').toString().replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function updateCount() {
  const sel = [...state.values()].filter(s => s.sel).length;
  const noNs = [...state.entries()].filter(([, s]) => s.sel && !s.ns).length;
  document.getElementById('count').innerHTML =
    `showing <b>${view.length}</b> of ${rows.length} &middot; selected <b>${sel}</b>` +
    (noNs ? ` &middot; <span class="warn">${noNs} without a namespace</span>` : '');
  document.getElementById('dl').disabled = sel === 0;
}

tbody.addEventListener('change', e => {
  const m = e.target.dataset.m, n = e.target.dataset.ns;
  if (m) { state.get(m).sel = e.target.checked; e.target.closest('tr').className = e.target.checked ? 'on' : ''; }
  if (n) { state.get(n).ns = e.target.value.trim(); }
  updateCount();
});

for (const id of ['q','cluster','folder','power','network','onlySel'])
  document.getElementById(id).addEventListener('input', render);

document.querySelectorAll('th[data-k]').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  sortDir = (k === sortKey) ? -sortDir : 1; sortKey = k; render();
}));

document.getElementById('selAll').onclick = () => { view.forEach(r => state.get(r.moref).sel = true); render(); };
document.getElementById('selNone').onclick = () => { view.forEach(r => state.get(r.moref).sel = false); render(); };
document.getElementById('applyNs').onclick = () => {
  const ns = document.getElementById('bulkNs').value.trim();
  if (!ns) return;
  view.forEach(r => { if (state.get(r.moref).sel) state.get(r.moref).ns = ns; });
  render();
};

document.getElementById('dl').onclick = () => {
  const out = [['vm_name','moref','namespace','wave','power_state','cluster','folder','networks']];
  const wave = document.getElementById('bulkWave').value.trim() || '1';
  for (const r of rows) {
    const s = state.get(r.moref);
    if (!s.sel) continue;
    out.push([r.name, r.moref, s.ns, wave, r.power_state, r.cluster, r.folder, r.networks]);
  }
  const csv = out.map(row => row.map(c => {
    const v = (c ?? '').toString();
    return /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }).join(',')).join('\\n');
  const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'selection.csv';
  a.click();
  URL.revokeObjectURL(a.href);
};

render();
"""


def write_picker(rows: Sequence[sqlite3.Row], path: str, source: str = "") -> str:
    """Write a standalone HTML picker: filter, tick, assign namespaces, export CSV."""
    data = []
    for r in rows:
        data.append({
            "moref": r["moref"], "name": r["name"], "power_state": r["power_state"],
            "cluster": r["cluster"], "folder": r["folder"], "networks": r["networks"],
            "cpu_count": r["cpu_count"], "memory_mb": r["memory_mb"],
            "tools_status": r["tools_status"], "guest_os": r["guest_os"],
            "selected": bool(r["selected"]), "namespace": r["namespace"],
        })
    payload = json.dumps(data).replace("</", "<\\/")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    doc = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Select VMs to import</title><style>{css}</style></head><body>
<header>
  <h1>Select VMs to import into VCF Automation</h1>
  <div class="controls">
    <input type="search" id="q" placeholder="search name, moref, cluster, network...">
    <select id="cluster"><option value="">all clusters</option></select>
    <select id="folder"><option value="">all folders</option></select>
    <select id="network"><option value="">all networks</option></select>
    <select id="power"><option value="">any power state</option></select>
    <label class="hint"><input type="checkbox" id="onlySel"> selected only</label>
    <span class="count" id="count"></span>
  </div>
</header>
<main><div class="scroll"><table>
<thead><tr>
  <th></th><th data-k="name">VM</th><th data-k="moref">moref</th><th data-k="power_state">power</th>
  <th data-k="cluster">cluster</th><th data-k="folder">folder</th><th data-k="networks">networks</th>
  <th data-k="cpu_count">vCPU</th><th data-k="memory_mb">MiB</th><th data-k="tools_status">tools</th>
  <th>namespace</th>
</tr></thead>
<tbody id="rows"></tbody>
</table></div></main>
<footer>
  <button id="selAll">select all shown</button>
  <button id="selNone">clear shown</button>
  <input type="text" id="bulkNs" placeholder="namespace for selected">
  <button id="applyNs">apply</button>
  <input type="text" id="bulkWave" placeholder="wave" value="1" style="width:70px">
  <button class="primary" id="dl">Download selection.csv</button>
  <span class="hint">{src} &middot; discovered {gen} &middot; then run: vcfa-import select --from-file selection.csv</span>
</footer>
<script>window.__VMS__ = {payload};</script>
<script>{js}</script>
</body></html>""".format(
        css=PICKER_CSS,
        js=PICKER_JS,
        payload=payload,
        gen=generated,
        src=html.escape(source or "vCenter inventory"),
    )
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return str(p)
