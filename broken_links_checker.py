#!/usr/bin/env python3
"""Incremental, robots-aware link audit. Run periodically with cron."""
import argparse
import configparser
import csv
import fcntl
import hashlib
import ipaddress
import logging
import os
import re
import smtplib
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, UnicodeDammit

LOG = logging.getLogger("linkcheck")
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "blob:")
NON_HTML = re.compile(r"\.(?:pdf|zip|gz|jpg|jpeg|png|gif|svg|webp|mp4|mp3|docx?|xlsx?|pptx?|css|js|xml|json|ics|woff2?)$", re.I)
UA = "UPV-BrokenLinks-Audit/1.0 (+mailto:webmaster@upv.es)"


class RunLimit(Exception):
    """Stop cleanly when the daily time budget runs out."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(raw, base=None):
    if not raw or raw.strip().lower().startswith(SKIP_SCHEMES):
        return None
    raw = urljoin(base, raw.strip()) if base else raw.strip()
    raw, _ = urldefrag(raw)
    p = urlsplit(raw)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    try:
        port = p.port
    except ValueError:
        return None
    host = p.hostname.lower()
    if host == "upv.es":
        host = "www.upv.es"
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port and port != (443 if p.scheme.lower() == "https" else 80) else "")
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", p.query, ""))


def crawlable(url):
    p = urlsplit(url)
    return not NON_HTML.search(p.path)


def upv_page(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "upv.es" or host.endswith(".upv.es")


def parse_level_schedule(value):
    """0:1;1:1;2:1;3:4 sets the minimum days between starts by depth."""
    result = {}
    previous_days = 0
    for item in value.split(";"):
        parts = item.strip().split(":")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            raise ValueError("level_schedule debe tener el formato 0:1;1:1;2:1;3:4")
        level, days = (int(part.strip()) for part in parts)
        if level in result or not (0 <= level <= 20 and 1 <= days <= 365):
            raise ValueError("Niveles únicos entre 0 y 20 y días entre 1 y 365")
        result[level] = days
    if sorted(result) != list(range(len(result))):
        raise ValueError("Los niveles deben ser consecutivos desde 0")
    for level in sorted(result):
        if result[level] < previous_days:
            LOG.warning("Nivel %s: %s días se elevan a %s para respetar la frecuencia del nivel anterior",
                        level, result[level], previous_days)
            result[level] = previous_days
        previous_days = result[level]
    return result


def public_host(url):
    host = urlsplit(url).hostname or ""
    if host.lower() == "localhost" or host.lower().endswith(".localhost") or host.lower().endswith(".local"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def connect(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, checked_at TEXT, next_due TEXT, priority INTEGER NOT NULL DEFAULT 0,
            level INTEGER, completed_round INTEGER NOT NULL DEFAULT 0, retry_at TEXT,
            last_completed_execution TEXT
        );
        CREATE TABLE IF NOT EXISTS links (
            source TEXT NOT NULL, target TEXT NOT NULL, anchor TEXT NOT NULL,
            PRIMARY KEY(source, target, anchor)
        );
        CREATE TABLE IF NOT EXISTS checks (
            url TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
            result TEXT NOT NULL, detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pending_links (
            url TEXT PRIMARY KEY, retry_at TEXT NOT NULL,
            detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_backfill (
            source TEXT PRIMARY KEY, attempted_at TEXT NOT NULL,
            retry_at TEXT, result TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pending_links_due ON pending_links(retry_at);
        CREATE INDEX IF NOT EXISTS pages_due ON pages(next_due, priority);
        CREATE INDEX IF NOT EXISTS links_target ON links(target);
        CREATE TABLE IF NOT EXISTS level_rounds (
            level INTEGER PRIMARY KEY, round_number INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 0, next_start_at TEXT, started_at TEXT
        );
        CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    # Preserve an existing SQLite database created by earlier script versions.
    columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
    for name, declaration in (("level", "INTEGER"), ("completed_round", "INTEGER NOT NULL DEFAULT 0"),
                              ("retry_at", "TEXT"), ("last_completed_execution", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE pages ADD COLUMN {name} {declaration}")
    if "context" not in {row[1] for row in db.execute("PRAGMA table_info(links)")}:
        db.execute("ALTER TABLE links ADD COLUMN context TEXT NOT NULL DEFAULT 'sin_datos'")
    round_columns = {row[1] for row in db.execute("PRAGMA table_info(level_rounds)")}
    if "started_at" not in round_columns:
        db.execute("ALTER TABLE level_rounds ADD COLUMN started_at TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS pages_round ON pages(level, completed_round, retry_at)")
    if not db.execute("SELECT 1 FROM schema_meta WHERE key='level_origin_home0'").fetchone():
        # Previous versions used both home and map as level-1 roots. Their
        # distances cannot be shifted: recompute reachability from home.
        db.execute("UPDATE pages SET level=NULL,completed_round=0,retry_at=NULL")
        db.execute("DELETE FROM level_rounds")
        db.execute("INSERT INTO schema_meta(key,value) VALUES('level_origin_home0','1')")
        db.commit()
    if not db.execute("SELECT 1 FROM schema_meta WHERE key='upv_only_page_scope_v1'").fetchone():
        # Keep external targets and their cached checks, but remove external
        # source pages and their graphs from the old all-domain crawl.
        external = [url for (url,) in db.execute("SELECT url FROM pages") if not upv_page(url)]
        db.executemany("DELETE FROM links WHERE source=?", ((url,) for url in external))
        db.executemany("DELETE FROM context_backfill WHERE source=?", ((url,) for url in external))
        db.executemany("DELETE FROM pages WHERE url=?", ((url,) for url in external))
        db.execute("DELETE FROM pending_links WHERE NOT EXISTS (SELECT 1 FROM links WHERE target=pending_links.url)")
        db.execute("INSERT INTO schema_meta(key,value) VALUES('upv_only_page_scope_v1','1')")
        db.commit()
        if external:
            LOG.info("Eliminadas %s páginas externas de la cola y sus enlaces salientes", len(external))
    return db


def bind_root(db, root):
    """Never reuse a level graph whose distances came from another root."""
    row = db.execute("SELECT value FROM schema_meta WHERE key='crawl_root'").fetchone()
    if row:
        if row[0] != root:
            raise ValueError(f"La base pertenece a {row[0]}; usa --start-url para crear un estado independiente")
        return
    old_root = db.execute("SELECT url FROM pages WHERE level=0 LIMIT 1").fetchone()
    if old_root and old_root[0] != root:
        raise ValueError(f"La base existente parte de {old_root[0]}; usa --start-url para crear un estado independiente")
    db.execute("INSERT INTO schema_meta(key,value) VALUES('crawl_root',?)", (root,))
    db.commit()


def start_and_finish_rounds(db, schedule):
    """Complete exhausted rounds and start due rounds, including freshly discovered levels."""
    for level, days in schedule.items():
        db.execute("INSERT OR IGNORE INTO level_rounds(level) VALUES(?)", (level,))
        round_number, active, next_start, started_at = db.execute(
            "SELECT round_number,active,next_start_at,started_at FROM level_rounds WHERE level=?", (level,)).fetchone()
        if active and not db.execute(
            "SELECT 1 FROM pages WHERE level=? AND completed_round<? LIMIT 1", (level, round_number)).fetchone():
            next_start = max(datetime.fromisoformat(started_at) + timedelta(days=days),
                             datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            db.execute("UPDATE level_rounds SET active=0,next_start_at=? WHERE level=?", (next_start, level))
            active = 0
            LOG.info("Ronda %s del nivel %s completada; siguiente después de %s", round_number, level, next_start)
        if not active and (next_start is None or next_start <= now()) and db.execute(
            "SELECT 1 FROM pages WHERE level=? LIMIT 1", (level,)).fetchone():
            db.execute("UPDATE level_rounds SET round_number=round_number+1,active=1,started_at=? WHERE level=?", (now(), level))
            LOG.info("Ronda del nivel %s iniciada", level)


def prune_old_data(db, days, root):
    """Discard stale observations without deleting the active crawl frontier."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.execute("DELETE FROM checks WHERE checked_at < ?", (cutoff,))
    db.execute("""DELETE FROM pages WHERE url != ? AND checked_at IS NOT NULL AND checked_at < ?
        AND NOT EXISTS (SELECT 1 FROM level_rounds r WHERE r.level=pages.level
                        AND r.active=1)""", (root, cutoff))
    db.execute("DELETE FROM links WHERE source NOT IN (SELECT url FROM pages)")
    db.execute("DELETE FROM pending_links WHERE NOT EXISTS (SELECT 1 FROM links WHERE target=pending_links.url)")
    db.execute("DELETE FROM context_backfill WHERE source NOT IN (SELECT url FROM pages)")


