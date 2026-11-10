#!/usr/bin/env python3
"""ACP SLI procurement collector: Excel/CSV ingestion + SQLite version history.

Live website export selectors are best-effort and must be verified on first run.
The original ACP SLI does NOT guarantee a three-year accessible history.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import re
import sqlite3
import sys
import unicodedata
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = 'https://apps.pancanal.com/sli/LicitacionesBusqueda/Welcome'
LOG = logging.getLogger('acp-agent')

CREATE_TABLES = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tenders (
    tender_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT,
    category TEXT,
    procurement_type TEXT,
    purchasing_agent TEXT,
    purchasing_unit TEXT,
    published_at TEXT,
    updated_at TEXT,
    closes_at TEXT,
    amendment_count INTEGER,
    supplier TEXT,
    amount TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tender_versions (
    tender_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (tender_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS source_files (
    sha256 TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    row_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    source TEXT NOT NULL,
    files INTEGER NOT NULL DEFAULT 0,
    observed INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    errors TEXT
);
CREATE INDEX IF NOT EXISTS idx_tenders_published ON tenders(published_at);
CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_versions_observed ON tender_versions(observed_at);
'''

FIELD_ALIASES = {
    'tender_id': ['licitacion', 'numero de licitacion', 'n de licitacion', 'no licitacion', 'nro licitacion', 'tender number', 'tender id', 'numero del acto', 'no de licitacion'],
    'title': ['descripcion', 'objeto', 'titulo', 'licitacion descripcion', 'description', 'description of purchase', 'detalle de la licitacion', 'nombre de la licitacion'],
    'status': ['estatus', 'estado', 'status', 'estado de la licitacion', 'estatus de la licitacion'],
    'category': ['categoria', 'categoria de compra', 'category', 'rubro'],
    'procurement_type': ['tipo de licitacion', 'tipo', 'procedimiento', 'tipo de contratacion', 'solicitud'],
    'purchasing_agent': ['agente de compras', 'agente comprador', 'buyer', 'comprador'],
    'purchasing_unit': ['unidad de compras', 'unidad solicitante', 'unidad', 'departamento'],
    'published_at': ['fecha de publicacion', 'publicacion', 'fecha publicacion', 'published', 'publication date'],
    'updated_at': ['ultima revision', 'fecha ultima revision', 'revision', 'modified', 'update date'],
    'closes_at': ['fecha y hora de cierre', 'fecha cierre', 'fecha de cierre', 'closing date', 'cierre'],
    'amendment_count': ['enmienda', 'numero de enmienda', 'cantidad de enmiendas', 'amendments'],
    'supplier': ['proveedor adjudicado', 'proveedor', 'supplier', 'adjudicatario'],
    'amount': ['monto adjudicado', 'monto', 'importe', 'valor adjudicado', 'amount', 'precio adjudicado'],
    'source_url': ['enlace', 'url', 'link', 'detalle url'],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def norm(value: Any) -> str:
    s = unicodedata.normalize('NFKD', str(value or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', re.sub(r'[^a-zA-Z0-9]+', ' ', s)).strip().lower()


def s(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value).strip()


def column_for(raw: str) -> str | None:
    x = norm(raw)
    if not x:
        return None
    # Disambiguate partial headers and combined label columns.
    for field, aliases in FIELD_ALIASES.items():
        if x in aliases:
            return field
    for field, aliases in FIELD_ALIASES.items():
        if field == 'tender_id' and (x == 'no' or x == 'numero'):
            return field
        if len(x) > 5 and any(x.startswith(alias + ' ') for alias in aliases if len(alias) >= 6):
            return field
    return None


def parse_dt(raw: Any) -> str | None:
    val = s(raw)
    if not val:
        return None
    if isinstance(raw, (date, datetime)):
        return raw.isoformat(sep=' ') if isinstance(raw, datetime) else raw.isoformat()
    val = val.replace('\u00a0', ' ')
    months = {'ene':'01','feb':'02','mar':'03','abr':'04','may':'05','jun':'06',
              'jul':'07','ago':'08','sep':'09','sept':'09','oct':'10','nov':'11','dic':'12',
              'jan':'01','apr':'04','aug':'08','dec':'12'}
    m = re.match(r'^(\d{1,2})[- /]([a-zA-Z]{3,4})[- /](\d{4})(.*)$', val)
    if m and m.group(2).lower() in months:
        val = f'{m.group(3)}-{months[m.group(2).lower()]}-{int(m.group(1)):02d}{m.group(4)}'
    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %I:%M:%S %p',
                '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
                '%d/%m/%Y %I:%M %p', '%d/%m/%Y %H:%M', '%d/%m/%Y',
                '%m/%d/%Y %I:%M %p', '%m/%d/%Y'):
        try:
            return datetime.strptime(val, fmt).isoformat(sep=' ', timespec='seconds')
        except ValueError:
            pass
    return val  # preserve the original when not parseable; don't fabricate dates


def is_in_range(raw: str | None, start: str, end: str) -> bool:
    if not raw:
        return True  # unknown dates are kept for review rather than discarded
    if not re.match(r'^\d{4}-\d{2}-\d{2}', raw):
        return True
    return start <= raw[:10] <= end


def as_dict(values: list[Any]) -> list[dict[str, str]]:
    """Find likely header row in first 20 rows and map named columns."""
    best = (-1, -1, {})
    for idx, row in enumerate(values[:20]):
        cols = {i: column_for(s(v)) for i, v in enumerate(row)}
        mapped = {i: k for i, k in cols.items() if k}
        score = len(set(mapped.values())) + (8 if 'tender_id' in mapped.values() else 0)
        if score > best[0]:
            best = (score, idx, mapped)
    score, header_idx, mapped = best
    if score < 10 or 'tender_id' not in mapped.values():
        raise ValueError('Cannot locate tender ID column in Excel/CSV. Inspect header and update FIELD_ALIASES.')
    out = []
    for row in values[header_idx + 1:]:
        entry = {key: s(row[i]) if i < len(row) else '' for i, key in mapped.items()}
        if entry.get('tender_id'):
            out.append(entry)
    return out


class _TableReader(HTMLParser):
    """Some legacy '.xls' downloads are HTML tables with an Excel MIME type."""
    def __init__(self):
        super().__init__()
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == 'table' and self.table is None:
            self.table = []
        elif self.table is not None and tag == 'tr':
            self.row = []
        elif self.row is not None and tag in ('td', 'th'):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def rows_from_html(data: bytes) -> list[dict[str, str]]:
    reader = _TableReader()
    reader.feed(data.decode('utf-8-sig', errors='replace'))
    for table in reader.tables:
        try:
            return as_dict(table)
        except ValueError:
            continue
    raise ValueError('No recognizable tender table in HTML/legacy Excel export')


def read_rows(path: Path) -> list[dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix in ('.xls', '.html', '.htm'):
        signature = path.read_bytes()[:1024].lstrip().lower()
        if b'<html' in signature or b'<table' in signature or b'<!doctype html' in signature:
            return rows_from_html(path.read_bytes())
    if suffix == '.csv':
        content = None
        for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
            try:
                with path.open('r', encoding=encoding, newline='') as handle:
                    content = list(csv.reader(handle))
                break
            except UnicodeDecodeError:
                continue
        return as_dict(content or [])
    if suffix in ('.xlsx', '.xls', '.xlsm', '.xlsb', '.ods'):
        try:
            from python_calamine import CalamineWorkbook
        except ImportError as e:
            raise RuntimeError('Excel import requires: pip install python-calamine') from e
        wb = CalamineWorkbook.from_path(str(path))
        all_rows: list[dict[str, str]] = []
        for name in wb.sheet_names:
            sheet = wb.get_sheet_by_name(name)
            values = sheet.to_python()
            try:
                all_rows.extend(as_dict(values))
            except ValueError:
                continue
        if not all_rows:
            raise ValueError(f'No recognizable tender table found in {path.name}')
        return all_rows
    if suffix == '.json':
        parsed = json.loads(path.read_text(encoding='utf-8'))
        records = parsed if isinstance(parsed, list) else parsed.get('records', [])
        if not isinstance(records, list):
            raise ValueError('JSON input must be a list of tender objects or {"records": [...]}.')
        return [{(column_for(str(k)) or str(k)): s(v) for k, v in obj.items()} for obj in records]
    raise ValueError(f'Unsupported file type: {path.suffix}')


def connect(dbpath: Path) -> sqlite3.Connection:
    dbpath.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dbpath))
    conn.row_factory = sqlite3.Row
    conn.executescript(CREATE_TABLES)
    return conn


def clean_tender_id(raw: str) -> str | None:
    # ACP tenders generally use digits; retain alphanumeric IDs from imported legacy sources.
    val = re.sub(r'^(licitaci[oó]n\s*(n[oú.]*)?\s*[:#-]?\s*)', '', raw.strip(), flags=re.I)
    val = re.sub(r'\s+', ' ', val).strip()
    return val if len(val) >= 3 and len(val) <= 80 else None


def upsert(conn: sqlite3.Connection, item: dict[str, str], source: str, cutoff: str, to_date: str) -> str:
    tender_id = clean_tender_id(item.get('tender_id', ''))
    if not tender_id:
        return 'skipped'
    item = dict(item)
    item['tender_id'] = tender_id
    for field in ('published_at', 'updated_at', 'closes_at'):
        item[field] = parse_dt(item.get(field)) or ''
    if not is_in_range(item.get('published_at'), cutoff, to_date):
        return 'skipped'
    old = conn.execute('SELECT fingerprint, raw_json, updated_at FROM tenders WHERE tender_id=?', (tender_id,)).fetchone()
    canonical = {key: item.get(key, '') for key in FIELD_ALIASES}
    canonical['tender_id'] = tender_id
    if old:
        incoming_updated = canonical.get('updated_at', '')
        previous = json.loads(old['raw_json'])
        # Keep richer fields already collected when a new export has blanks.
        for k, prior in previous.items():
            if k in canonical and not canonical[k]:
                canonical[k] = prior
        # An older archived snapshot should not roll back a newer status.
        if (source != 'ACP SLI public Excel export' and old['updated_at']
                and incoming_updated and incoming_updated < old['updated_at']):
            canonical['status'] = previous.get('status', canonical['status'])
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    now = utcnow()
    conn.execute('''INSERT OR IGNORE INTO tender_versions(tender_id, fingerprint, observed_at, source, raw_json)
                    VALUES (?, ?, ?, ?, ?)''', (tender_id, fingerprint, now, source, payload))
    try:
        amendments = int(re.sub(r'\D+', '', canonical.get('amendment_count', '')) or '0')
    except ValueError:
        amendments = None
    base = (canonical.get('title'), canonical.get('status'), canonical.get('category'),
            canonical.get('procurement_type'), canonical.get('purchasing_agent'),
            canonical.get('purchasing_unit'), canonical.get('published_at'),
            canonical.get('updated_at'), canonical.get('closes_at'), amendments,
            canonical.get('supplier'), canonical.get('amount'), source,
            canonical.get('source_url') or BASE_URL)
    if old:
        if old['fingerprint'] == fingerprint:
            conn.execute('UPDATE tenders SET last_seen_at=? WHERE tender_id=?', (now, tender_id))
            return 'same'
        conn.execute('''UPDATE tenders SET title=?, status=?, category=?, procurement_type=?,
            purchasing_agent=?, purchasing_unit=?, published_at=?, updated_at=?, closes_at=?,
            amendment_count=?, supplier=?, amount=?, source=?, source_url=?,
            last_seen_at=?, last_changed_at=?, fingerprint=?, raw_json=? WHERE tender_id=?''',
            (*base, now, now, fingerprint, payload, tender_id))
        return 'updated'
    conn.execute('''INSERT INTO tenders(tender_id, title, status, category, procurement_type,
        purchasing_agent, purchasing_unit, published_at, updated_at, closes_at,
        amendment_count, supplier, amount, source, source_url, first_seen_at,
        last_seen_at, last_changed_at, fingerprint, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (tender_id, *base, now, now, now, fingerprint, payload))
    return 'inserted'


def ingest_files(conn: sqlite3.Connection, files: list[Path], source: str, cutoff: str, to_date: str) -> dict:
    results = {'files': 0, 'observed': 0, 'inserted': 0, 'updated': 0, 'same': 0, 'skipped': 0, 'errors': []}
    started = utcnow()
    run_id = conn.execute('INSERT INTO runs(started_at, source) VALUES (?, ?)', (started, source)).lastrowid
    conn.commit()
    for path in files:
        try:
            raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            rows = read_rows(path)
            if not rows:
                raise ValueError('No rows in file.')
            with conn:
                for row in rows:
                    outcome = upsert(conn, row, source, cutoff, to_date)
                    results['observed'] += 1
                    results[outcome] += 1
                conn.execute('''INSERT OR IGNORE INTO source_files (sha256, filename, source, collected_at, row_count)
                    VALUES (?,?,?,?,?)''', (raw_hash, path.name, source, utcnow(), len(rows)))
                results['files'] += 1
            LOG.info('Ingested %s: %d source rows', path.name, len(rows))
        except Exception as exc:
            LOG.exception('Cannot ingest %s', path)
            results['errors'].append(f'{path}: {exc}')
    conn.execute('''UPDATE runs SET finished_at=?, files=?, observed=?, inserted=?,
        updated=?, errors=? WHERE run_id=?''',
        (utcnow(), results['files'], results['observed'], results['inserted'],
         results['updated'], json.dumps(results['errors']), run_id))
    conn.commit()
    return results


def export_csv(conn: sqlite3.Connection, outpath: Path) -> int:
    outpath.parent.mkdir(parents=True, exist_ok=True)
    cols = ['tender_id', 'title', 'status', 'category', 'procurement_type', 'purchasing_agent',
            'purchasing_unit', 'published_at', 'updated_at', 'closes_at',
            'amendment_count', 'supplier', 'amount', 'source', 'source_url',
            'first_seen_at', 'last_seen_at', 'last_changed_at']
    count = 0
    with outpath.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for record in conn.execute(f'SELECT {", ".join(cols)} FROM tenders ORDER BY published_at DESC, tender_id'):
            writer.writerow(dict(record))
            count += 1
    return count


async def scrape_export(download_dir: Path, headless: bool, delay: float, inspect_only: bool) -> list[Path]:
    """Use public SLI UI's Excel export; no undocumented API or auth bypass.

    This entry point has NOT been live-tested in this environment because remote
    browser access to apps.pancanal.com is blocked. Run --inspect on a machine
    with Internet access to diagnose controls before unattended operation.
    """
    from playwright.async_api import async_playwright
    download_dir.mkdir(parents=True, exist_ok=True)
    files = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(accept_downloads=True, locale='es-PA', timezone_id='America/Panama')
        page = await context.new_page()
        await page.goto(BASE_URL, wait_until='domcontentloaded', timeout=90000)
        await page.wait_for_timeout(3000)
        if inspect_only:
            # Does not expose credentials/cookies. A browser screenshot + form inventory.
            await page.screenshot(path=str(download_dir / 'portal-inspection.png'), full_page=True)
            forms = await page.evaluate('''() => ({url: location.href,
                selects: [...document.querySelectorAll('select')].map((e,i)=>({i,options:[...e.options].map(x=>x.textContent.trim())})),
                buttons: [...document.querySelectorAll('button,input[type=button],input[type=submit],a')]
                  .map(e=>({tag:e.tagName,text:(e.textContent||e.value||'').trim()})).filter(x=>x.text).slice(0,100)})''')
            (download_dir / 'portal-inspection.json').write_text(json.dumps(forms, ensure_ascii=False, indent=2), encoding='utf-8')
            LOG.info('Saved screenshot and form inventory to %s', download_dir)
            await browser.close()
            return []

        # Select 'all' if a native status selector exposes it, otherwise keep defaults.
        selects = page.locator('select')
        for i in range(await selects.count()):
            opts = await selects.nth(i).locator('option').all_text_contents()
            if any(re.search(r'(?i)abiertas|evaluaci[oó]n|adjudicaci[oó]n|estatus', v) for v in opts):
                idx = next((j for j, v in enumerate(opts) if re.search(r'(?i)desplegar todos|todos|all', v)), None)
                if idx is not None:
                    await selects.nth(i).select_option(index=idx)
                break
        search_btn = page.get_by_role('button', name=re.compile(r'desplegar resultado|show results', re.I))
        if await search_btn.count() == 0:
            search_btn = page.locator('input[value*="Desplegar resultado"],button:has-text("Desplegar resultado")')
        if not await search_btn.count():
            raise RuntimeError('SLI search button not found. Run collect --inspect and inspect page.')
        await search_btn.first.click()
        await page.wait_for_load_state('domcontentloaded', timeout=60000)
        await page.wait_for_timeout(2000)
        excel = page.get_by_text(re.compile(r'^\s*Excel\s*$', re.I)).first
        if not await excel.count():
            await page.screenshot(path=str(download_dir / 'no-excel.png'), full_page=True)
            raise RuntimeError('No Excel export control found after search. Inspect no-excel.png.')
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        async with page.expect_download(timeout=90000) as future_download:
            await excel.click()
        dl = await future_download.value
        suffix = Path(dl.suggested_filename).suffix.lower() or '.xlsx'
        target = download_dir / f'acp_sli_{timestamp}{suffix}'
        await dl.save_as(str(target))
        LOG.info('Downloaded %s (%d bytes)', target, target.stat().st_size)
        files.append(target)
        await page.wait_for_timeout(max(5.0, delay) * 1000)
        await browser.close()
    return files


def summary(conn: sqlite3.Connection) -> dict:
    n = conn.execute('SELECT COUNT(*) FROM tenders').fetchone()[0]
    versions = conn.execute('SELECT COUNT(*) FROM tender_versions').fetchone()[0]
    statuses = {r[0] or '(unspecified)': r[1] for r in conn.execute('SELECT status, COUNT(*) FROM tenders GROUP BY status ORDER BY 2 DESC')}
    range_ = conn.execute('SELECT MIN(published_at), MAX(published_at) FROM tenders WHERE published_at != ""').fetchone()
    return {'tenders': n, 'versions': versions, 'published_range': list(range_), 'statuses': statuses}


def main() -> int:
    parser = argparse.ArgumentParser(description='ACP SLI procurement intelligence collector')
    parser.add_argument('--db', type=Path, default=Path('data/procurements.sqlite'))
    parser.add_argument('--start', default='2023-11-10', help='Inclusive publication date YYYY-MM-DD')
    parser.add_argument('--end', default=date.today().isoformat(), help='Inclusive end YYYY-MM-DD')
    sub = parser.add_subparsers(dest='cmd', required=True)
    collect = sub.add_parser('collect', help='Use SLI public UI to download Excel and ingest')
    collect.add_argument('--download-dir', type=Path, default=Path('data/downloads'))
    collect.add_argument('--headed', action='store_true')
    collect.add_argument('--inspect', action='store_true', help='Only screenshot and inventory portal form')
    collect.add_argument('--delay', type=float, default=8.0)
    ingest = sub.add_parser('ingest', help='Import ACP Excel, CSV or JSON files')
    ingest.add_argument('files', type=Path, nargs='+')
    ingest.add_argument('--source', default='ACP historical export')
    sub.add_parser('stats', help='Show coverage and history')
    export = sub.add_parser('export', help='Export normalized procurement CSV')
    export.add_argument('--out', type=Path, default=Path('data/procurements.csv'))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.start) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.end):
        parser.error('Dates must be YYYY-MM-DD')
    if args.start > args.end:
        parser.error('--start must be earlier than --end')
    if args.cmd == 'collect':
        try:
            files = asyncio.run(scrape_export(args.download_dir, not args.headed, args.delay, args.inspect))
        except Exception:
            LOG.exception('Public SLI collection failed (no successful retrieval will be reported)')
            return 1
        if args.inspect:
            return 0
        if not files:
            LOG.error('No downloaded exports; aborting.')
            return 1
        with connect(args.db) as conn:
            stats = ingest_files(conn, files, 'ACP SLI public Excel export', args.start, args.end)
            LOG.info('Ingestion: %s', stats)
            if stats['errors'] or not stats['files']:
                return 1
            LOG.info('Database: %s', summary(conn))
    elif args.cmd == 'ingest':
        with connect(args.db) as conn:
            stats = ingest_files(conn, args.files, args.source, args.start, args.end)
            print(json.dumps(stats, indent=2))
            if stats['errors'] or not stats['files']:
                return 1
    elif args.cmd == 'stats':
        with connect(args.db) as conn:
            print(json.dumps(summary(conn), ensure_ascii=False, indent=2))
    elif args.cmd == 'export':
        with connect(args.db) as conn:
            print(f'Exported {export_csv(conn, args.out)} records to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
