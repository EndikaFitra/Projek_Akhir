#!/usr/bin/env python3
"""
Scraper dokumen peraturan akademik PENS.

Sumber : https://www.pens.ac.id/dokumen/
Plugin : WP File Download (wpfd) — data folder & file diambil melalui
         admin-ajax.php?action=wpfd.

Struktur folder di-mirror ke filesystem. contoh:
    data/documents/Akademik/<file>.pdf
    data/documents/Peraturan Direktur/<subfolder>/<file>.pdf

Jalankan:
    ./venv/bin/python scrape_pens_dokumen.py              # unduh semua
    ./venv/bin/python scrape_pens_dokumen.py --dry-run    # lihat rencana, tanpa unduh
    ./venv/bin/python scrape_pens_dokumen.py --root 897   # batasi ke root category tertentu
    ./venv/bin/python scrape_pens_dokumen.py --workers 8  # atur concurrency

Catatan performa:
    Halaman /dokumen/ berisi 200+ kategori bersarang (max depth 6). Crawl
    serial memakan ~2 menit hanya untuk struktur; skrip ini memakai thread
    pool + cache sehingga jauh lebih cepat dan progresnya terlihat live.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Optional
from urllib.parse import unquote

import requests

# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------

AJAX_URL = "https://www.pens.ac.id/wp-admin/admin-ajax.php"

# Root category "Dokumen PENS" pada halaman /dokumen/
DEFAULT_ROOT_CATEGORY = 897

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "documents"
MANIFEST_FILE = BASE_DIR / "manifest.json"
TREE_CACHE_FILE = BASE_DIR / "tree_cache.json"
FILES_CACHE_FILE = BASE_DIR / "files_cache.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.pens.ac.id/dokumen/",
}

REQUEST_TIMEOUT = 30
MAX_RETRIES = 5
RETRY_BACKOFF = 1.5          # detik
DELAY_BETWEEN_FILES = 0.2    # jeda sopan per unduhan
MAX_PAGES = 200              # pengaman anti-loop pagination
DEFAULT_WORKERS = 3          # concurrency request (server menolak >3-4 -> 503)

_thread_local = threading.local()


def get_session() -> requests.Session:
    """Session per-thread (requests.Session tidak thread-safe)."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
    return s