def html_context(tag):
    """Record DOM clues, without claiming computed visibility at any viewport."""
    ancestors = [tag, *tag.parents]
    names = {node.name for node in ancestors if getattr(node, "name", None)}
    roles = {node.get("role", "").lower() for node in ancestors if getattr(node, "name", None)}
    if "nav" in names or "navigation" in roles:
        location = "navegacion"
    elif "footer" in names:
        location = "pie"
    elif "header" in names:
        location = "cabecera"
    elif "aside" in names:
        location = "lateral"
    else:
        location = "contenido"
    flags = set()
    for node in ancestors:
        if not getattr(node, "name", None):
            continue
        tokens = " ".join([str(node.get("id", "")), *node.get("class", [])]).lower()
        if re.search(r"(?:^|[\s_-])(?:mobile|movil|smartphone|hamburger)(?:$|[\s_-])", tokens):
            flags.add("indicio_movil")
        if node.has_attr("hidden") or node.has_attr("inert"):
            flags.add("oculto_html")
        if str(node.get("aria-hidden", "")).lower() == "true":
            flags.add("aria_hidden")
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node.get("style", ""), re.I):
            flags.add("oculto_inline")
    return ";".join([location, *sorted(flags)])


def extract_edges(html, final_url):
    """Map (destination, text) to the DOM clues observed for that link."""
    soup = BeautifulSoup(html, "html.parser")
    base = urljoin(final_url, soup.base.get("href", "")) if soup.base else final_url
    edges = defaultdict(set)
    for tag in soup.find_all("a", href=True):
        target = normalize(tag["href"], base)
        if target:
            anchor = (" ".join(tag.get_text(" ", strip=True).split())
                      or tag.get("aria-label") or tag.get("title")
                      or " ".join(img.get("alt", "") for img in tag.find_all("img"))).strip()[:500]
            edges[(target, anchor)].update(html_context(tag).split(";"))
    return edges


