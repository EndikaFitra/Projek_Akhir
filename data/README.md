# Scraper Dokumen PENS

Scraper untuk dokumen peraturan akademik di <https://www.pens.ac.id/dokumen/>.

Halaman tersebut memakai plugin WordPress **WP File Download (wpfd)**. Struktur
folder & daftar file tidak ada di HTML, melainkan diambil via AJAX:

- `admin-ajax.php?action=wpfd&task=categories.display&view=categories&id=<id>&top=<root>`
- `admin-ajax.php?action=wpfd&task=files.display&view=files&id=<id>&rootcat=<root>&page=<n>`

Root category halaman `/dokumen/` adalah **897** (`Dokumen PENS`).

## Environment

Virtualenv sudah tersedia di `venv/`:

```bash
cd data
./venv/bin/pip install -r requirements.txt   # jika perlu install ulang
```

Dependensi: `requests`.

## Cara pakai

```bash
# 1. Lihat rencana tanpa mengunduh (cepat karena ada cache)
./venv/bin/python scrape_pens_dokumen.py --dry-run

# 2. Unduh semua dokumen, mirror struktur folder
./venv/bin/python scrape_pens_dokumen.py

# Opsi lain
./venv/bin/python scrape_pens_dokumen.py --limit 20    # unduh 20 file pertama
./venv/bin/python scrape_pens_dokumen.py --workers 4   # atur concurrency
./venv/bin/python scrape_pens_dokumen.py --refresh     # crawl ulang struktur folder
./venv/bin/python scrape_pens_dokumen.py --root 899    # mulai dari root category lain
```

## Hasil

- `documents/` — dokumen tersimpan, struktur folder ditiru dari situs.
  Contoh: `documents/Akademik/...`, `documents/Peraturan Direktur/<sub>/...`
- `manifest.json` — daftar file + path relatif hasil unduhan.
- `tree_cache.json` — cache struktur kategori (218 kategori).
- `files_cache.json` — cache daftar file (1186 dokumen) agar re-run tidak query ulang.
- `download.log` — log proses unduhan terakhir.

## Catatan teknis

- Server PENS membalas **503** jika dibanjiri request. Concurrency default
  `3` worker + backoff eksponensial + jitter. Jangan naikkan `--workers`
  terlalu tinggi.
- Unduhan bersifat **resumable**: file yang sudah ada dengan ukuran sama
  akan dilewati (`SKIP`), sehingga aman dijalankan ulang.
- File besar (`.mp4`) ikut terunduh; total ± 1,6 GB untuk 1186 dokumen.
