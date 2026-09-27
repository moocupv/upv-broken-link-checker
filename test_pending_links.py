"""Regression tests for persistent link retries, with no network or SMTP."""
import configparser
import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import broken_links_checker as checker
from bs4 import BeautifulSoup


class PendingLinksTest(unittest.TestCase):
    def test_uncertain_outgoing_link_does_not_repeat_page(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(Path(__file__).with_name("config.ini.example"))
            cfg.set("paths", "database", str(base / "state.sqlite3"))
            cfg.set("paths", "reports", str(base / "reports"))
            cfg.set("crawl", "level_schedule", "0:1")
            cfg.set("crawl", "allow_private_hosts", "yes")
            cfg.set("crawl", "start_url", "http://localhost/")
            cfg.set("crawl", "min_interval_seconds", "0")
            cfg.set("crawl", "min_global_interval_seconds", "0")
            with (base / "config.ini").open("w") as out:
                cfg.write(out)

            def run(result):
                with patch.object(sys, "argv", ["checker", "--config", str(base / "config.ini")]), \
                     patch.object(checker.Auditor, "allowed", return_value=True), \
                     patch.object(checker.Auditor, "fetch", return_value=(
                         "ok", "HTTP 200", '<nav class="mobile-menu" aria-hidden="true"><a href="/unstable">Unstable</a></nav>',
                         "http://localhost/")) as fetched, \
                     patch.object(checker.Auditor, "check", return_value=result) as checked:
                    checker.main()
                    return checked.call_count, fetched.call_count

            self.assertEqual(run(("unknown", "HTTP 503", None)), (1, 1))
            with sqlite3.connect(base / "state.sqlite3") as db:
                self.assertEqual(db.execute("SELECT completed_round,retry_at FROM pages WHERE level=0").fetchone(), (1, None))
                self.assertEqual(db.execute("SELECT url FROM pending_links").fetchone()[0], "http://localhost/unstable")
                self.assertEqual(db.execute("SELECT context FROM links").fetchone()[0],
                                 "aria_hidden;indicio_movil;navegacion")
                db.execute("UPDATE pending_links SET retry_at='2000-01-01T00:00:00+00:00'")

            self.assertEqual(run(("broken", "HTTP 404", None)), (1, 0))
            with sqlite3.connect(base / "state.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_links").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT completed_round FROM pages WHERE level=0").fetchone()[0], 1)
            reports = sorted(p for p in (base / "reports").glob("*.csv") if not p.name.endswith("_agrupados.csv"))
            with reports[-1].open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pagina_origen"], "http://localhost/")
            self.assertEqual(rows[0]["enlace_roto"], "http://localhost/unstable")
            self.assertEqual(rows[0]["texto_ancla"], "Unstable")
            self.assertEqual(rows[0]["contexto_html"], "aria_hidden;indicio_movil;navegacion")
            with reports[-1].with_name(reports[-1].stem + "_agrupados.csv").open(encoding="utf-8-sig", newline="") as file:
                grouped = list(csv.DictReader(file))
            self.assertEqual(grouped[0]["paginas_afectadas"], "1")
            self.assertIn("indicio_movil", grouped[0]["contextos_html"])

    def test_group_by_exact_destination_with_multiple_origins(self):
        rows = {(2, "2026-09-27T10:00:00+02:00", source, "https://example.org/missing", "Menu", "HTTP 404", context)
                for source, context in (("https://a.example/a", "navegacion;indicio_movil"),
                                        ("https://a.example/b", "pie"))}
        groups = checker.group_findings(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["rows"], 2)
        self.assertEqual(len(groups[0]["sources"]), 2)
        self.assertEqual(groups[0]["contexts"], {"navegacion", "indicio_movil", "pie"})

    def test_html_context_is_a_dom_clue(self):
        soup = BeautifulSoup('<footer><nav class="mobile-menu" aria-hidden="true">'
                             '<a href="/x"><img alt="Abrir página" src="/image.png"></a>'
                             '</nav></footer>', "html.parser")
        self.assertEqual(checker.html_context(soup.a), "navegacion;aria_hidden;indicio_movil")

    def test_upgrade_preserves_existing_links(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE links(source TEXT NOT NULL,target TEXT NOT NULL,anchor TEXT NOT NULL,PRIMARY KEY(source,target,anchor))")
                db.execute("INSERT INTO links VALUES('https://a.example/','https://b.example/','Anchor')")
                db.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("INSERT INTO schema_meta VALUES('level_origin_home0','1')")
            with checker.connect(path) as db:
                self.assertEqual(db.execute("SELECT source,target,anchor,context FROM links").fetchone(),
                                 ("https://a.example/", "https://b.example/", "Anchor", "sin_datos"))


if __name__ == "__main__":
    unittest.main()
