import sqlite3
import tempfile
import unittest
from pathlib import Path
from agent import connect, ingest_files, export_csv, summary

SAMPLE = Path(__file__).parent / 'sample_tenders.csv'

class TestACPAgent(unittest.TestCase):
    def test_import_and_deduplication(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'db.sqlite'
            out = Path(td) / 'out.csv'
            with connect(db) as conn:
                stats = ingest_files(conn, [SAMPLE], 'synthetic test', '2023-11-10', '2026-11-10')
                self.assertEqual(stats['inserted'], 2)
                self.assertEqual(stats['skipped'], 1)
                self.assertEqual(summary(conn)['tenders'], 2)
                self.assertEqual(summary(conn)['versions'], 2)
                again = ingest_files(conn, [SAMPLE], 'synthetic test', '2023-11-10', '2026-11-10')
                self.assertEqual(again['inserted'], 0)
                self.assertEqual(again['updated'], 0)
                self.assertEqual(summary(conn)['versions'], 2)
                self.assertEqual(export_csv(conn, out), 2)
                self.assertTrue(out.exists())

if __name__ == '__main__':
    unittest.main()

class TestLegacyExcelHTML(unittest.TestCase):
    def test_html_disguised_as_xls(self):
        from agent import read_rows
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'legacy.xls'
            path.write_text('<html><table><tr><th>Licitación</th><th>Descripción</th><th>Fecha de publicación</th></tr><tr><td>210005</td><td>Test item</td><td>10-nov-2026</td></tr></table></html>')
            self.assertEqual(read_rows(path)[0]['tender_id'], '210005')

    def test_status_change_creates_version(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'db.sqlite'
            with connect(db) as conn:
                ingest_files(conn, [SAMPLE], 'synthetic test', '2023-11-10', '2026-11-10')
                from agent import upsert
                result = upsert(conn, {'tender_id':'209900','status':'ADJUDICADA'},
                                'ACP SLI public Excel export', '2023-11-10', '2026-11-10')
                self.assertEqual(result, 'updated')
                self.assertEqual(summary(conn)['versions'], 3)
                self.assertEqual(conn.execute("SELECT title FROM tenders WHERE tender_id='209900'").fetchone()[0],
                                 'ROV REMOTO PARA INSPECCIÓN SUBACUÁTICA')