class Auditor:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.level_schedule = parse_level_schedule(cfg.get("crawl", "level_schedule"))
        self.retention_days = cfg.getint("crawl", "db_retention_days")
        if self.retention_days < max(self.level_schedule.values()):
            raise ValueError("db_retention_days no puede ser menor que el intervalo mayor de level_schedule")
        self.local_tz = ZoneInfo(cfg.get("crawl", "report_timezone", fallback="Europe/Madrid"))
        self.allow_private = cfg.getboolean("crawl", "allow_private_hosts", fallback=False)
        self.patterns = [re.compile(x.strip(), re.I) for x in cfg.get("crawl", "soft_404_patterns").splitlines() if x.strip()]
        self.timeout = cfg.getfloat("crawl", "timeout_seconds")
        self.max_bytes = cfg.getint("crawl", "max_html_bytes")
        self.agent = cfg.get("crawl", "user_agent", fallback=UA)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.agent})
        self.robots = {}
        self.unknown_cache = {}
        self.next_request = {}
        self.blocked_hosts = set()
        self.interval = cfg.getfloat("crawl", "min_interval_seconds", fallback=1.)
        self.global_interval = cfg.getfloat("crawl", "min_global_interval_seconds", fallback=0.2)
        self.next_global_request = 0.
        self.max_retry_wait = cfg.getfloat("crawl", "max_retry_wait_seconds", fallback=30.)
        self.deadline = time.monotonic() + cfg.getfloat("crawl", "max_duration_hours", fallback=20.) * 3600
        self.request_limit = cfg.getint("crawl", "max_http_requests_per_run", fallback=50000)
        self.stats = {"pages": 0, "checked": 0, "unknown": 0, "robots": 0, "throttled": 0,
                      "levels": set(), "source_levels": set()}

    def ensure_time(self):
        if time.monotonic() >= self.deadline:
            raise RunLimit("Tiempo máximo de ejecución alcanzado")
        if self.stats["checked"] >= self.request_limit:
            raise RunLimit("Límite de solicitudes HTTP alcanzado")

    def pause(self, seconds):
        self.ensure_time()
        if seconds > 0:
            if time.monotonic() + seconds >= self.deadline:
                raise RunLimit("Tiempo máximo de ejecución alcanzado")
            time.sleep(seconds)

    def retry_after(self, value, attempt):
        if value:
            try:
                return max(0., float(value))
            except ValueError:
                try:
                    return max(0., (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(60., 2. ** (attempt + 2))

    def request(self, url, stream=True):
        """One paced HTTP transaction, with bounded retries and a per-host circuit breaker."""
        if not self.allow_private and not public_host(url):
            return None, "host privado o local excluido"
        host = urlsplit(url).netloc.lower()
        if host in self.blocked_hosts:
            return None, "host pausado por throttling"
        for attempt in range(3):
            self.pause(max(0., self.next_request.get(host, 0.) - time.monotonic(),
                           self.next_global_request - time.monotonic()))
            # Space requests by host, including unsuccessful attempts and robots.txt.
            self.next_request[host] = time.monotonic() + self.interval
            self.next_global_request = time.monotonic() + self.global_interval
            self.ensure_time()
            # Count attempts, including redirects, retries, and network failures.
            self.stats["checked"] += 1
            try:
                response = self.session.get(url, timeout=self.timeout, stream=stream, allow_redirects=False)
            except requests.RequestException as e:
                return None, f"{type(e).__name__}: {str(e)[:180]}"
            if response.status_code not in (429, 503):
                return response, None
            self.stats["throttled"] += 1
            wait = self.retry_after(response.headers.get("Retry-After"), attempt)
            response.close()
            if wait > self.max_retry_wait or attempt == 2:
                self.blocked_hosts.add(host)
                LOG.warning("Host pausado hasta la siguiente ejecución: %s (HTTP 429/503)", host)
                return None, f"HTTP 429/503; host pausado (Retry-After: {wait:.0f}s)"
            self.pause(max(wait, self.interval))
        raise AssertionError("unreachable")

    def allowed(self, url):
        if not self.cfg.getboolean("crawl", "respect_robots_txt"):
            return True
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self.robots:
            robots_url = root + "/robots.txt"
            try:
                for _ in range(6):
                    response, error = self.request(robots_url, stream=False)
                    if response is not None and response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                        with response:
                            robots_url = normalize(response.headers["Location"], robots_url)
                        if not robots_url:
                            response = None
                            break
                        continue
                    break
                if response is None:
                    # A throttled host can recover on the next cron execution.
                    if urlsplit(robots_url).netloc.lower() not in self.blocked_hosts:
                        self.robots[root] = None
                    return False
                with response:
                    parser = RobotFileParser()
                    if 400 <= response.status_code < 500:
                        parser.parse([])  # RFC 9309: unavailable robots permits access.
                    else:
                        response.raise_for_status()
                        parser.parse(response.text.splitlines())
                    self.robots[root] = parser
            except requests.RequestException as e:
                LOG.warning("Robots no disponible %s: %s; se omite este host en esta ejecución", root, e)
                self.robots[root] = None
        parser = self.robots[root]
        return parser is not None and parser.can_fetch(self.agent, url)

    def fetch(self, url):
        """Return result, detail, HTML (or None). Never load entire binary resources."""
        try:
            for _ in range(6):
                response, error = self.request(url)
                if response is None:
                    return "unknown", error, None, url
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    with response:
                        url = normalize(response.headers["Location"], url)
                    if not url:
                        return "unknown", "redirección no HTTP", None, url
                    if not self.allowed(url):
                        return "unknown", "redirección excluida por robots.txt", None, url
                    continue
                break
            else:
                return "unknown", "demasiadas redirecciones", None, url
            with response:
                status = response.status_code
                if status in (401, 403, 408, 429) or status >= 500:
                    return "unknown", f"HTTP {status}", None, url
                if status >= 400:
                    return "broken", f"HTTP {status}", None, url
                if status < 200:
                    return "unknown", f"HTTP {status}", None, url
                if not upv_page(url):
                    # External targets are checked by status, never downloaded
                    # and parsed as pages (including UPV-to-external redirects).
                    return "ok", f"HTTP {status}", None, url
                mime = response.headers.get("Content-Type", "").lower()
                # Mislabelled responses with no Content-Type can still be HTML.
                is_html = "text/html" in mime or "application/xhtml+xml" in mime or (not mime and not NON_HTML.search(urlsplit(url).path))
                if not is_html:
                    return "ok", f"HTTP {status}", None, url
                data = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    data.extend(chunk[:max(0, self.max_bytes - len(data))])
                    if len(data) >= self.max_bytes:
                        break
                html = UnicodeDammit(bytes(data), is_html=True).unicode_markup or bytes(data).decode("utf-8", errors="replace")
                for pattern in self.patterns:
                    if pattern.search(html):
                        return "broken", "soft-404: " + pattern.pattern, None, url
                return "ok", f"HTTP {status}", html, url
        except requests.RequestException as e:
            return "unknown", f"{type(e).__name__}: {str(e)[:180]}", None, url

    def check(self, url, ttl_days, force=False):
        row = self.db.execute("SELECT checked_at,result,detail FROM checks WHERE url=?", (url,)).fetchone()
        if not force and row and datetime.fromisoformat(row[0]) > datetime.now(timezone.utc) - timedelta(days=ttl_days):
            return row[1], row[2], None
        if url in self.unknown_cache:
            return "unknown", self.unknown_cache[url], None
        if not self.allowed(url):
            self.stats["robots"] += 1
            return "unknown", "excluido por robots.txt", None
        result, detail, html, _ = self.fetch(url)
        if result == "unknown":
            self.stats["unknown"] += 1
            self.unknown_cache[url] = detail
        else:
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?,?)", (url, now(), result, detail))
        return result, detail, html

    def queue_uncertain_link(self, url, detail):
        retry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.db.execute("INSERT INTO pending_links(url,retry_at,detail) VALUES(?,?,?) "
                        "ON CONFLICT(url) DO UPDATE SET detail=excluded.detail",
                        (url, retry, detail))

    def enqueue(self, url, level):
        if level in self.level_schedule and upv_page(url) and crawlable(url) and (self.allow_private or public_host(url)):
            self.db.execute("""
                INSERT INTO pages(url,level) VALUES(?,?)
                ON CONFLICT(url) DO UPDATE SET
                    completed_round=CASE WHEN pages.level IS NULL OR pages.level>excluded.level THEN 0 ELSE pages.completed_round END,
                    retry_at=CASE WHEN pages.level IS NULL OR pages.level>excluded.level THEN NULL ELSE pages.retry_at END,
                    level=CASE WHEN pages.level IS NULL OR pages.level>excluded.level THEN excluded.level ELSE pages.level END
            """, (url, level))

    def process_page(self, url, level, link_ttl):
        # Fetch fresh even when link status is cached: the page's link graph may change.
        if not self.allowed(url):
            self.stats["robots"] += 1
            p = urlsplit(url)
            root = f"{p.scheme}://{p.netloc}"
            return [], self.robots.get(root) is not None and p.netloc.lower() not in self.blocked_hosts
        result, detail, html, final_url = self.fetch(url)
        if result == "unknown":
            self.stats["unknown"] += 1
            LOG.warning("Página no comprobable %s: %s", url, detail)
            return [], False  # Preserve the old graph and retry tomorrow.
        self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?,?)", (url, now(), result, detail))
        if result == "broken" or html is None:
            self.stats["pages"] += 1
            day = datetime.now(self.local_tz).date().isoformat()
            self.stats["levels"].add((level, day))
            self.stats["source_levels"].add((level, day, url))
            return [], True
        edges = extract_edges(html, final_url)
        self.db.execute("DELETE FROM links WHERE source=?", (url,))
        self.db.executemany("INSERT OR IGNORE INTO links(source,target,anchor,context) VALUES (?,?,?,?)",
                            [(url, target, anchor, ";".join(sorted(contexts)))
                             for (target, anchor), contexts in edges.items()])
        for target, _ in edges:
            self.enqueue(target, level + 1)
        broken = []
        for target in sorted({t for t, _ in edges}):
            if not self.allow_private and not public_host(target):
                continue
            if not self.allowed(target):
                self.stats["robots"] += 1
                p = urlsplit(target)
                root = f"{p.scheme}://{p.netloc}"
                if self.robots.get(root) is None:
                    self.queue_uncertain_link(target, "robots.txt temporalmente no disponible")
                continue
            r, d, _ = self.check(target, link_ttl)
            if r == "broken":
                stamp = datetime.now(self.local_tz).isoformat(timespec="seconds")
                broken.extend((level, stamp, url, target, anchor, d, ";".join(sorted(contexts)))
                              for (t, anchor), contexts in edges.items() if t == target)
            if r == "unknown":
                self.queue_uncertain_link(target, d)
            else:
                self.db.execute("DELETE FROM pending_links WHERE url=?", (target,))
        self.stats["pages"] += 1
        day = datetime.now(self.local_tz).date().isoformat()
        self.stats["levels"].add((level, day))
        self.stats["source_levels"].add((level, day, url))
        # An uncertain outgoing link does not invalidate the page's HTML/graph.
        # It has its own persistent retry queue instead.
        return broken, True

    def record_page(self, url, round_number, complete, run_token):
        retry = None if complete else (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.db.execute("""UPDATE pages SET checked_at=?,
            completed_round=CASE WHEN ? THEN ? ELSE completed_round END,
            retry_at=?, last_completed_execution=CASE WHEN ? THEN ? ELSE last_completed_execution END
            WHERE url=?""", (now(), int(complete), round_number, retry,
                             int(complete), run_token, url))


def group_findings(findings):
    groups = {}
    for level, stamp, source, target, anchor, detail, context in findings:
        key = (target, detail)
        if key not in groups:
            groups[key] = {"target": target, "detail": detail, "rows": 0,
                           "sources": set(), "anchors": set(), "contexts": set(),
                           "levels": set(), "days": set(), "hosts": set()}
        group = groups[key]
        group["rows"] += 1
        group["sources"].add(source)
        group["anchors"].add(anchor or "(sin texto)")
        group["contexts"].update(context.split(";"))
        group["levels"].add(level)
        group["days"].add(stamp[:10])
        group["hosts"].add(urlsplit(source).hostname or "")
    return sorted(groups.values(), key=lambda g: (-len(g["sources"]), -g["rows"], g["target"], g["detail"]))


def csv_delimiter(cfg):
    format_name = cfg.get("report", "csv_format", fallback="es").strip().lower()
    if format_name not in ("es", "en"):
        raise ValueError("report.csv_format debe ser es (separador ;) o en (separador ,)")
    return ";" if format_name == "es" else ","


def subdomain_routes(cfg):
    """Return (host, recipient) pairs, most specific first."""
    routes = {}
    raw = cfg.get("mail", "subdominios", fallback="")
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(f"subdominios: falta ':' en {entry!r}")
        host, recipient = (part.strip() for part in entry.rsplit(":", 1))
        host = host.lower().rstrip(".")
        if (host == "upv.es" or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.upv\.es", host)):
            raise ValueError(f"subdominios: dominio inválido {host!r}")
        if not re.fullmatch(r"[^@\s,:]+@[^@\s,:]+\.[^@\s,:]+", recipient):
            raise ValueError(f"subdominios: correo inválido para {host}")
        if host in routes:
            raise ValueError(f"subdominios: dominio repetido {host}")
        routes[host] = recipient
    return sorted(routes.items(), key=lambda pair: (-len(pair[0]), pair[0]))


def recipient_for_source(url, routes):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for domain, recipient in routes:
        if host == domain or host.endswith("." + domain):
            return recipient
    return None


def write_detail_report(path, findings, delimiter):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=delimiter)
        writer.writerow(("fecha", "nivel", "pagina_origen", "enlace_roto", "texto_ancla", "resultado", "contexto_html"))
        for level, stamp, source, target, anchor, detail, context in sorted(findings):
            writer.writerow((stamp, level, source, target, anchor, detail, context))


def write_grouped_report(path, groups, delimiter):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=delimiter)
        writer.writerow(("enlace_roto", "resultado", "apariciones", "paginas_afectadas", "niveles",
                         "dias_deteccion", "sitios_origen", "textos_ancla", "contextos_html", "pagina_ejemplo"))
        for g in groups:
            writer.writerow((g["target"], g["detail"], g["rows"], len(g["sources"]),
                             ";".join(map(str, sorted(g["levels"]))), ";".join(sorted(g["days"])),
                             ";".join(sorted(g["hosts"])), ";".join(sorted(g["anchors"])),
                             ";".join(sorted(g["contexts"])), min(g["sources"])))


