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
            cfg.set("crawl", "start_url", "https://www.upv.es/")
            cfg.set("crawl", "min_interval_seconds", "0")
            cfg.set("crawl", "min_global_interval_seconds", "0")
            with (base / "config.ini").open("w") as out:
                cfg.write(out)

            def run(result):
                with patch.object(sys, "argv", ["checker", "--config", str(base / "config.ini")]), \
                     patch.object(checker.Auditor, "allowed", return_value=True), \
                     patch.object(checker.Auditor, "fetch", return_value=(
                         "ok", "HTTP 200", '<nav class="mobile-menu" aria-hidden="true"><a href="/unstable">Unstable</a></nav>',
                         "https://www.upv.es/")) as fetched, \
                     patch.object(checker.Auditor, "check", return_value=result) as checked:
                    checker.main()
                    return checked.call_count, fetched.call_count

            self.assertEqual(run(("unknown", "HTTP 503", None)), (1, 1))
            with sqlite3.connect(base / "state.sqlite3") as db:
                self.assertEqual(db.execute("SELECT completed_round,retry_at FROM pages WHERE level=0").fetchone(), (1, None))
                self.assertEqual(db.execute("SELECT url FROM pending_links").fetchone()[0], "https://www.upv.es/unstable")
                self.assertEqual(db.execute("SELECT context FROM links").fetchone()[0],
                                 "aria_hidden;indicio_movil;navegacion")
                db.execute("UPDATE pending_links SET retry_at='2000-01-01T00:00:00+00:00'")

            self.assertEqual(run(("broken", "HTTP 404", None)), (1, 0))
            with sqlite3.connect(base / "state.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_links").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT completed_round FROM pages WHERE level=0").fetchone()[0], 1)
            reports = sorted(p for p in (base / "reports").glob("*.csv") if not p.name.endswith("_agrupados.csv"))
            with reports[-1].open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file, delimiter=";"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pagina_origen"], "https://www.upv.es/")
            self.assertEqual(rows[0]["enlace_roto"], "https://www.upv.es/unstable")
            self.assertEqual(rows[0]["texto_ancla"], "Unstable")
            self.assertEqual(rows[0]["contexto_html"], "aria_hidden;indicio_movil;navegacion")
            with reports[-1].with_name(reports[-1].stem + "_agrupados.csv").open(encoding="utf-8-sig", newline="") as file:
                grouped = list(csv.DictReader(file, delimiter=";"))
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

    def test_csv_format_default_and_english(self):
        cfg = configparser.ConfigParser()
        self.assertEqual(checker.csv_delimiter(cfg), ";")
        cfg.add_section("report")
        cfg.set("report", "csv_format", "en")
        self.assertEqual(checker.csv_delimiter(cfg), ",")
        groups = checker.group_findings({(2, "2026-09-27T10:00:00+02:00", "https://a.example/",
                                          "https://b.example/a,b", "A,B", "HTTP 404", "navegacion")})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "groups.csv"
            checker.write_grouped_report(path, groups, checker.csv_delimiter(cfg))
            with path.open(encoding="utf-8-sig", newline="") as file:
                row = list(csv.DictReader(file, delimiter=","))[0]
            self.assertEqual(row["enlace_roto"], "https://b.example/a,b")
            self.assertEqual(row["textos_ancla"], "A,B")
        cfg.set("report", "csv_format", "xyz")
        with self.assertRaises(ValueError):
            checker.csv_delimiter(cfg)

    def test_html_context_is_a_dom_clue(self):
        soup = BeautifulSoup('<footer><nav class="mobile-menu" aria-hidden="true">'
                             '<a href="/x"><img alt="Abrir página" src="/image.png"></a>'
                             '</nav></footer>', "html.parser")
        self.assertEqual(checker.html_context(soup.a), "navegacion;aria_hidden;indicio_movil")

    def test_external_links_are_checked_but_not_enqueued(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(Path(__file__).with_name("config.ini.example"))
            cfg.set("crawl", "level_schedule", "0:1;1:1")
            with checker.connect(Path(temp) / "state.sqlite3") as db:
                auditor = checker.Auditor(cfg, db)
                with patch.object(auditor, "allowed", return_value=True), \
                     patch.object(auditor, "fetch", return_value=(
                         "ok", "HTTP 200", '<a href="https://apps.apple.com/oferta">Apple</a>'
                         '<a href="https://dept.upv.es/info">UPV</a>', "https://www.upv.es/")), \
                     patch.object(auditor, "check", side_effect=lambda url, ttl: (
                         ("broken", "HTTP 404", None) if "apple.com" in url else ("ok", "HTTP 200", None))):
                    broken, complete = auditor.process_page("https://www.upv.es/", 0, 1)
                self.assertTrue(complete)
                self.assertEqual(len(broken), 1)
                self.assertEqual(broken[0][3], "https://apps.apple.com/oferta")
                self.assertEqual([row[0] for row in db.execute("SELECT url FROM pages")],
                                 ["https://dept.upv.es/info"])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM links").fetchone()[0], 2)

    def test_external_response_body_is_not_parsed(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def iter_content(self, **kwargs):
                raise AssertionError("No se debe descargar el HTML externo")

        with tempfile.TemporaryDirectory() as temp:
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(Path(__file__).with_name("config.ini.example"))
            with checker.connect(Path(temp) / "state.sqlite3") as db:
                auditor = checker.Auditor(cfg, db)
                with patch.object(auditor, "request", return_value=(Response(), None)):
                    self.assertEqual(auditor.fetch("https://apps.apple.com/oferta"),
                                     ("ok", "HTTP 200", None, "https://apps.apple.com/oferta"))

    def test_domain_scope_and_existing_database_migration(self):
        self.assertTrue(checker.upv_page("https://upv.es/"))
        self.assertTrue(checker.upv_page("https://dept.upv.es/a"))
        self.assertFalse(checker.upv_page("https://notupv.es/a"))
        self.assertFalse(checker.upv_page("https://upv.es.evil.example/a"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            with checker.connect(path) as db:
                db.execute("INSERT INTO pages(url,level) VALUES('https://www.upv.es/',0)")
                db.execute("INSERT INTO pages(url,level) VALUES('https://apps.apple.com/oferta',1)")
                db.execute("INSERT INTO links(source,target,anchor,context) VALUES(?,?,?,'sin_datos')",
                           ("https://www.upv.es/", "https://apps.apple.com/oferta", "Apple"))
                db.execute("INSERT INTO links(source,target,anchor,context) VALUES(?,?,?,'sin_datos')",
                           ("https://apps.apple.com/oferta", "https://example.com/", "Outside"))
                db.execute("INSERT INTO checks VALUES(?,?,?,?)",
                           ("https://apps.apple.com/oferta", checker.now(), "broken", "HTTP 404"))
                db.execute("INSERT INTO pending_links VALUES(?,?,?)",
                           ("https://apps.apple.com/oferta", checker.now(), "temporary"))
                db.execute("DELETE FROM schema_meta WHERE key='upv_only_page_scope_v1'")
            with checker.connect(path) as db:
                self.assertEqual(db.execute("SELECT url FROM pages").fetchall(), [("https://www.upv.es/",)])
                self.assertEqual(db.execute("SELECT source,target FROM links").fetchall(),
                                 [("https://www.upv.es/", "https://apps.apple.com/oferta")])
                self.assertEqual(db.execute("SELECT result FROM checks").fetchone()[0], "broken")
                self.assertEqual(db.execute("SELECT url FROM pending_links").fetchone()[0],
                                 "https://apps.apple.com/oferta")

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

    def test_backfill_updates_only_context_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(Path(__file__).with_name("config.ini.example"))
            cfg.set("paths", "database", str(base / "state.sqlite3"))
            cfg.set("paths", "reports", str(base / "reports"))
            cfg.set("crawl", "level_schedule", "0:1;1:1")
            cfg.set("crawl", "allow_private_hosts", "yes")
            with (base / "config.ini").open("w") as out:
                cfg.write(out)
            url = "https://www.upv.es/old"
            target = "https://www.upv.es/missing"
            with checker.connect(base / "state.sqlite3") as db:
                db.execute("INSERT INTO pages(url,level,completed_round) VALUES(?,1,1)", (url,))
                db.execute("INSERT INTO links(source,target,anchor,context) VALUES(?,?,?,'sin_datos')",
                           (url, target, "Old"))
                db.execute("INSERT INTO checks VALUES(?,?,?,?)", (target, checker.now(), "broken", "HTTP 404"))
                db.execute("INSERT INTO pending_links VALUES(?,?,?)", (target, checker.now(), "temporary"))
                db.execute("INSERT INTO level_rounds(level,round_number,active,started_at) VALUES(1,1,1,?)",
                           (checker.now(),))

            def run():
                with patch.object(sys, "argv", ["checker", "--config", str(base / "config.ini"), "--backfill-context"]), \
                     patch.object(checker.Auditor, "allowed", return_value=True), \
                     patch.object(checker.Auditor, "fetch", return_value=(
                         "ok", "HTTP 200", '<nav class="mobile-menu"><a href="/missing">Old</a></nav>',
                         url)) as fetch, \
                     patch.object(checker.Auditor, "check") as check:
                    self.assertEqual(checker.main(), 0)
                    check.assert_not_called()
                    return fetch.call_count

            self.assertEqual(run(), 1)
            self.assertEqual(run(), 0)
            with sqlite3.connect(base / "state.sqlite3") as db:
                self.assertEqual(db.execute("SELECT context FROM links").fetchone()[0],
                                 "indicio_movil;navegacion")
                self.assertEqual(db.execute("SELECT level,completed_round FROM pages").fetchone(), (1, 1))
                self.assertEqual(db.execute("SELECT round_number,active FROM level_rounds WHERE level=1").fetchone(), (1, 1))
                self.assertEqual(db.execute("SELECT result,detail FROM checks").fetchone(), ("broken", "HTTP 404"))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_links").fetchone()[0], 1)
            reports = sorted((base / "reports").glob("contexto_pendiente_*.csv"))
            with reports[0].open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(list(csv.DictReader(file, delimiter=";"))[0]["enlaces_con_contexto"], "1")


if __name__ == "__main__":
    unittest.main()
