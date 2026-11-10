# ACP Procurement Intelligence Agent — prototype

A deployable Python collector and SQLite database for public records from the **Autoridad del Canal de Panamá (ACP) Sistema de Licitaciones por Internet (SLI)**.

**Project status (2026-11-10):** Database ingestion, date filtering, versioning, deduplication, and CSV export were tested using **synthetic test data only**. The public-portal browser download has **not** been live-tested because the build environment cannot establish a browser/network connection to `apps.pancanal.com`. The script does not claim to contain any real ACP tender records yet.

## Scope and limits

- Start-of-archive: **2023-11-10** (three years before 2026-11-10).
- Records collected from publicly accessible SLI result listings via its **Excel** export, if the site permits browser automation without login or anti-bot restrictions.
- Structured fields: tender ID, description/title, status, category, procurement type, purchasing agent and unit, publication/closing/revision dates, amendments, supplier and amount **when included in the source**, source URL, ingestion timestamps.
- Stable tender ID is the primary key. Changes are preserved in `tender_versions`; unchanged repeated snapshots do not create duplicate versions.
- Daily automation included via GitHub Actions, scheduled for **08:00 Panama time**. It persists `data/procurements.sqlite` and `data/procurements.csv` in a GitHub repo, once deployed and enabled.
- Raw Excel downloads are saved as GitHub Actions artifacts for **30 days**, not as a permanent archive. For legal-grade original files, configure a separate object-storage archive.
- **Important:** The SLI's official manual says some tender information is visible only for a few days after closing or award (awarded items about 30 days). Therefore, downloading current results **cannot prove completeness** for 2023–2026. Historical completeness will require ACP archival datasets or a formal records request, reconciliation of item counts by month, and possibly a second source.
- This version does **not** download tender attachments/PDFs, scrape all award-edict details, or guarantee the Excel result export covers every status or every result page. Those are separate integration tasks, requiring live validation.

Official links:
- [SLI public portal](https://apps.pancanal.com/sli/LicitacionesBusqueda/Welcome)
- [ACP SLI user guide](https://pancanal.com/manual-de-usuario/)
- [ACP procurement awards / edictos](https://apps.pancanal.com/sli/Departamental/Award)
- [ACP transparency](https://pancanal.com/transparencia/)
- [ACP information requests](https://pancanal.com/solicitudes/)

## Files

```text
agent.py                            Python collector, parser, SQLite storage
requirements.txt                    Browser + Excel reader dependencies
.github/workflows/daily.yml         GitHub Actions daily 08:00 Panama scheduler
tests/sample_tenders.csv            SYNTHETIC FAKE test records, not real tenders
tests/test_agent.py                 Local ingestion/deduplication regression tests
data/                               DB and CSV destination after first successful run
```

## Quick start (macOS / Linux / Windows with Python 3.12)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

# Step 1: Inspect the live SLI interface, do NOT download anything
python agent.py collect --inspect --headed
# Review data/downloads/portal-inspection.json and portal-inspection.png.

# Step 2: Execute live public Excel export (requires selectors to match live site)
python agent.py collect --headed

# Step 3: Inspect the coverage and export to CSV
python agent.py stats
python agent.py export
```

For **historical ACP exports** obtained separately, use:

```bash
python agent.py ingest /path/to/ACP_2024.xlsx /path/to/ACP_2025.xls --source 'ACP historical export'
python agent.py stats
python agent.py export
```

Global flags `--db`, `--start`, and `--end` must precede the subcommand, e.g.:

```bash
python agent.py --start 2023-11-10 --end 2026-11-10 ingest /path/to/archive.csv
```

## Deploy as recurring GitHub Action (browser only, no local Python required)

1. Create a **private GitHub repository** named `acp-procurement-agent`, with no starter README, license or `.gitignore`.
2. Unzip the project and upload the **contents inside** `acp_procurement_agent/` to the repository root (`agent.py`, `requirements.txt`, `README.md`, `data/`, `tests/`, `.github/`, `.gitignore`). Do not put a second `acp_procurement_agent/` folder around them. On macOS Finder, press `Command+Shift+.` to show hidden items like `.github` before selecting. At the repository root verify that `.github/workflows/daily.yml` exists.
3. Under **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, then **Save**. GitHub organization policies may override this.
4. Go to **Actions → ACP procurement daily sync → Run workflow** and select **inspect**, then run. After the run, open the run Summary and download the artifact `acp-sli-results-<run number>` to see `portal-inspection.png` and `portal-inspection.json`. Inspect mode does not collect tenders or change the database.
5. If inspection confirms the page loaded, run the workflow again with **collect**. Inspect the logs of the Collect, Create searchable CSV, and Show collected coverage steps. Successful execution does not itself establish historical completeness.
6. Confirm that `data/procurements.sqlite` and `data/procurements.csv` were updated in the repository. After that, GitHub Actions schedules a daily sync around **08:00 Panama time** (scheduled executions may be delayed).

The live portal UI **has not yet been tested**; if its search/export controls differ from those assumed in `scrape_export()`, the Collect step will fail rather than silently invent records. Use `portal-inspection.json` and the screenshot to adjust the selectors in `agent.py` before relying on the daily workflow. Raw Excel downloads and inspection screenshots are kept as workflow artifacts for 30 days.

If you prefer local diagnostics on macOS:

```bash
python agent.py collect --inspect --headed
python agent.py collect --headed
python agent.py stats
```

Do not treat an empty or partial result as evidence that ACP had no procurements. Bulk historical exports should be obtained directly from ACP. Automated collection should respect ACP terms, site load and access restrictions.

## Historical completeness work plan

**Phase A — live feed:** Capture currently visible SLI tenders and use daily collection to preserve status changes (subject to browser/Excel live validation).

**Phase B — backfill:** Obtain an official historical extract from ACP for **2023-11-10 through 2026-11-10**, ideally all procurement procedures, tender revisions, cancellations, purchase orders, awards, suppliers and dates. Check ACP's transparency pages and request the archival dataset via the official ACP information request route. Ingest any Excel/CSV/JSON received with `agent.py ingest`.

**Phase C — reconcile:** Count unique procedures **by month, year, status, and source**, compare them with ACP totals, and record missing intervals as coverage gaps. Do not report the archive as complete until independently reconciled.

**Phase D — enrich:** Add ACP Edictos awards, document attachment downloads, structured RFI market-sounding notices, and optional targeted alerts for robotics, underwater inspection, ICT, security systems, etc.

## Database

- `tenders`: latest normalized procurement record keyed by `tender_id`.
- `tender_versions`: every distinct normalized version, first observed timestamp and provenance.
- `source_files`: SHA-256 of imported raw export files and import times.
- `runs`: successful/failed batch activity counters and errors.

SQLite is suitable for an initial archive. For multi-user production use, migrate to PostgreSQL and store original Excel/PDF files in versioned S3-compatible storage.

## Tests

```bash
python -m unittest discover -s tests -v
```

**Test fixture warning:** `tests/sample_tenders.csv` contains invented IDs and invented items solely for exercising the parser. It contains **no evidence** of ACP purchasing those items.