def email_report(cfg, path, grouped_path, stats, findings, groups, recipients=None, scope=None):
    if not cfg.getboolean("mail", "enabled"):
        return
    server = cfg.get("mail", "smtp_server_address")
    protocol = cfg.get("mail", "smtp_protocol").lower()
    if protocol not in ("ssl", "starttls"):
        raise ValueError("smtp_protocol debe ser ssl o starttls")
    port = cfg.getint("mail", "smtp_server_port", fallback=465 if protocol == "ssl" else 587)
    user = os.environ.get(cfg.get("mail", "user_env", fallback="UPV_SMTP_USER"), "")
    password = os.environ.get(cfg.get("mail", "password_env", fallback="UPV_SMTP_PASSWORD"), "")
    if user and not password:
        raise ValueError("Falta la contraseña SMTP en la variable de entorno configurada")
    msg = EmailMessage()
    sender = cfg.get("mail", "from_address", fallback="").strip() or user
    if not sender:
        raise ValueError("Falta from_address o el usuario SMTP en el entorno")
    msg["From"] = sender
    msg["To"] = recipients or cfg.get("mail", "recipients")
    report_day = datetime.now(ZoneInfo(cfg.get("crawl", "report_timezone", fallback="Europe/Madrid"))).date()
    msg["Subject"] = (f"UPV: revisión de enlaces {report_day}" + (f" [{scope}]" if scope else "") + " "
                      f"({len({r[3] for r in findings})} destinos, {len(findings)} apariciones)")
    counts = defaultdict(lambda: {"targets": set(), "sources": set(), "rows": 0})
    for level, stamp, source, target, *_ in findings:
        key = (level, stamp[:10])
        counts[key]["targets"].add(target)
        counts[key]["sources"].add(source)
        counts[key]["rows"] += 1
    lines = ([f"Ámbito: {scope}"] if scope else []) + [
        "Resumen de enlaces rotos",
        f"Fin de esta ejecución: {stats.get('finish_reason', 'No registrado')}",
        f"Páginas intentadas en total: {stats.get('attempted_pages', 0)}",
        "Nivel | Día | Destinos distintos | Apariciones | Páginas afectadas"]
    for level, day in sorted(stats["levels"] | set(counts)):
        c = counts[(level, day)]
        lines.append(f"{level} | {day} | {len(c['targets'])} | {c['rows']} | {len(c['sources'])}")
    if not stats["levels"]:
        lines.append("Sin páginas rastreadas en esta ejecución")
    lines.extend(["", f"Destinos distintos en la ejecución: {len({r[3] for r in findings})}",
                  f"Apariciones: {len(findings)}", "",
                  "Enlaces compartidos (agrupados por destino exacto y resultado)"])
    limit = max(0, cfg.getint("mail", "max_inline_groups", fallback=30))
    shared = [g for g in groups if len(g["sources"]) > 1]
    isolated = [g for g in groups if len(g["sources"]) == 1]
    isolated_budget = min(10, max(1, limit // 3), len(isolated)) if shared else limit
    selected_shared = shared[:max(0, limit - isolated_budget)]
    selected_isolated = isolated[:limit - len(selected_shared)]

    def describe(g):
        anchors = ", ".join(sorted(g["anchors"]))[:120]
        contexts = ", ".join(sorted(g["contexts"]))
        return (f"{g['detail']} | {len(g['sources'])} páginas | {g['rows']} apariciones | "
                f"{g['target']}\n  Texto: {anchors}; contexto HTML: {contexts}; "
                f"sitio: {', '.join(sorted(g['hosts']))}; ejemplo: {min(g['sources'])}")

    lines.extend(describe(g) for g in selected_shared)
    lines.extend(["", "Enlaces en una sola página (muestra)"])
    lines.extend(describe(g) for g in selected_isolated)
    if len(groups) > len(selected_shared) + len(selected_isolated):
        lines.append(f"... {len(groups) - len(selected_shared) - len(selected_isolated)} grupos adicionales en el CSV agrupado")
    lines.extend(["", f"Páginas procesadas en este ámbito: {stats['pages']}",
                  f"Solicitudes HTTP totales del rastreo: {stats['checked']}",
                  f"Comprobaciones inciertas totales: {stats['unknown']}",
                  f"Exclusiones robots totales: {stats['robots']}",
                  f"Respuestas de throttling totales: {stats['throttled']}",
                  f"Enlaces pendientes de reintento totales: {stats['pending_links']}",
                  "CSV agrupado y detalle completo adjuntos.",
                  "El contexto HTML es una pista estructural; no prueba visibilidad en móvil."])
    msg.set_content("\n".join(lines) + "\n")
    msg.add_attachment(grouped_path.read_bytes(), maintype="text", subtype="csv", filename=grouped_path.name)
    msg.add_attachment(path.read_bytes(), maintype="text", subtype="csv", filename=path.name)
    for attempt in range(3):
        try:
            conn = smtplib.SMTP_SSL(server, port, timeout=30) if protocol == "ssl" else smtplib.SMTP(server, port, timeout=30)
            with conn as smtp:
                if protocol == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
            return
        except (smtplib.SMTPException, OSError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def send_partitioned_reports(cfg, report_dir, run_stamp, delimiter, findings, stats, routes):
    """Give each mailbox only findings from the source domains assigned to it."""
    if not routes:
        report = report_dir / f"enlaces_rotos_{run_stamp}.csv"
        grouped = report_dir / f"enlaces_rotos_{run_stamp}_agrupados.csv"
        email_report(cfg, report, grouped, stats, findings, group_findings(findings))
        return

    route_domains = defaultdict(list)
    for domain, recipient in routes:
        route_domains[recipient].append(domain)
    finding_sets = defaultdict(set)
    for finding in findings:
        recipient = recipient_for_source(finding[2], routes)
        finding_sets[recipient].add(finding)
    scanned = defaultdict(set)
    for level, day, source in stats["source_levels"]:
        recipient = recipient_for_source(source, routes)
        scanned[recipient].add((level, day, source))

    delivery_errors = []
    for recipient in [None, *route_domains]:
        subset = finding_sets[recipient]
        source_rows = scanned[recipient]
        if recipient is not None and not subset and not source_rows:
            continue  # No page or incident from this route in this execution.
        suffix = "resto" if recipient is None else "dominio_" + hashlib.sha256(recipient.encode()).hexdigest()[:10]
        detail_path = report_dir / f"enlaces_rotos_{run_stamp}_{suffix}.csv"
        grouped_path = report_dir / f"enlaces_rotos_{run_stamp}_{suffix}_agrupados.csv"
        write_detail_report(detail_path, subset, delimiter)
        groups = group_findings(subset)
        write_grouped_report(grouped_path, groups, delimiter)
        segment_stats = dict(stats)
        segment_stats["levels"] = {(level, day) for level, day, _ in source_rows}
        segment_stats["pages"] = len({source for _, _, source in source_rows})
        scope = "Resto UPV" if recipient is None else ", ".join(sorted(route_domains[recipient]))
        try:
            email_report(cfg, detail_path, grouped_path, segment_stats, subset, groups,
                         recipients=recipient, scope=scope)
        except (smtplib.SMTPException, OSError) as exc:
            LOG.exception("No se pudo enviar el informe %s", scope)
            delivery_errors.append((scope, exc))
        LOG.info("Informe %s: %s apariciones, %s páginas procesadas", scope, len(subset), segment_stats["pages"])
    if delivery_errors:
        raise RuntimeError("Falló el envío de informes: " + ", ".join(scope for scope, _ in delivery_errors))


def run_context_backfill(cfg, db, auditor, report_dir):
    """Fetch legacy pages once and update only link contexts, preserving rounds."""
    quota = cfg.getint("crawl", "max_context_backfill_pages", fallback=1000)
    if quota < 1:
        raise ValueError("max_context_backfill_pages debe ser mayor que cero")
    levels = sorted(auditor.level_schedule)
    placeholders = ",".join("?" for _ in levels)
    candidates = list(db.execute(f"""
        SELECT p.url,p.level FROM pages p
        WHERE p.level IN ({placeholders})
          AND EXISTS (SELECT 1 FROM links l WHERE l.source=p.url AND l.context='sin_datos')
          AND NOT EXISTS (SELECT 1 FROM context_backfill b WHERE b.source=p.url
                          AND (b.retry_at IS NULL OR b.retry_at>?))
        ORDER BY p.level,p.url LIMIT ?
    """, (*levels, now(), quota)))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report = report_dir / f"contexto_pendiente_{stamp}.csv"
    processed = updated = 0
    with report.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=csv_delimiter(cfg))
        writer.writerow(("pagina_origen", "nivel", "resultado", "enlaces_con_contexto", "enlaces_antiguos", "detalle"))
        for url, level in candidates:
            try:
                auditor.ensure_time()
                old = list(db.execute("SELECT target,anchor FROM links WHERE source=? AND context='sin_datos'", (url,)))
                if not old:
                    continue
                retry_at = None
                matched = 0
                detail = ""
                if not auditor.allowed(url):
                    p = urlsplit(url)
                    root = f"{p.scheme}://{p.netloc}"
                    if auditor.robots.get(root) is None:
                        result, detail = "reintentar", "robots.txt no disponible"
                    else:
                        result, detail = "excluida", "excluida por robots.txt"
                else:
                    status, detail, html, final_url = auditor.fetch(url)
                    if status == "unknown":
                        result = "reintentar"
                    elif html is None:
                        result = "sin_html"
                    else:
                        edges = extract_edges(html, final_url)
                        for target, anchor in old:
                            context = edges.get((target, anchor))
                            if context:
                                db.execute("UPDATE links SET context=? WHERE source=? AND target=? AND anchor=?",
                                           (";".join(sorted(context)), url, target, anchor))
                                matched += 1
                        result = "actualizada" if matched else "sin_coincidencias"
                        updated += matched
                if result == "reintentar":
                    retry_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                db.execute("""INSERT INTO context_backfill(source,attempted_at,retry_at,result) VALUES(?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at,
                    retry_at=excluded.retry_at,result=excluded.result""", (url, now(), retry_at, result))
                db.commit()
                writer.writerow((url, level, result, matched, len(old), detail))
                processed += 1
                if processed % 100 == 0:
                    LOG.info("Contexto: %s páginas intentadas, %s enlaces actualizados, %s solicitudes HTTP",
                             processed, updated, auditor.stats["checked"])
            except RunLimit:
                db.rollback()
                break
            except Exception as exc:
                db.rollback()
                LOG.exception("Error al actualizar contexto de %s", url)
                retry_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                db.execute("""INSERT INTO context_backfill(source,attempted_at,retry_at,result) VALUES(?,?,?,'reintentar')
                    ON CONFLICT(source) DO UPDATE SET attempted_at=excluded.attempted_at,
                    retry_at=excluded.retry_at,result=excluded.result""", (url, now(), retry_at))
                db.commit()
                writer.writerow((url, level, "reintentar", 0, len(old), f"{type(exc).__name__}: {str(exc)[:180]}"))
                processed += 1
    remaining = db.execute("SELECT COUNT(DISTINCT source) FROM links WHERE context='sin_datos'").fetchone()[0]
    LOG.info("Contexto: %s páginas intentadas, %s enlaces actualizados, %s solicitudes HTTP; "
             "%s páginas aún contienen contexto antiguo (incluidas excluidas/sin coincidencias); informe: %s",
             processed, updated, auditor.stats["checked"], remaining, report)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--start-url", help="Raíz UPV alternativa; crea una base y carpeta de informes independientes")
    ap.add_argument("--backfill-context", action="store_true",
                    help="Actualizar solo el contexto HTML de enlaces antiguos, sin revisar sus destinos")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(args.config):
        ap.error("No se pudo leer la configuración")
    delimiter = csv_delimiter(cfg)
    routes = subdomain_routes(cfg)
    configured_root = normalize(cfg.get("crawl", "start_url"))
    root = normalize(args.start_url) if args.start_url else configured_root
    if root is None or not upv_page(root):
        raise ValueError("start_url debe ser una URL HTTP(S) de upv.es o uno de sus subdominios")
    base_db_path = Path(cfg.get("paths", "database")).expanduser()
    db_path = base_db_path
    report_dir = Path(cfg.get("paths", "reports")).expanduser()
    if args.start_url and root != configured_root:
        host = urlsplit(root).hostname.replace(".", "_")
        suffix = f"{host}-{hashlib.sha256(root.encode()).hexdigest()[:8]}"
        db_path = db_path.with_name(f"{db_path.stem}-{suffix}{db_path.suffix}")
        report_dir = report_dir / suffix
    db_path.parent.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    # Every root from this configuration shares a lock: their per-host rate
    # limiters are independent and must not make concurrent requests.
    with open(str(base_db_path) + ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Ya hay una ejecución activa; se omite")
            return 2 if args.backfill_context else 0
        with connect(db_path) as db:
            auditor = Auditor(cfg, db)
            bind_root(db, root)
            if args.backfill_context:
                return run_context_backfill(cfg, db, auditor, report_dir)
            prune_old_data(db, auditor.retention_days, root)
            auditor.enqueue(root, 0)
            db.commit()
            findings = set()
            run_token = uuid.uuid4().hex
            quota = cfg.getint("crawl", "max_pages_per_run")
            attempted = 0
            stopped = False
            finish_reason = None
            # Check old uncertain links once per run without fetching their source
            # pages again. Bound this work so page discovery cannot be starved.
            pending_quota = cfg.getint("crawl", "max_pending_links_per_run", fallback=1000)
            due_links = list(db.execute(
                "SELECT url FROM pending_links WHERE retry_at<=? ORDER BY retry_at LIMIT ?",
                (now(), pending_quota)))
            retried = 0
            for (target,) in due_links:
                try:
                    auditor.ensure_time()
                    r, detail, _ = auditor.check(target, cfg.getfloat("crawl", "link_ttl_days"), force=True)
                    if r == "unknown":
                        target_origin = f"{urlsplit(target).scheme}://{urlsplit(target).netloc}"
                        if detail == "excluido por robots.txt" and auditor.robots.get(target_origin) is not None:
                            db.execute("DELETE FROM pending_links WHERE url=?", (target,))
                        else:
                            auditor.queue_uncertain_link(target, detail)
                    else:
                        db.execute("DELETE FROM pending_links WHERE url=?", (target,))
                        if r == "broken":
                            stamp = datetime.now(auditor.local_tz).isoformat(timespec="seconds")
                            for source, anchor, context, level in db.execute(
                                "SELECT l.source,l.anchor,l.context,p.level FROM links l JOIN pages p ON p.url=l.source "
                                "WHERE l.target=? AND p.level IS NOT NULL", (target,)):
                                if level in auditor.level_schedule:
                                    findings.add((level, stamp, source, target, anchor, detail, context))
                    db.commit()
                    retried += 1
                except RunLimit as exc:
                    db.rollback()
                    stopped = True
                    finish_reason = str(exc)
                    break
            if due_links:
                LOG.info("Reintentados %s enlaces pendientes; %s solicitudes HTTP hasta ahora",
                         retried, auditor.stats["checked"])
            while attempted < quota and not stopped:
                try:
                    auditor.ensure_time()
                except RunLimit as exc:
                    finish_reason = str(exc)
                    break
                start_and_finish_rounds(db, auditor.level_schedule)
                db.commit()
                # Pages are unique within a level's round, across all cron invocations.
                batch = list(db.execute(
                    "SELECT p.url,p.level,r.round_number FROM pages p JOIN level_rounds r ON r.level=p.level "
                    "WHERE r.active=1 AND p.completed_round<r.round_number "
                    "AND (p.retry_at IS NULL OR p.retry_at<=?) "
                    "ORDER BY p.level,(p.checked_at IS NOT NULL),p.url LIMIT ?",
                    (now(), min(100, quota - attempted))))
                if not batch:
                    finish_reason = "No hay páginas listas para revisar ahora"
                    break
                for url, level, round_number in batch:
                    if attempted >= quota:
                        break
                    current = db.execute("SELECT level,completed_round,last_completed_execution FROM pages WHERE url=?", (url,)).fetchone()
                    if current is None or current[0] != level or current[1] >= round_number:
                        continue
                    if current[2] == run_token:
                        # Same page got a shorter path during this execution.
                        # Reuse its stored link graph to reveal descendants.
                        for (target,) in db.execute("SELECT DISTINCT target FROM links WHERE source=?", (url,)):
                            auditor.enqueue(target, level + 1)
                        auditor.record_page(url, round_number, True, run_token)
                        db.commit()
                        continue
                    if urlsplit(url).netloc.lower() in auditor.blocked_hosts:
                        auditor.record_page(url, round_number, False, run_token)
                        db.commit()
                        attempted += 1
                        continue
                    try:
                        auditor.ensure_time()
                        broken, complete = auditor.process_page(url, level, cfg.getfloat("crawl", "link_ttl_days"))
                        findings.update(broken)
                        auditor.record_page(url, round_number, complete, run_token)
                        db.commit()
                    except RunLimit as exc:
                        db.rollback()
                        stopped = True
                        finish_reason = str(exc)
                        break
                    except Exception:
                        db.rollback()
                        LOG.exception("Error al procesar %s; se reintentará mañana", url)
                        auditor.record_page(url, round_number, False, run_token)
                        db.commit()
                    attempted += 1
                    if attempted % 100 == 0:
                        LOG.info("Progreso: %s páginas intentadas, %s procesadas, %s solicitudes HTTP",
                                 attempted, auditor.stats["pages"], auditor.stats["checked"])
            if finish_reason is None:
                finish_reason = f"Límite de páginas intentadas alcanzado ({quota})"
            auditor.stats["finish_reason"] = finish_reason
            auditor.stats["attempted_pages"] = attempted
            auditor.stats["pending_links"] = db.execute("SELECT COUNT(*) FROM pending_links").fetchone()[0]
            run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            report = report_dir / f"enlaces_rotos_{run_stamp}.csv"
            write_detail_report(report, findings, delimiter)
            groups = group_findings(findings)
            grouped_report = report_dir / f"enlaces_rotos_{run_stamp}_agrupados.csv"
            write_grouped_report(grouped_report, groups, delimiter)
            LOG.info("Fin: %s; %s páginas intentadas, %s procesadas; %s solicitudes; %s respuestas 429/503; %s enlaces pendientes; %s incidencias; informe: %s",
                     finish_reason, attempted,
                     auditor.stats["pages"], auditor.stats["checked"], auditor.stats["throttled"],
                     auditor.stats["pending_links"], len(findings), report)
            send_partitioned_reports(cfg, report_dir, run_stamp, delimiter, findings, auditor.stats, routes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
