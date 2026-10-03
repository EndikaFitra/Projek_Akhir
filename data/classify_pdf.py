#!/usr/bin/env python3
"""Kelompokkan PDF di documents/ berdasarkan JENIS dokumen.

Hasil: documents_by_type/<Jenis>[/<Sub>]/<file>.pdf  (hardlink, folder asli tidak diubah)
       classification.csv  (mapping file asal -> kategori)

Pakai: python3 classify_pdf.py [--dry-run]
"""
import csv, json, os, re, subprocess, sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "documents")
DST = os.path.join(BASE, "documents_by_type")

def first_page_text(path):
    try:
        out = subprocess.run(["pdftotext", "-l", "1", path, "-"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        out = ""
    return re.sub(r"\s+", " ", out).strip().upper()[:1500]

def has(s, *pats):
    return any(re.search(p, s) for p in pats)

def classify(title, folder, fname, text):
    t = title.upper().replace("&AMP;", "&")
    f = folder.upper()
    head = text[:400]

    # 1. Lampiran pengakuan SKS per mahasiswa (RPL)
    if re.match(r"^\d{10}_", fname) or t.startswith("D3 PJJ -") or "SKS YANG DIAKUI" in head:
        return None  # lampiran pengakuan SKS per mahasiswa: tidak diperlukan, dilewati

    # 2. Undang-Undang & Peraturan Pemerintah
    if has(t, r"^UU\b", r"UNDANG-UNDANG", r"^PP \d"):
        return "01_Undang-Undang & Peraturan Pemerintah"

    # 3. Statuta
    if "STATUTA" in t:
        return "02_Statuta"

    # 4. Peraturan / Keputusan Menteri & Dirjen
    if has(t, r"PERMEN", r"KEPMEN", r"KEPDIRJEN", r"SALINAN KEP", r"PERPANJANGAN AKREDITASI OTOMATIS",
           r"KUALIFIKASI D-IV", r"SK KEMENDIKBUD"):
        return "03_Peraturan & Keputusan Menteri"

    # 5. Peraturan Direktur / peraturan internal
    if has(t, r"PERATURAN[- ]DIREKTUR", r"PERDIR", r"PERATURAN[- ]AKADEMIK", r"^PERAK ",
           r"KODE ETIK", r"PENANGANAN PELANGGARAN ETIK", r"PENYUSUNAN PEMUTAKHIRAN", r"^GELAR"):
        return "04_Peraturan Direktur & Peraturan Internal"

    # 5b. Folder yang isinya dokumen prosedur (POB/SOP/Standar Pelayanan)
    if not re.match(r"^(\d+[.-] ?)?SK\b", t):
        if f in ("AKADEMIK", "KEMAHASISWAAN"):
            if head.startswith("PEDOMAN"):
                return "11_Panduan & Pedoman"
            return "14_SOP, POB & Standar Pelayanan"
        if f == "STANDAR PELAYANAN PUBLIC":
            return "14_SOP, POB & Standar Pelayanan"

    # 5c. Kasus khusus berbasis judul
    if "SURAT EDARAN" in t:
        return "07_Surat Edaran"
    if "SOSIALISASI" in t:
        return "15_Materi Sosialisasi"
    if re.match(r"^NOMOR \d+ TAHUN \d{4}", t) and "PERATURAN MENTERI" in head:
        return "03_Peraturan & Keputusan Menteri"
    if t.startswith(("PANDUAN", "PETUNJUK", "TUTORIAL")):
        return "11_Panduan & Pedoman"
    if "CPNS" in t or "PPPK" in t:
        return "06_Pengumuman"
    if "JADWAL" in t and "PENGUMUMAN" not in t:
        return "10_Jadwal & Kalender Akademik"
    if f == "PENGUMUMAN AKADEMIK" and has(t, r"DAFTAR ULANG") and not t.startswith("SK"):
        return "06_Pengumuman"
    if has(t, r"^REVISI SK"):
        return "05_Surat Keputusan (SK)/Penerimaan Mahasiswa Baru"

    # 6. Akreditasi (SK & Sertifikat)
    if has(t, r"SERTIFIKAT", r"LAMINFOKOM"):
        return "05_Surat Keputusan (SK)/Akreditasi & Sertifikat Akreditasi"
    if has(t, r"BAN[- ]PT", r"AKREDITASI", r"LAM[- ]INFOKOM", r"LAM TEKNIK", r"^SKM ", r"^096\. SKM",
           r"^SK (D3|D4|S2) ", r"SURAT KETERANGAN REAKREDITASI"):
        return "05_Surat Keputusan (SK)/Akreditasi & Sertifikat Akreditasi"

    # 7. Pendirian / izin / penataan prodi
    if has(t, r"PENDIRIAN", r"IZIN", r"IJIN", r"PEMBENTUKAN PRODI", r"PENATAA+N", r"PERUBAHAN NAMA PRODI",
           r"PERUBAHAN PRODI", r"PERUBAHAN IT MENJADI", r"RALAT NAMA", r"PROGRAM EKSTENSI",
           r"PEMBUKAAN PRODI", r"PEMBUKAAN PROGRAM"):
        return "05_Surat Keputusan (SK)/Pendirian & Izin Program Studi"

    # 8. SK Kurikulum, Yudisium, Kalender
    if "KURIKULUM" in t:
        return "05_Surat Keputusan (SK)/Kurikulum"
    if re.search(r"YUDIS", t) and t.startswith(("SK", "1.", "2.", "3.", "4.", "5.")):
        return "05_Surat Keputusan (SK)/Yudisium"
    if "KALENDER AKADEMIK" in t and t.startswith("SK"):
        return "05_Surat Keputusan (SK)/Kalender Akademik"

    is_pengumuman = has(t, r"PENGUMUMAN", r"PEMBERITAHUAN", r"HIMBAUAN") or (
        "PENGUMUMAN" in head and "KEPUTUSAN" not in head)

    # 9. SK Penerimaan Mahasiswa Baru (seleksi, daftar ulang, kuota, UKT, RPL, dsb)
    pmb = has(t, r"SELEKSI", r"HASIL", r"DAFTAR ULANG", r"DAFUL", r"KUOTA", r"MABA", r"UKT", r"PMB",
              r"PENGAKUAN SKS", r"CAPAIAN PEMBELAJARAN", r"KELAS IUP", r"REGISTRASI", r"SNBT", r"SNBP",
              r"SNMPTN", r"SBMPTN", r"RPL", r"PETUNJUK TEKNIS", r"PENETAPAN")
    if not is_pengumuman and pmb and (t.startswith(("SK", "REV")) or re.match(r"^\d+\.? ?-?SK", t)
                                      or "KEPUTUSAN" in head or not head):
        return "05_Surat Keputusan (SK)/Penerimaan Mahasiswa Baru"
    if not is_pengumuman and re.match(r"^(\d+[.-] ?)?SK\b", t):
        return "05_Surat Keputusan (SK)/Lainnya"

    # 10. Surat Edaran
    if "SURAT EDARAN" in t:
        return "07_Surat Edaran"

    # 11. Formulir
    if f in ("FORMULIR AKADEMIK", "LAIN-LAIN") or has(t, r"^FORM", r"FORMULIR", r"^PERMOHONAN SURAT"):
        return "09_Formulir"

    # 12. Pengumuman
    if is_pengumuman or f in ("PENGUMUMAN",) or has(t, r"^PERPANJANGAN", r"^REVISI PENGUMUMAN",
                                                    r"DAFTAR ULANG", r"^REV-", r"PERUBAHAN PELAKSANAAN",
                                                    r"SEMESTER ANTARA", r"PELAMAR", r"FORMASI CPNS",
                                                    r"RINCIAN KEBUTUHAN", r"PENGECEKAN", r"PENGISIAN",
                                                    r"JAM KULIAH", r"PEMBAYARAN", r"KELENGKAPAN"):
        if pmb and not is_pengumuman and f not in ("PENGUMUMAN", "PENGUMUMAN AKADEMIK"):
            return "05_Surat Keputusan (SK)/Penerimaan Mahasiswa Baru"
        return "06_Pengumuman"

    # 13. Jadwal & Kalender
    if has(t, r"JADWAL", r"KALENDER", r"SUSUNAN ACARA"):
        return "10_Jadwal & Kalender Akademik"

    # 14. Surat (keterangan, pernyataan, pengantar, usulan)
    if has(t, r"SURAT", r"SURKET", r"USULAN", r"PERMOHONAN"):
        return "08_Surat (Keterangan, Pernyataan, Permohonan)"

    # 15. Perencanaan & kinerja
    if has(t, r"RENCANA STRATEGIS", r"^RKT ", r"^RBA ", r"^RIP$", r"PERJANJIAN KINERJA"):
        return "12_Perencanaan & Perjanjian Kinerja"

    # 16. Laporan
    if has(t, r"LAPORAN", r"LAKIP", r"LAKIN", r"RETOOLING", r"_LN_", r"INTERNATIONAL ELECTRICAL",
           r"JUMAT BERSIH", r"PENGEMBANGAN SDM") or f in ("LUAR NEGERI", "KEGIATAN PEGAWAI"):
        return "13_Laporan Kegiatan & Kinerja"

    # 17. Panduan & Pedoman
    if has(t, r"PANDUAN", r"PEDOMAN", r"PETUNJUK", r"TUTORIAL") or "PEDOMAN" in head[:120]:
        return "11_Panduan & Pedoman"

    # 18. SOP / POB / Standar Pelayanan
    if f in ("AKADEMIK", "KEMAHASISWAAN", "STANDAR PELAYANAN PUBLIC") or has(
            t, r"\bPOB\b", r"\bSOP\b", r"STANDAR(D)? PELAYANAN", r"^SPP ") or has(
            head, r"\bPOB\b", r"STANDAR OPERASIONAL", r"STANDAR PELAYANAN"):
        return "14_SOP, POB & Standar Pelayanan"

    # 19. Materi sosialisasi
    if "SOSIALISASI" in t:
        return "15_Materi Sosialisasi"

    return "16_Lain-lain"

def main():
    dry = "--dry-run" in sys.argv
    manifest = {m["relative_path"]: m for m in json.load(open(os.path.join(BASE, "manifest.json")))}
    rows = []
    for root, _, files in os.walk(SRC):
        for fn in sorted(files):
            if not fn.lower().endswith(".pdf"):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, SRC)
            folder = rel.split(os.sep)[0]
            title = manifest.get(rel, {}).get("title") or os.path.splitext(fn)[0]
            cat = classify(title, folder, fn, first_page_text(path))
            if cat is None:
                continue
            rows.append((rel, folder, title, cat))

    used = set()
    out_rows = []
    for rel, folder, title, cat in sorted(rows, key=lambda r: r[0]):
        fn = os.path.basename(rel)
        target = os.path.join(cat, fn)
        if target.lower() in used:  # nama sama dari folder berbeda -> beri suffix folder asal
            stem, ext = os.path.splitext(fn)
            target = os.path.join(cat, f"{stem}__{re.sub(r'[^A-Za-z0-9]+', '-', folder).strip('-')}{ext}")
        used.add(target.lower())
        out_rows.append((rel, folder, title, cat, target))
        if not dry:
            dst = os.path.join(DST, target)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                try:
                    os.link(os.path.join(SRC, rel), dst)
                except OSError:
                    import shutil; shutil.copy2(os.path.join(SRC, rel), dst)

    with open(os.path.join(BASE, "classification.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file_asal", "folder_asal", "judul", "kategori", "file_tujuan"])
        w.writerows(out_rows)

    from collections import Counter
    for k, v in sorted(Counter(r[3] for r in out_rows).items()):
        print(f"{v:5d}  {k}")
    print(f"{len(out_rows):5d}  TOTAL")

if __name__ == "__main__":
    main()