def log(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# Model data
# --------------------------------------------------------------------------

@dataclass
class Category:
    term_id: int
    name: str
    slug: str
    parent: Optional[int] = None


@dataclass
class Document:
    file_id: int
    category_id: int
    title: str
    ext: str
    size: int
    download_url: str
    post_name: str = ""

    @property
    def filename(self) -> str:
        name = self.post_name or self.title or f"file-{self.file_id}"
        name = unquote(name)
        if self.ext and not name.lower().endswith(f".{self.ext}"):
            name = f"{name}.{self.ext}"
        return sanitize_filename(name)


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------

_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = _INVALID_FS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name or "unnamed"


def ajax(task: str, params: dict) -> Optional[dict]:
    """Panggil endpoint AJAX wpfd dan kembalikan JSON.

    Server PENS membalas 503 saat kebanjiran request; backoff eksponensial
    ditambah jeda acak agar crawl tetap stabil.
    """
    query = {"juwpfisadmin": "false", "action": "wpfd", "task": task, **params}
    session = get_session()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(AJAX_URL, params=query, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (429, 503):
                raise requests.HTTPError(f"rate-limited ({resp.status_code})")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_err = exc
            if attempt < MAX_RETRIES:
                # backoff eksponensial + jitter agar worker tidak sinkron
                delay = RETRY_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                time.sleep(delay)
    log(f"  [!] gagal {task} {params}: {last_err}")
    return None


def get_child_categories(cat_id: int, top: int) -> list[Category]:
    data = ajax("categories.display",
                {"view": "categories", "id": cat_id, "top": top})
    if not data:
        return []
    out = []
    for c in data.get("categories") or []:
        out.append(Category(
            term_id=int(c["term_id"]),
            name=(c.get("name") or "").strip(),
            slug=c.get("slug", ""),
            parent=c.get("parent"),
        ))
    return out


def get_files(cat_id: int, root: int) -> list[Document]:
    """Ambil semua file dalam kategori, ikuti pagination, dedup by ID."""
    docs: list[Document] = []
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        data = ajax("files.display",
                    {"view": "files", "id": cat_id, "rootcat": root, "page": page})
        if not data:
            break
        files = data.get("files") or []
        if not files:
            break
        new = 0
        for f in files:
            fid = int(f["ID"])
            if fid in seen:
                continue
            seen.add(fid)
            new += 1
            docs.append(Document(
                file_id=fid,
                category_id=int(f.get("catid", cat_id)),
                title=f.get("post_title", ""),
                ext=(f.get("ext") or "").lower(),
                size=int(f.get("size") or 0),
                download_url=f.get("linkdownload") or "",
                post_name=f.get("post_name") or f.get("post_title", ""),
            ))
        if new == 0:  # server mengulang halaman yang sama
            break
    return docs


# --------------------------------------------------------------------------
# Crawl struktur folder (paralel + cache)
# --------------------------------------------------------------------------

def crawl_children_level(
    cat_ids: list[tuple[int, str]], workers: int
) -> dict[int, list[Category]]:
    """Ambil subkategori untuk sekumpulan kategori secara paralel."""
    result: dict[int, list[Category]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(get_child_categories, cid, DEFAULT_ROOT_CATEGORY): cid
            for cid, _ in cat_ids
        }
        for fut in as_completed(futures):
            cid = futures[fut]
            try:
                result[cid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"  [!] gagal children {cid}: {exc}")
                result[cid] = []
    return result


def crawl_tree(root_cat: int, use_cache: bool = True) -> dict:
    """
    Telusuri seluruh pohon kategori secara paralel (BFS per level).

    Mengembalikan struktur:
        {"root": 897, "categories": {id: {...}}, "children": {parent: [ids]}}
    """
    root = get_child_categories(root_cat, root_cat)
    log(f"Root '{root_cat}': {len(root)} kategori level-1")

    categories: dict[int, dict] = {}
    children: dict[int, list[int]] = {}
    parents: list[tuple[int, str]] = [(c.term_id, c.name) for c in root]
    for c in root:
        categories[c.term_id] = {"id": c.term_id, "name": c.name, "slug": c.slug}

    level = 0
    while parents:
        level += 1
        log(f"  level {level}: crawl {len(parents)} kategori...")
        child_map = crawl_children_level(parents, DEFAULT_WORKERS)
        next_parents: list[tuple[int, str]] = []
        for pid, _ in parents:
            kids = child_map.get(pid, [])
            children[pid] = [k.term_id for k in kids]
            for k in kids:
                if k.term_id not in categories:
                    categories[k.term_id] = {
                        "id": k.term_id, "name": k.name, "slug": k.slug,
                    }
                    next_parents.append((k.term_id, k.name))
        parents = next_parents

    log(f"  total kategori: {len(categories)}")
    return {"root": root_cat, "categories": categories, "children": children}


def load_cache() -> Optional[dict]:
    if TREE_CACHE_FILE.exists():
        try:
            return json.loads(TREE_CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def save_cache(tree: dict) -> None:
    TREE_CACHE_FILE.write_text(
        json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Bangun daftar dokumen dari tree + folder path
# --------------------------------------------------------------------------

def collect_documents(tree: dict, workers: int) -> list[tuple[Document, Path]]:
    """Ambil file tiap kategori paralel, sekaligus hitung path folder.

    Hasil listing di-cache ke files_cache.json agar re-run tidak perlu
    menanyai server lagi.
    """
    cats = tree["categories"]
    kids = tree["children"]

    # hitung path folder tiap kategori via traversal pohon
    root_id = tree["root"]
    path_of: dict[int, Path] = {}
    stack = [(root_id, OUTPUT_DIR)]
    visited: set[int] = set()
    while stack:
        cid, base = stack.pop()
        if cid in visited:
            continue
        visited.add(cid)
        for child_id in kids.get(cid, []):
            name = sanitize_filename(cats[child_id]["name"])
            folder = base / name
            path_of[child_id] = folder
            stack.append((child_id, folder))

    if FILES_CACHE_FILE.exists():
        try:
            raw = json.loads(FILES_CACHE_FILE.read_text(encoding="utf-8"))
            items = [
                (Document(e["file_id"], e["category_id"], e["title"], e["ext"],
                          e["size"], e["download_url"], e.get("post_name", "")),
                 OUTPUT_DIR / e["folder"])
                for e in raw
            ]
            log(f"[cache] memakai files_cache.json ({len(items)} file)")
            return items
        except (json.JSONDecodeError, KeyError):
            log("[cache] files_cache.json rusak, crawl ulang daftar file")

    cat_ids = [(cid, cats[cid]["name"]) for cid in cats]
    log(f"  ambil daftar file dari {len(cat_ids)} kategori...")

    results: list[tuple[Document, Path]] = []
    cache_entries: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(get_files, cid, root_id): cid for cid, _ in cat_ids
        }
        for fut in as_completed(futures):
            cid = futures[fut]
            done += 1
            try:
                files = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"  [!] gagal files {cid}: {exc}")
                continue
            folder = path_of.get(cid, OUTPUT_DIR / sanitize_filename(cats[cid]["name"]))
            for d in files:
                results.append((d, folder))
                cache_entries.append({
                    "file_id": d.file_id,
                    "category_id": d.category_id,
                    "title": d.title,
                    "ext": d.ext,
                    "size": d.size,
                    "download_url": d.download_url,
                    "post_name": d.post_name,
                    "folder": str(folder.relative_to(OUTPUT_DIR)) if folder != OUTPUT_DIR else "",
                })
            if done % 25 == 0 or done == len(cat_ids):
                log(f"    {done}/{len(cat_ids)} kategori, {len(results)} file")

    FILES_CACHE_FILE.write_text(
        json.dumps(cache_entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


# --------------------------------------------------------------------------
# Unduh
# --------------------------------------------------------------------------

def download(doc: Document, folder: Path) -> tuple[str, int]:
    """Unduh satu dokumen. Return (status, bytes)."""
    if not doc.download_url:
        return "no-url", 0
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / doc.filename
    if target.exists() and doc.size > 0 and target.stat().st_size == doc.size:
        return "skip", target.stat().st_size
    try:
        session = get_session()
        with session.get(doc.download_url, stream=True, timeout=REQUEST_TIMEOUT) as r:
            r.raise_for_status()
            tmp = target.with_suffix(target.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
            tmp.replace(target)
        time.sleep(DELAY_BETWEEN_FILES)
        return "ok", target.stat().st_size
    except requests.RequestException as exc:
        return f"err:{exc}", 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Scraper dokumen PENS per folder")
    ap.add_argument("--root", type=int, default=DEFAULT_ROOT_CATEGORY,
                    help=f"root category id (default: {DEFAULT_ROOT_CATEGORY})")
    ap.add_argument("--dry-run", action="store_true",
                    help="tampilkan rencana tanpa mengunduh")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"jumlah worker paralel (default: {DEFAULT_WORKERS})")
    ap.add_argument("--refresh", action="store_true",
                    help="abaikan tree_cache.json, crawl ulang struktur folder")
    ap.add_argument("--limit", type=int, default=0,
                    help="batasi jumlah file yang diunduh (0 = semua)")
    args = ap.parse_args()

    t0 = time.time()

    tree = None
    if not args.refresh:
        tree = load_cache()
        if tree and tree.get("root") == args.root:
            log(f"[cache] memakai tree_cache.json ({len(tree['categories'])} kategori)")
    if not tree:
        log("[crawl] menelusuri struktur folder...")
        tree = crawl_tree(args.root)
        save_cache(tree)

    items = collect_documents(tree, args.workers)
    log(f"\nTotal dokumen ditemukan: {len(items)}")
    log(f"Waktu crawl: {round(time.time() - t0, 1)}s\n")

    manifest = [
        {
            "file_id": d.file_id,
            "category_id": d.category_id,
            "title": d.title,
            "ext": d.ext,
            "size": d.size,
            "download_url": d.download_url,
            "relative_path": str(folder.relative_to(OUTPUT_DIR) / d.filename),
        }
        for d, folder in items
    ]

    if args.dry_run:
        for m in manifest[: args.limit or len(manifest)]:
            print(f"  [{m['ext']:>4}] {m['relative_path']}")
        log(f"\n(dry-run) {len(manifest)} dokumen.")
        return 0

    to_run = items[: args.limit] if args.limit else items
    stats = {"ok": 0, "skip": 0, "fail": 0}
    lock = threading.Lock()

    def worker(pair: tuple[Document, Path]) -> None:
        doc, folder = pair
        status, nbytes = download(doc, folder)
        with lock:
            if status == "ok":
                stats["ok"] += 1
            elif status == "skip":
                stats["skip"] += 1
            else:
                stats["fail"] += 1
            total = sum(stats.values())
            tag = {"ok": "OK ", "skip": "SKIP"}.get(status, "GAGAL")
            log(f"[{total:>4}/{len(to_run)}] {tag} {doc.filename}")

    log(f"Mengunduh {len(to_run)} dokumen dengan {args.workers} worker...\n")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, to_run))

    MANIFEST_FILE.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"\nSelesai dalam {round(time.time() - t0, 1)}s")
    log(f"  berhasil : {stats['ok']}")
    log(f"  dilewati : {stats['skip']}")
    log(f"  gagal    : {stats['fail']}")
    log(f"Manifest : {MANIFEST_FILE}")
    log(f"Dokumen  : {OUTPUT_DIR}")
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
