#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NSRL Generic Hash Extractor
============================
NSRL Modern / iOS / Android / Legacy veritabanlarindan OS/PKG filtreleri ile
Magnet Axiom uyumlu .db dosyasi uretir. Opsiyonel olarak delta SQL dosyasini
base DB uzerine uygular (base + delta -> merged.db), sonra filtreler.

Ozellikler:
- Tum datasetlerde ayni NSRL semasi (FILE, MFG, OS, PKG, VERSION + DISTINCT_HASH view)
- Hardware analizi (RAM, disk tipi) ile dinamik cache/batch ayari
- Windows BACKGROUND mode (CPU+I/O dusuk oncelik) -> sistem kilitlenmez
- Batch/checkpoint/throttling ile stabilite
- Delta SQL dosyasini parca parca uygular (1+ GB SQL, RAM'e yuklenmeden)
"""

import sqlite3
import sys
import os
import time
import shutil
import argparse
import signal
import subprocess
import ctypes
import threading
import queue as _queue_mod
from datetime import timedelta

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        os.system('chcp 65001 >nul 2>&1')
    except Exception:
        pass


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS FILE (
 sha256     VARCHAR NOT NULL,
 sha1       VARCHAR NOT NULL,
 md5        VARCHAR NOT NULL,
 crc32      VARCHAR NOT NULL,
 file_name  VARCHAR NOT NULL,
 file_size  INTEGER NOT NULL,
 package_id INTEGER NOT NULL,
 CONSTRAINT PK_FILE__FILE PRIMARY KEY (sha256, sha1, md5, crc32, file_name, file_size, package_id)
);

CREATE TABLE IF NOT EXISTS MFG (
 manufacturer_id INTEGER NOT NULL,
 name            VARCHAR NOT NULL,
 CONSTRAINT PK_MFG__MFG_ID PRIMARY KEY (manufacturer_id)
);

CREATE TABLE IF NOT EXISTS OS (
 operating_system_id INTEGER NOT NULL,
 name                VARCHAR NOT NULL,
 version             VARCHAR NOT NULL,
 manufacturer_id     INTEGER NOT NULL,
 CONSTRAINT PK_OS__OS_ID PRIMARY KEY (operating_system_id, manufacturer_id)
);

CREATE TABLE IF NOT EXISTS PKG (
 package_id          INTEGER NOT NULL,
 name                VARCHAR NOT NULL,
 version             VARCHAR NOT NULL,
 operating_system_id INTEGER NOT NULL,
 manufacturer_id     INTEGER NOT NULL,
 language            VARCHAR NOT NULL,
 application_type    VARCHAR NOT NULL,
 CONSTRAINT PK_PGK__PKG_ID PRIMARY KEY (package_id, operating_system_id, manufacturer_id, language, application_type)
);

CREATE TABLE IF NOT EXISTS VERSION (
 version      VARCHAR UNIQUE NOT NULL,
 build_set    VARCHAR NOT NULL,
 build_date   TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
 release_date TIMESTAMP NOT NULL,
 description  VARCHAR NOT NULL,
 CONSTRAINT PK_VERSION__VERSION PRIMARY KEY (version)
);
"""

VIEW_SQL = """
CREATE VIEW IF NOT EXISTS DISTINCT_HASH AS
 SELECT DISTINCT sha256, sha1, md5, crc32 FROM FILE;
"""

# Filtre metadata tablosu — sonradan append-delta icin (hangi filtre uygulanmisti)
META_SQL = """
CREATE TABLE IF NOT EXISTS _filter_meta (
 key   VARCHAR NOT NULL PRIMARY KEY,
 value VARCHAR NOT NULL
);
"""

NORMAL_PRIORITY_CLASS = 0x00000020   # varsayilan Windows onceligi


def set_process_priority():
    """Windows: process'i NORMAL priority'ye al (maksimum verimlilik)."""
    if sys.platform != 'win32':
        return False
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        return bool(ctypes.windll.kernel32.SetPriorityClass(handle, NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


enable_background_mode = set_process_priority  # backward-compat


def unique_output_path(path):
    """Ayni isimde .db / -wal / -shm varsa _1, _2 ... ekler; bos slot bulana kadar."""
    def taken(p):
        return any(os.path.exists(p + suf) for suf in ('', '-wal', '-shm'))
    if not taken(path):
        return path
    root, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = f"{root}_{n}{ext}"
        if not taken(candidate):
            return candidate
        n += 1


def format_time(seconds):
    return str(timedelta(seconds=int(seconds)))


def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def analyze_hardware(target_db):
    """
    Sistem donanımını detaylı analiz edip ölçeklenen performans profili üretir.
    Taşınabilir: Her sistemde (16GB-128GB RAM, 1-48 CPU, SSD/HDD/USB) otomatik ayarlar.
    """
    print(f"\n{'='*70}")
    print(f"  Donanım Analizi ve Performans Profili")
    print(f"{'='*70}")

    cpu_count = os.cpu_count() or 1

    # Başlangıç değerleri — RAM/disk analizi sonrası üzerine yazılır
    config = {
        "total_ram_gb": 0.0, "avail_ram_gb": 0.0,
        "target_cache_size": -524288,   # 512 MB
        "src_cache_size":   -131072,    # 128 MB (per reader)
        "disk_type": "Unknown", "is_os_drive": False,
        "batch_size": 100000,
        "num_threads": min(max(2, cpu_count - 2), 8),
        "max_utilization": 0.72,   # hedef disk kullanımı ~%70-75
        "read_max_utilization": 0.72,
        "read_parallelism": 1,
        "file_copy_mode": "direct",
        "min_sleep": 0.02,         # WAL-mode'da batch_dur≈0 olur; garantili pause (SSD için üzerine yazılır)
    }

    # --- RAM analizi ---
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        config["total_ram_gb"] = stat.ullTotalPhys / (1024 ** 3)
        config["avail_ram_gb"] = stat.ullAvailPhys / (1024 ** 3)
        avail = config["avail_ram_gb"]
        num_t = config["num_threads"]

        # Toplam SQLite cache bütçesi: mevcut RAM'ın %55'i, max 32GB
        cache_budget_mb = min(avail * 1024 * 0.55, 32768)

        # Target cache: bütçenin %35'i, min 512MB, max 8GB
        target_mb = min(max(cache_budget_mb * 0.35, 512), 8192)

        # Src cache per reader: kalan bütçeyi thread'lere böl, min 128MB, max 2GB
        remaining_mb = cache_budget_mb - target_mb
        src_mb = min(max(remaining_mb / max(num_t, 1), 128), 2048)

        config["target_cache_size"] = -int(target_mb * 1024)   # KB cinsinden negatif
        config["src_cache_size"]    = -int(src_mb   * 1024)

        total_used_mb = target_mb + num_t * src_mb
        print(f"  RAM      : Toplam {config['total_ram_gb']:.1f} GB / Boş {avail:.1f} GB")
        print(f"  Cache    : target={target_mb:.0f} MB + src={src_mb:.0f} MB × {num_t} thread "
              f"= {total_used_mb:.0f} MB SQLite")
    except Exception as e:
        print(f"  RAM analizi başarısız ({e}), varsayılan kullanılacak.")

    # --- Disk analizi ---
    target_drive = os.path.splitdrive(os.path.abspath(target_db))[0].upper() or "C:"
    sys_drive = os.environ.get('SystemDrive', 'C:').upper()
    config["is_os_drive"] = (target_drive == sys_drive)
    print(f"  Sürücü   : {target_drive} (OS diski: {'EVET' if config['is_os_drive'] else 'HAYIR'})")

    try:
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        ps_cmd = (f"Get-Partition -DriveLetter {target_drive[0]} | "
                  f"Get-Disk | Get-PhysicalDisk | Select-Object -ExpandProperty MediaType")
        r = subprocess.run(["powershell", "-Command", ps_cmd],
                           capture_output=True, text=True, creationflags=cflags)
        media = r.stdout.strip().upper()
        if not media:
            ps2 = "Get-WmiObject Win32_DiskDrive | Select-Object -ExpandProperty MediaType"
            r2 = subprocess.run(["powershell", "-Command", ps2],
                                capture_output=True, text=True, creationflags=cflags)
            media = r2.stdout.strip().upper().replace('\n', ' ')
        if "SSD" in media or "NVME" in media:
            config["disk_type"] = "SSD"
        elif "HDD" in media or "FIXED HARD DISK" in media:
            config["disk_type"] = "HDD"
        elif "USB" in media or "REMOVABLE" in media:
            config["disk_type"] = "USB"
        else:
            config["disk_type"] = "Virtual/Unknown"
    except Exception:
        pass
    print(f"  Disk     : {config['disk_type']}")

    # --- Batch, thread ve mmap: disk tipine ve CPU'ya göre ---
    if config["disk_type"] == "SSD":
        # OS diskinde: tek reader thread (dogrudan kopyalama gibi), buyuk batch.
        # Ayri diskte: 2-3 paralel reader (daha hizli).
        config["batch_size"]  = 200000 if config["is_os_drive"] else 300000
        config["num_threads"] = 1 if config["is_os_drive"] else min(max(2, cpu_count - 2), 3)
        config["read_parallelism"] = 1 if config["is_os_drive"] else 2
        config["file_copy_mode"] = "parallel"
        mmap_gb = min(config["avail_ram_gb"] * 0.25, 8.0)
        config["mmap_size"]   = int(mmap_gb * 1024 ** 3)
        config["min_sleep"]   = 0.025 if config["is_os_drive"] else 0.015
        if config["is_os_drive"]:
            config["max_utilization"] = 0.70
            config["read_max_utilization"] = 0.70
    elif config["disk_type"] == "HDD":
        config["batch_size"]  = 30000
        config["num_threads"] = 1
        config["read_parallelism"] = 1
        config["mmap_size"]   = 0
        config["min_sleep"]   = 0.0   # HDD zaten seek ile kendi kendini kısıtlar
    elif config["disk_type"] == "USB":
        config["batch_size"]  = 20000
        config["num_threads"] = 1
        config["read_parallelism"] = 1
        config["mmap_size"]   = 0
        config["min_sleep"]   = 0.0
    else:
        config["batch_size"]  = 100000 if config["is_os_drive"] else 200000
        config["num_threads"] = 1 if config["is_os_drive"] else min(max(2, cpu_count // 3), 3)
        config["read_parallelism"] = 1 if config["is_os_drive"] else 2
        config["file_copy_mode"] = "parallel"
        config["mmap_size"]   = 0
        config["min_sleep"]   = 0.05 if config["is_os_drive"] else 0.03
        if config["is_os_drive"]:
            config["max_utilization"] = 0.70
            config["read_max_utilization"] = 0.70

    if config["avail_ram_gb"] > 0:
        cache_budget_mb = min(config["avail_ram_gb"] * 1024 * 0.55, 32768)
        target_mb = min(max(cache_budget_mb * 0.35, 512), 8192)
        remaining_mb = cache_budget_mb - target_mb
        src_mb = min(max(remaining_mb / max(config["num_threads"], 1), 128), 2048)
        config["target_cache_size"] = -int(target_mb * 1024)
        config["src_cache_size"] = -int(src_mb * 1024)
        total_used_mb = target_mb + config["num_threads"] * src_mb
    else:
        target_mb = abs(config["target_cache_size"]) / 1024
        src_mb = abs(config["src_cache_size"]) / 1024
        total_used_mb = target_mb + config["num_threads"] * src_mb

    print(f"  CPU      : {cpu_count} logical → {config['num_threads']} paralel okuma thread'i")
    print(f"  Cache final: target={target_mb:.0f} MB + src={src_mb:.0f} MB × {config['num_threads']} thread "
          f"= {total_used_mb:.0f} MB SQLite")
    print(f"  Batch    : {config['batch_size']:,} satır/batch")
    print(f"  Read cap : ayni anda {config['read_parallelism']} disk okuma, hedef ≤%{int(config['read_max_utilization']*100)}")
    print(f"  mmap     : {format_size(config['mmap_size'])} (kaynak okuma)")
    print(f"  Throttle : hedef disk kullanımı ≤%{int(config['max_utilization']*100)}")
    print(f"{'='*70}\n")
    return config


def _apply_pragmas(conn, hw_config, is_target=True, journal_mode=None):
    # Yeni target DB'ler için DELETE: her commit doğrudan .db'ye yazar (dosya büyür,
    # batch_dur gerçek I/O'yu ölçer → throttle çalışır). Mevcut WAL DB'ler için 'WAL' geç.
    if journal_mode is None:
        journal_mode = 'DELETE' if is_target else 'WAL'
    conn.execute(f"PRAGMA journal_mode = {journal_mode};")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute(f"PRAGMA cache_size = {hw_config['target_cache_size' if is_target else 'src_cache_size']};")
    conn.execute("PRAGMA temp_store = MEMORY;")
    mmap = hw_config.get('mmap_size', 0) if not is_target else 0
    conn.execute(f"PRAGMA mmap_size = {mmap};")
    if journal_mode == 'WAL':
        conn.execute("PRAGMA wal_autocheckpoint = 10000;")
    conn.execute("PRAGMA journal_size_limit = 268435456;")
    conn.execute("PRAGMA cache_spill = 0;")


# -------------------------- PARALLEL FILE COPY --------------------------

def _parallel_file_copy(src_db, tgt_conn, hw_config, pkg_ids_set, hash_types,
                        cancelled_ref, progress_cb=None):
    """
    FILE tablosunu N reader thread ile paralel okuyup tgt_conn'a yazar.
    Her reader kendi bağımsız SQLite bağlantısını açar (WAL çakışma yok).
    pkg_ids_set: Python frozenset — Python düzeyinde filtre (JOIN'den daha hızlı).
    Writer (main thread) result_q'dan alır, executemany ile toplu INSERT.
    Döner: (total_inserted, total_scanned)
    """
    batch_size  = hw_config['batch_size']
    num_threads = hw_config['num_threads']
    src_cache   = hw_config['src_cache_size']
    mmap_sz     = hw_config.get('mmap_size', 0)
    read_parallelism = max(1, int(hw_config.get('read_parallelism', 1)))
    read_util = max(0.10, min(1.0, float(hw_config.get('read_max_utilization', 0.72))))
    read_min_sleep = float(hw_config.get('min_sleep', 0.0))

    # Kaynak rowid aralığını bul
    probe = sqlite3.connect(src_db)
    probe.execute(f"PRAGMA cache_size = {src_cache};")
    probe.execute("PRAGMA query_only = 1;")
    # MIN ve MAX ayri sorgularda O(log N) ile cozulur.
    # Birlikte (MIN(rowid), MAX(rowid)) 432M satirlik tam tarama yapar — ~14 dk bekletir.
    min_rid = probe.execute("SELECT MIN(rowid) FROM FILE").fetchone()[0]
    max_rid = probe.execute("SELECT MAX(rowid) FROM FILE").fetchone()[0]
    probe.close()

    if min_rid is None or max_rid is None:
        return 0, 0
    total_range = max_rid - min_rid + 1

    # Chunk listesi
    chunks = []
    r = min_rid
    while r <= max_rid:
        chunks.append((r, min(r + batch_size - 1, max_rid)))
        r += batch_size
    total_chunks = len(chunks)

    print(f"  [src] ROWID: {min_rid:,} – {max_rid:,} ({total_range:,} satır) | "
          f"{num_threads} thread × batch={batch_size:,} = {total_chunks:,} chunk")

    chunk_q  = _queue_mod.Queue()
    result_q = _queue_mod.Queue(maxsize=num_threads * 3)
    read_gate = threading.Semaphore(read_parallelism)

    for ch in chunks:
        chunk_q.put(ch)
    for _ in range(num_threads):
        chunk_q.put(None)  # her thread için sentinel

    # Hangi hash sütunlarını boşalt
    blank_sha256 = "sha256" not in hash_types
    blank_sha1   = "sha1"   not in hash_types
    blank_md5    = "md5"    not in hash_types
    blank_crc32  = "crc32"  not in hash_types
    any_blank    = blank_sha256 or blank_sha1 or blank_md5 or blank_crc32

    def reader_fn(reader_idx):
        conn = sqlite3.connect(src_db)
        conn.execute(f"PRAGMA cache_size = {src_cache};")
        conn.execute("PRAGMA query_only = 1;")
        if mmap_sz > 0:
            conn.execute(f"PRAGMA mmap_size = {mmap_sz};")
        if reader_idx:
            time.sleep(min(1.0, reader_idx * 0.25))
        try:
            while not cancelled_ref[0]:
                item = chunk_q.get()
                if item is None:
                    break
                lo, hi = item
                read_gate.acquire()
                try:
                    rs = time.time()
                    rows = conn.execute(
                        "SELECT sha256, sha1, md5, crc32, file_name, file_size, package_id "
                        "FROM FILE WHERE rowid BETWEEN ? AND ?", (lo, hi)
                    ).fetchall()
                    read_dur = time.time() - rs
                    read_sleep = read_dur * (1.0 / read_util - 1.0) if read_util < 1.0 and read_dur > 0 else 0.0
                    if read_sleep > 0 or read_min_sleep > 0:
                        time.sleep(max(read_min_sleep, min(2.0, read_sleep)))
                finally:
                    read_gate.release()
                filtered = []
                for rw in rows:
                    if rw[6] in pkg_ids_set:
                        if any_blank:
                            rw = (
                                "" if blank_sha256 else rw[0],
                                "" if blank_sha1   else rw[1],
                                "" if blank_md5    else rw[2],
                                "" if blank_crc32  else rw[3],
                                rw[4], rw[5], rw[6],
                            )
                        filtered.append(rw)
                # Backpressure: result_q doluysa bekle, ama cancel'a duyarlı kal
                while True:
                    try:
                        result_q.put((lo, hi, filtered), timeout=0.5)
                        break
                    except _queue_mod.Full:
                        if cancelled_ref[0]:
                            return
        except Exception as exc:
            try:
                result_q.put(("__error__", str(exc), []), timeout=2.0)
            except _queue_mod.Full:
                pass
        finally:
            conn.close()

    threads = []
    for idx in range(num_threads):
        t = threading.Thread(target=reader_fn, args=(idx,), daemon=True)
        t.start()
        threads.append(t)

    print(f"  {num_threads} reader thread baslatildi; disk okuma kapisi={read_parallelism}, "
          f"hedef read ≤%{int(read_util*100)}.")
    print(f"  Ilk progress, ilk {batch_size:,} satirlik chunk okunup filtrelenince gelir...")
    sys.stdout.flush()

    total_inserted = 0
    total_scanned  = 0
    chunks_done    = 0
    batch_n        = 0
    last_wait_msg  = time.time()
    wait_ticks     = 0

    while chunks_done < total_chunks and not cancelled_ref[0]:
        try:
            item = result_q.get(timeout=1.0)
        except _queue_mod.Empty:
            now = time.time()
            if now - last_wait_msg >= 10:
                wait_ticks += 1
                print(f"  [bekleniyor] reader'lar kaynak FILE okuyor... "
                      f"tamamlanan chunk={chunks_done:,}/{total_chunks:,} "
                      f"({wait_ticks * 10}s)")
                sys.stdout.flush()
                last_wait_msg = now
            if not any(t.is_alive() for t in threads):
                break
            continue

        lo, hi, filtered = item
        if lo == "__error__":
            print(f"\n  [HATA] Reader thread hatası: {hi}")
            cancelled_ref[0] = True
            break

        chunks_done   += 1
        total_scanned += hi - lo + 1
        batch_n       += 1

        ws = time.time()
        if filtered:
            tgt_conn.executemany(
                "INSERT OR IGNORE INTO main.FILE "
                "(sha256, sha1, md5, crc32, file_name, file_size, package_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                filtered,
            )
            tgt_conn.commit()
            total_inserted += len(filtered)
        # DELETE journal: commit süresi gerçek disk I/O'yu yansıtır → throttle çalışır
        # Writer uyuyunca result_q dolar → reader'lar da yavaşlar (backpressure)
        bd     = time.time() - ws
        util   = hw_config.get('max_utilization', 1.0)
        min_sl = hw_config.get('min_sleep', 0.0)
        sleep  = (bd * (1.0 / util - 1.0)) if util < 1.0 and bd > 0 else 0.0
        time.sleep(max(min_sl, min(sleep, 2.0)))

        if progress_cb:
            progress_cb(chunks_done, total_chunks, total_inserted, total_scanned)

    for t in threads:
        t.join(timeout=2.0)

    return total_inserted, total_scanned


# -------------------------- DELTA APPLY --------------------------

def copy_with_progress(src, dst, chunk_mb=256):
    """shutil.copyfile yerine progress'li kopyalama. 181 GB'lik base icin
    sessizce beklemek yerine satir satir ilerlemeyi gosterir."""
    total = os.path.getsize(src)
    chunk = chunk_mb * 1024 * 1024
    written = 0
    t_start = time.time()
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            written += len(buf)
            elapsed = time.time() - t_start
            pct = (written / total * 100) if total else 0
            speed = written / elapsed if elapsed else 0
            eta = (total - written) / speed if speed > 0 else 0
            sys.stdout.write(
                f"\r  {pct:5.1f}% | {format_size(written)}/{format_size(total)} | "
                f"{format_size(speed)}/s | Kalan: ~{format_time(eta)}   "
            )
            sys.stdout.flush()
    sys.stdout.write("\n")


def apply_delta_lite(delta_sql, additions_db, hw_config):
    """
    HIZLI DELTA: Base DB'yi (181 GB) kopyalamak YERINE,
    delta SQL'deki sadece INSERT satirlarini kucuk bir additions.db'ye yazar.
    Sonra filtreleme asamasinda base + additions iki kaynak olarak birlikte kullanilir.

    Kazanc: ~5-15 dk (base kopyalama atlanir).
    Trade-off: Delta'daki DELETE/UPDATE'ler atlanir. Hash set icin zararsiz:
      - DELETE: NSRL'nin sildigi kayit hala 'known-good' oldugundan kalmasi sorun degil.
      - UPDATE: Genelde metadata degisikligi (dosya adi gibi), hash degismez.
    """
    # WAL/SHM/main kalintilarini temizle
    for suf in ['', '-wal', '-shm']:
        p = additions_db + suf
        if os.path.exists(p):
            os.remove(p)

    conn = sqlite3.connect(additions_db)
    _apply_pragmas(conn, hw_config, is_target=True)
    conn.executescript(SCHEMA_SQL)

    delta_size = os.path.getsize(delta_sql)
    print(f"\n[DELTA-LITE] SQL uygulaniyor -> {os.path.basename(additions_db)}")
    print(f"  Kaynak: {format_size(delta_size)} | base kopyalanmadi (hizli mod)")

    BATCH = 10000
    pending = []        # bekleyen (INSERT OR IGNORE, sql) tuple listesi
    bytes_read = 0
    inserts_applied = 0
    inserts_errored = 0
    non_inserts_skipped = 0
    t_start = time.time()
    cur = conn.cursor()

    def flush():
        nonlocal pending, inserts_errored
        if not pending:
            return
        conn.execute("BEGIN")
        for sql in pending:
            try:
                cur.execute(sql)
            except sqlite3.Error:
                inserts_errored += 1
        conn.commit()
        pending = []

    stmt_acc = []   # çok satırlı INSERT biriktirici
    in_insert = False

    with open(delta_sql, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            bytes_read += len(line.encode('utf-8', errors='replace'))
            s = line.strip()

            if not in_insert:
                if not s or s in ("BEGIN TRANSACTION;", "COMMIT;", "BEGIN;"):
                    continue
                if s.upper().startswith("INSERT INTO "):
                    # INSERT OR IGNORE olarak yeniden yaz, INTO sonrasını koru
                    rewritten = "INSERT OR IGNORE INTO " + s[len("INSERT INTO "):]
                    stmt_acc = [rewritten]
                    in_insert = True
                else:
                    non_inserts_skipped += 1
                    continue
            else:
                stmt_acc.append(s)

            # Noktalı virgül ile biten satır → ifade tamamlandı
            if stmt_acc and stmt_acc[-1].rstrip().endswith(";"):
                full_stmt = " ".join(stmt_acc)
                pending.append(full_stmt)
                stmt_acc = []
                in_insert = False
                inserts_applied += 1

                if len(pending) >= BATCH:
                    bs = time.time()
                    flush()
                    bd = time.time() - bs
                    sleep = bd * (1.0 / hw_config['max_utilization'] - 1.0)
                    time.sleep(max(hw_config['min_sleep'], min(2.0, sleep)))

                    pct = (bytes_read / delta_size) * 100
                    el  = time.time() - t_start
                    eta = (el / bytes_read) * (delta_size - bytes_read) if bytes_read else 0
                    db_sz = os.path.getsize(additions_db) if os.path.exists(additions_db) else 0
                    sys.stdout.write(
                        f"\r  {pct:5.1f}% | {format_size(bytes_read)}/{format_size(delta_size)} | "
                        f"INSERT: {inserts_applied:,} | DB: {format_size(db_sz)} | "
                        f"Gecen: {format_time(el)} | Kalan: ~{format_time(eta)}   "
                    )
                    sys.stdout.flush()

    flush()
    sys.stdout.write(f"\r  Son commit tamamlandi...                                                   \n")
    sys.stdout.flush()
    conn.close()
    err_note = f", {inserts_errored} hatali atlandi" if inserts_errored else ""
    print(f"  [OK] {inserts_applied:,} INSERT uygulandi, {non_inserts_skipped} non-INSERT atlandi{err_note} "
          f"({format_time(time.time()-t_start)})")
    print(f"  Additions DB boyutu: {format_size(os.path.getsize(additions_db))}")
    return additions_db


def apply_delta(base_db, delta_sql, merged_db, hw_config):
    """
    TAM MERGE MODU (yavas): Base DB'nin bir kopyasini alip delta SQL'i parca parca uygular.
    1+ GB SQL dosyasini RAM'e yuklemeden, ifade ifade isler.
    DELETE/UPDATE ifadeleri de dahil uygulanir. --full-merge flag'i ile tetiklenir.
    """
    print(f"\n[DELTA] Base kopyalaniyor -> {os.path.basename(merged_db)}")
    print(f"        Kaynak: {format_size(os.path.getsize(base_db))} -> ~5-15 dk (SSD), ~30-60 dk (HDD)")
    t0 = time.time()
    # WAL/SHM kalintilarini temizle
    for suf in ['', '-wal', '-shm']:
        p = merged_db + suf
        if os.path.exists(p):
            os.remove(p)
    copy_with_progress(base_db, merged_db)
    print(f"  [OK] Kopya tamam ({format_time(time.time()-t0)}, {format_size(os.path.getsize(merged_db))})")

    conn = sqlite3.connect(merged_db)
    _apply_pragmas(conn, hw_config, is_target=True, journal_mode='WAL')  # base kopyası, WAL koru

    delta_size = os.path.getsize(delta_sql)
    print(f"\n[DELTA] SQL uygulaniyor: {format_size(delta_size)}")
    print(f"  (Dosya satir satir okunuyor, RAM'e yuklenmiyor)")

    BATCH = 10000  # ifade sayisi — %80 kapasite hedefi icin yukseltildi
    buffer = ["BEGIN TRANSACTION;"]
    count = 0
    bytes_read = 0
    t_start = time.time()
    cur = conn.cursor()

    def flush():
        nonlocal buffer, count
        if count == 0:
            return
        buffer.append("COMMIT;")
        cur.executescript("\n".join(buffer))
        conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        buffer = ["BEGIN TRANSACTION;"]
        count = 0

    with open(delta_sql, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            bytes_read += len(line.encode('utf-8', errors='replace'))
            s = line.strip()
            if not s or s == "BEGIN TRANSACTION;" or s == "COMMIT;":
                continue
            # Duplicate safety: INSERT INTO -> INSERT OR IGNORE INTO
            # (delta zaten uygulanmis base'lere de uygulanabilir).
            if s.startswith("INSERT INTO "):
                s = "INSERT OR IGNORE INTO " + s[len("INSERT INTO "):]
            buffer.append(s)
            count += 1
            if count >= BATCH:
                batch_start = time.time()
                flush()
                batch_dur = time.time() - batch_start
                sleep = batch_dur * (1.0 / hw_config['max_utilization'] - 1.0)
                sleep = max(hw_config['min_sleep'], min(3.0, sleep))
                time.sleep(sleep)

                pct = (bytes_read / delta_size) * 100
                el = time.time() - t_start
                eta = (el / bytes_read) * (delta_size - bytes_read) if bytes_read else 0
                merged_size = os.path.getsize(merged_db) if os.path.exists(merged_db) else 0
                sys.stdout.write(
                    f"\r  {pct:5.1f}% | {format_size(bytes_read)}/{format_size(delta_size)} | "
                    f"Merged: {format_size(merged_size)} | "
                    f"Gecen: {format_time(el)} | Kalan: ~{format_time(eta)}   "
                )
                sys.stdout.flush()

    flush()
    print(f"\n  [OK] Delta uygulandi ({format_time(time.time()-t_start)})")

    # WAL truncate + analiz
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()
    return merged_db


# -------------------------- DEDUPLICATION --------------------------

def dedup_file_table(db_path):
    """
    FILE tablosundaki hash-level duplicate satirlari siler.
    Her benzersiz (sha256, sha1, md5, crc32) kombinasyonu icin tek bir satir tutar.
    Secilmemis hash kolonlari '' oldugundan, GROUP BY yalnizca dolu kolonlari etkili sekilde kullanir.
    Geri donus: (before_count, after_count)
    """
    print(f"\n[DEDUP] Basliyor: {os.path.basename(db_path)}", flush=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -524288")   # 512 MB
    conn.execute("PRAGMA temp_store = FILE")

    t0 = time.time()

    before = conn.execute("SELECT COUNT(*) FROM FILE").fetchone()[0]
    print(f"  FILE satirlari (onceki) : {before:,}", flush=True)

    distinct = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM FILE GROUP BY sha256, sha1, md5, crc32)"
    ).fetchone()[0]
    duplicates = before - distinct

    if duplicates == 0:
        print(f"  Duplicate hash yok — islem atlandi.", flush=True)
        conn.close()
        return before, before

    print(f"  Duplicate satir sayisi  : {duplicates:,}  (%{duplicates/before*100:.1f})", flush=True)
    print(f"  Hedef satir sayisi      : {distinct:,}", flush=True)

    # Mevcut basarisiz calismadan kalma tablo varsa temizle
    conn.execute("DROP TABLE IF EXISTS _FILE_DEDUP")

    print(f"  Yeni tablo olusturuluyor...", flush=True)
    conn.execute("""
        CREATE TABLE _FILE_DEDUP (
            sha256     VARCHAR NOT NULL,
            sha1       VARCHAR NOT NULL,
            md5        VARCHAR NOT NULL,
            crc32      VARCHAR NOT NULL,
            file_name  VARCHAR NOT NULL,
            file_size  INTEGER NOT NULL,
            package_id INTEGER NOT NULL,
            CONSTRAINT PK_FILE__FILE PRIMARY KEY (sha256, sha1, md5, crc32, file_name, file_size, package_id)
        )
    """)

    print(f"  Benzersiz satirlar aktariliyor (bu asama uzun surebilir)...", flush=True)
    conn.execute("""
        INSERT INTO _FILE_DEDUP (sha256, sha1, md5, crc32, file_name, file_size, package_id)
        SELECT sha256, sha1, md5, crc32,
               MIN(file_name), MIN(file_size), MIN(package_id)
        FROM   FILE
        GROUP  BY sha256, sha1, md5, crc32
    """)
    conn.commit()

    print(f"  Tablo degistirme...", flush=True)
    conn.execute("DROP VIEW IF EXISTS DISTINCT_HASH")
    conn.execute("DROP TABLE FILE")
    conn.execute("ALTER TABLE _FILE_DEDUP RENAME TO FILE")
    conn.execute("CREATE VIEW DISTINCT_HASH AS SELECT DISTINCT sha256, sha1, md5, crc32 FROM FILE")
    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM FILE").fetchone()[0]
    elapsed_min = (time.time() - t0) / 60
    print(f"  [OK] {before:,} → {after:,} satir  ({before-after:,} duplicate silindi, {elapsed_min:.1f} dk)", flush=True)

    print(f"  VACUUM: disk alani geri kazaniliyor...", flush=True)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("VACUUM")
    conn.close()

    sz_gb = os.path.getsize(db_path) / (1024 ** 3)
    print(f"  Son DB boyutu: {sz_gb:.2f} GB", flush=True)
    print(f"[DEDUP] Tamamlandi.", flush=True)
    return before, after


# -------------------------- EXTRACT / FILTER --------------------------

HASH_COLS = ("sha256", "sha1", "md5", "crc32")


def build_file_select(hash_types):
    """FILE tablosu icin SELECT sutun listesi. Secilmeyen hash'ler '' olur."""
    parts = []
    for c in HASH_COLS:
        if c in hash_types:
            parts.append(f"f.{c}")
        else:
            parts.append(f"'' AS {c}")
    parts += ["f.file_name", "f.file_size", "f.package_id"]
    return ", ".join(parts)


def extract_filtered(source_db, target_db, os_filter="%", os_list_file=None,
                     pkg_filter=None, hash_types=None, additions_db=None,
                     do_vacuum=False, dataset=None, append_mode=False):
    """
    Source DB'den OS filtreleri ile Axiom-uyumlu filtrelenmis DB uretir.
    additions_db verilirse (delta-lite): ikinci kaynak olarak ATTACH edilir, OS/MFG/PKG/FILE
      her iki kaynaktan da eklenir (base + delta'daki yeni satirlar).
    do_vacuum=False: VACUUM atlanir (~3-10 dk kazanc, dosya biraz daha buyuk).
    dataset: sonradan --append-delta icin DB'ye yazilir (hangi dataset filtreleri).
    """
    if hash_types is None:
        hash_types = set(HASH_COLS)
    hash_types = {h for h in hash_types if h in HASH_COLS}
    if not hash_types:
        print(f"[HATA] En az bir hash turu secilmeli. Secenekler: {HASH_COLS}")
        sys.exit(1)
    file_select = build_file_select(hash_types)
    print(f"\n{'='*70}")
    print(f"  NSRL Hash Extractor")
    print(f"{'='*70}")
    print(f"  Kaynak : {source_db}")
    print(f"  Hedef  : {target_db}")
    if os_list_file:
        print(f"  Filtre : OS Listesi dosyasi ({os_list_file})")
    else:
        print(f"  Filtre : OS name LIKE '{os_filter}'")
    if pkg_filter:
        print(f"  PKG    : PKG name LIKE '{pkg_filter}'")
    kept = [h for h in HASH_COLS if h in hash_types]
    dropped = [h for h in HASH_COLS if h not in hash_types]
    print(f"  Hash   : {'+'.join(kept).upper()}" + (f" (bos: {','.join(dropped)})" if dropped else ""))
    print(f"{'='*70}\n")

    if not os.path.exists(source_db):
        print(f"[HATA] Kaynak veritabani bulunamadi: {source_db}")
        sys.exit(1)

    src_size = os.path.getsize(source_db)
    print(f"  Kaynak boyut: {format_size(src_size)}")
    if additions_db and os.path.exists(additions_db):
        print(f"  Additions  : {format_size(os.path.getsize(additions_db))} (delta-lite)")

    if not append_mode:
        for suf in ['', '-wal', '-shm']:
            p = target_db + suf
            if os.path.exists(p):
                print(f"  [UYARI] Siliniyor: {os.path.basename(p)}")
                os.remove(p)
        target_dir = os.path.dirname(target_db)
        if target_dir and not os.path.exists(target_dir):
            os.makedirs(target_dir)

    set_process_priority()
    hw_config = analyze_hardware(target_db)

    total_start   = time.time()
    cancelled_ref = [False]

    # ATTACH edilecek kaynaklar: src (base) + opsiyonel add_ (delta additions)
    sources = [("src", source_db)]
    if additions_db and os.path.exists(additions_db):
        sources.append(("add_", additions_db))

    if append_mode:
        print(f"\n[1/8] Mevcut DB'ye ekleniyor (append modu)...")
        tgt = sqlite3.connect(target_db)
        _apply_pragmas(tgt, hw_config, is_target=True, journal_mode='WAL')
        tgt.executescript(SCHEMA_SQL)   # IF NOT EXISTS — guvenli
        tgt.executescript(META_SQL)
        print(f"  [OK] Mevcut DB acildi: {os.path.basename(target_db)}")
    else:
        print(f"\n[1/8] Hedef DB olusturuluyor...")
        tgt = sqlite3.connect(target_db)
        _apply_pragmas(tgt, hw_config, is_target=True)
        tgt.executescript(SCHEMA_SQL)
        tgt.executescript(META_SQL)
        meta = {
            "dataset": dataset or "",
            "os_filter": os_filter or "%",
            "pkg_filter": pkg_filter or "",
            "hash_types": ",".join(sorted(hash_types)) if hash_types else "",
            "os_list": "",
            "extraction_status": "running",
            "file_count": "0",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if os_list_file and os.path.exists(os_list_file):
            with open(os_list_file, "r", encoding="utf-8") as f:
                meta["os_list"] = "\n".join(line.strip() for line in f if line.strip())
        tgt.executemany("INSERT OR REPLACE INTO _filter_meta (key, value) VALUES (?, ?)",
                        list(meta.items()))
        tgt.commit()
        print(f"  [OK] Sema + filter_meta olusturuldu")

    print(f"\n[2/8] Kaynak(lar) baglaniyor (ATTACH)...")
    for alias, path in sources:
        tgt.execute(f"ATTACH DATABASE '{path.replace(chr(92), '/')}' AS {alias};")
        tgt.execute(f"PRAGMA {alias}.cache_size = {hw_config['src_cache_size']};")
        tgt.execute(f"PRAGMA {alias}.mmap_size = 0;")
        print(f"  [OK] {alias} -> {os.path.basename(path)}")

    print(f"\n[3/8] VERSION kopyalaniyor...")
    t = time.time()
    for alias, _ in sources:
        tgt.execute(f"INSERT OR IGNORE INTO main.VERSION SELECT * FROM {alias}.VERSION;")
    tgt.commit()
    cnt = tgt.execute("SELECT COUNT(*) FROM main.VERSION").fetchone()[0]
    print(f"  [OK] {cnt} kayit ({format_time(time.time()-t)})")

    print(f"\n[4/8] OS filtreleniyor...")
    t = time.time()
    if os_list_file and os.path.exists(os_list_file):
        with open(os_list_file, "r", encoding="utf-8") as f:
            os_names = [line.strip() for line in f if line.strip()]
        tgt.execute("CREATE TEMP TABLE os_filter_list (name VARCHAR);")
        tgt.executemany("INSERT INTO temp.os_filter_list VALUES (?)", [(n,) for n in os_names])
        for alias, _ in sources:
            tgt.execute(f"""
                INSERT OR IGNORE INTO main.OS
                SELECT o.* FROM {alias}.OS o
                INNER JOIN temp.os_filter_list t ON o.name = t.name;
            """)
        tgt.execute("DROP TABLE temp.os_filter_list;")
    else:
        for alias, _ in sources:
            tgt.execute(f"INSERT OR IGNORE INTO main.OS SELECT * FROM {alias}.OS WHERE name LIKE '{os_filter}';")
    tgt.commit()
    os_count = tgt.execute("SELECT COUNT(*) FROM main.OS").fetchone()[0]
    print(f"  [OK] {os_count} OS kaydi ({format_time(time.time()-t)})")

    if os_count == 0:
        print(f"\n[UYARI] Hic OS kaydi bulunamadi! Filtre kriterlerinizi kontrol edin.")
        for alias, _ in sources:
            tgt.execute(f"DETACH DATABASE {alias};")
        tgt.close()
        if not append_mode:
            os.remove(target_db)
        sys.exit(1)

    os_rows = tgt.execute("SELECT name, version FROM main.OS ORDER BY name, version").fetchall()
    if len(os_rows) <= 10:
        for n, v in os_rows:
            print(f"    - {n} {v}")
    else:
        for n, v in os_rows[:5]:
            print(f"    - {n} {v}")
        print(f"    ... ({len(os_rows)-10} daha) ...")
        for n, v in os_rows[-5:]:
            print(f"    - {n} {v}")

    print(f"\n[5/8] Ilgili MFG kopyalaniyor...")
    t = time.time()
    for alias, _ in sources:
        tgt.execute(f"""
            INSERT OR IGNORE INTO main.MFG SELECT DISTINCT m.* FROM {alias}.MFG m
            WHERE m.manufacturer_id IN (SELECT DISTINCT manufacturer_id FROM main.OS);
        """)
    tgt.commit()
    mfg_count = tgt.execute("SELECT COUNT(*) FROM main.MFG").fetchone()[0]
    print(f"  [OK] {mfg_count} MFG kaydi ({format_time(time.time()-t)})")

    print(f"\n[6/8] PKG kopyalaniyor (opsiyonel PKG filtresi ile)...")
    t = time.time()
    for alias, _ in sources:
        if pkg_filter:
            tgt.execute(f"""
                INSERT OR IGNORE INTO main.PKG SELECT p.* FROM {alias}.PKG p
                INNER JOIN main.OS o
                    ON p.operating_system_id = o.operating_system_id
                    AND p.manufacturer_id = o.manufacturer_id
                WHERE p.name LIKE '{pkg_filter}';
            """)
        else:
            tgt.execute(f"""
                INSERT OR IGNORE INTO main.PKG SELECT p.* FROM {alias}.PKG p
                INNER JOIN main.OS o
                    ON p.operating_system_id = o.operating_system_id
                    AND p.manufacturer_id = o.manufacturer_id;
            """)
    tgt.commit()
    pkg_count = tgt.execute("SELECT COUNT(*) FROM main.PKG").fetchone()[0]
    print(f"  [OK] {pkg_count:,} PKG kaydi ({format_time(time.time()-t)})")

    for alias, _ in sources:
        tgt.execute(f"""
            INSERT OR IGNORE INTO main.MFG SELECT DISTINCT m.* FROM {alias}.MFG m
            WHERE m.manufacturer_id IN (SELECT DISTINCT manufacturer_id FROM main.PKG);
        """)
    tgt.commit()

    if pkg_count == 0:
        print(f"\n[UYARI] Filtre sonucu hic PKG bulunamadi. Islem durduruluyor.")
        for alias, _ in sources:
            tgt.execute(f"DETACH DATABASE {alias};")
        tgt.close()
        if not append_mode:
            os.remove(target_db)
        sys.exit(1)

    print(f"\n  Gecici package_id tablosu olusturuluyor...")
    t = time.time()
    tgt.execute("CREATE TEMP TABLE win_pkg_ids AS SELECT DISTINCT package_id FROM main.PKG;")
    tgt.execute("CREATE INDEX temp.idx_win_pkg ON win_pkg_ids(package_id);")
    tgt.commit()
    pkg_ids_rows = tgt.execute("SELECT package_id FROM temp.win_pkg_ids").fetchall()
    pkg_ids_set  = frozenset(r[0] for r in pkg_ids_rows)
    print(f"  [OK] {len(pkg_ids_set):,} package_id ({format_time(time.time()-t)})")

    # Reader thread'ler source_db'ye bagimsiz SQLite baglantilari acar.
    # tgt uzerindeki ATTACH lock'u cakismasin diye src DETACH ediyoruz.
    tgt.execute("DETACH DATABASE src;")
    sources = [(a, p) for a, p in sources if a != "src"]

    n_threads = hw_config['num_threads']
    mode_label = (
        f"dogrudan kopyalama, 1 reader, disk<=%{int(hw_config['max_utilization']*100)}, batch={hw_config['batch_size']:,}"
        if n_threads == 1
        else f"{n_threads} reader thread, disk<=%{int(hw_config['max_utilization']*100)}, batch={hw_config['batch_size']:,}"
    )
    print(f"\n[7/8] FILE kopyalaniyor... ({mode_label})")
    file_start = time.time()
    total_inserted = 0

    def sigh(sig, frame):
        print(f"\n\n  [UYARI] Ctrl+C algilandi, batch bitince duracak...")
        cancelled_ref[0] = True
    old_h = signal.signal(signal.SIGINT, sigh)

    try:
        def _prog(chunks_done, total_chunks, ins, _scanned):
            elapsed = time.time() - file_start
            pct     = chunks_done / total_chunks * 100 if total_chunks else 0
            eta_str = (format_time(elapsed * (total_chunks - chunks_done) / chunks_done)
                       if chunks_done else "?")
            db_sz   = os.path.getsize(target_db) if os.path.exists(target_db) else 0
            line    = (f"  {pct:5.1f}% [{chunks_done:,}/{total_chunks:,}] "
                       f"Hash: {ins:,} | DB: {format_size(db_sz)} | "
                       f"Gecen: {format_time(elapsed)} | Kalan: ~{eta_str}")
            end = "\n" if chunks_done % 25 == 0 else "\r"
            sys.stdout.write(f"\r{line}   {end}")
            sys.stdout.flush()

        fi_count, _fi_scanned = _parallel_file_copy(
            source_db, tgt, hw_config, pkg_ids_set, hash_types, cancelled_ref, _prog
        )
        total_inserted += fi_count

        # --- ADDITIONS (küçük kaynak): tek query ---
        if not cancelled_ref[0] and any(a == "add_" for a, _ in sources):
            print(f"\n  [add_] additions.db'den FILE ekleniyor...")
            ab = time.time()
            tgt.execute(f"""
                INSERT OR IGNORE INTO main.FILE (sha256, sha1, md5, crc32, file_name, file_size, package_id)
                SELECT {file_select} FROM add_.FILE f
                WHERE EXISTS (SELECT 1 FROM temp.win_pkg_ids w WHERE w.package_id = f.package_id);
            """)
            tgt.commit()
            add_inserted = tgt.execute("SELECT changes();").fetchone()[0]
            total_inserted += add_inserted
            print(f"  [add_] {add_inserted:,} ek FILE ({format_time(time.time()-ab)})")
    finally:
        signal.signal(signal.SIGINT, old_h)

    total_files = tgt.execute("SELECT COUNT(*) FROM main.FILE").fetchone()[0]
    print(f"\n  [OK] {total_files:,} FILE kaydi toplam ({format_time(time.time()-file_start)})")

    if cancelled_ref[0]:
        print(f"  [UYARI] Islem iptal edildi, kismi veri kaydedildi.")

    print(f"\n[8/8] View + finalize...")
    t = time.time()
    tgt.executescript(VIEW_SQL)
    for alias, _ in sources:
        tgt.execute(f"DETACH DATABASE {alias};")
    tgt.execute("DROP TABLE IF EXISTS temp.win_pkg_ids;")
    if do_vacuum:
        print(f"  VACUUM calistiriliyor (birkac dakika)...")
        tgt.execute("VACUUM;")
    else:
        print(f"  VACUUM atlandi (--vacuum ile acilir). Dosya biraz daha buyuk olabilir ama Axiom icin sorunsuz.")
    final_status = "cancelled" if cancelled_ref[0] else ("failed_no_files" if total_files == 0 else "completed")
    tgt.executemany(
        "INSERT OR REPLACE INTO _filter_meta (key, value) VALUES (?, ?)",
        [
            ("extraction_status", final_status),
            ("file_count", str(total_files)),
            ("completed_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ],
    )
    tgt.commit()
    tgt.execute("PRAGMA optimize;")
    tgt.close()
    print(f"  [OK] Finalize tamam ({format_time(time.time()-t)})")

    if cancelled_ref[0]:
        print("[HATA] Islem kullanici tarafindan iptal edildi. Bu DB append/Axiom icin kullanilmamali.")
        sys.exit(130)
    if total_files == 0 and not append_mode:
        print("[HATA] FILE tablosu bos kaldi. Cikti DB gecersiz; filtreyi veya onceki loglari kontrol edin.")
        sys.exit(2)

    total_elapsed = time.time() - total_start
    target_size = os.path.getsize(target_db)
    print(f"\n{'='*70}")
    print(f"  TAMAMLANDI")
    print(f"{'='*70}")
    print(f"  Kaynak   : {format_size(src_size)}")
    print(f"  Hedef    : {format_size(target_size)} (%{(1-target_size/src_size)*100:.1f} kuculme)")
    print(f"  OS/MFG/PKG/FILE: {os_count} / {mfg_count} / {pkg_count:,} / {total_files:,}")
    print(f"  Sure     : {format_time(total_elapsed)}")
    print(f"{'='*70}\n")


# -------------------------- APPEND DELTA --------------------------

def append_delta_to_existing(existing_db, delta_sql, keep_additions=False):
    """
    Mevcut filtrelenmis DB'ye sadece yeni delta'dan uygun satirlari ekler.
    - Mevcut DB'nin _filter_meta'sindan filtre kriterleri okunur (OS listesi, PKG filter, hash types)
    - Delta SQL'den kucuk bir additions.db olusturulur (delta-lite)
    - Additions'tan filtreye uyan OS/MFG/PKG/FILE satirlari mevcut DB'ye INSERT OR IGNORE ile eklenir

    Tam re-run'dan 10-15x daha hizli (~2-5 dk). Mevcut DB yerinde guncellenir.
    """
    if not os.path.exists(existing_db):
        print(f"[HATA] Mevcut DB bulunamadi: {existing_db}")
        sys.exit(1)
    if not os.path.exists(delta_sql):
        print(f"[HATA] Delta SQL bulunamadi: {delta_sql}")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  Append Delta — Mevcut DB'ye yeni delta uygula")
    print(f"{'='*70}")
    print(f"  Hedef DB    : {existing_db}")
    print(f"  Yeni delta  : {delta_sql}")

    # Metadata'yi oku
    tgt = sqlite3.connect(existing_db)
    try:
        meta = dict(tgt.execute("SELECT key, value FROM _filter_meta").fetchall())
    except sqlite3.OperationalError:
        print(f"[HATA] _filter_meta tablosu yok. Bu DB eski versiyonla uretilmis olabilir.")
        print(f"       Cozum: DB'yi bastan olustur (bir kez), sonraki runlardan itibaren append calisir.")
        tgt.close()
        sys.exit(1)

    os_list = [l for l in meta.get("os_list", "").split("\n") if l]
    os_filter = meta.get("os_filter", "%")
    pkg_filter = meta.get("pkg_filter") or None
    hash_types = set(h for h in meta.get("hash_types", "").split(",") if h in HASH_COLS) or set(HASH_COLS)
    dataset = meta.get("dataset", "")
    print(f"  Dataset     : {dataset}")
    print(f"  OS filtre   : {len(os_list)} exact name" if os_list else f"  OS filtre   : LIKE '{os_filter}'")
    print(f"  PKG filtre  : {pkg_filter or '(yok)'}")
    print(f"  Hash turu   : {','.join(sorted(hash_types))}")
    print(f"{'='*70}\n")

    set_process_priority()
    hw_config = analyze_hardware(existing_db)

    # 1) Delta'dan additions.db uret
    out_dir = os.path.dirname(os.path.abspath(existing_db))
    additions_db = os.path.join(out_dir, f"_new_additions_{dataset or 'custom'}.db")
    for suf in ['', '-wal', '-shm']:
        p = additions_db + suf
        if os.path.exists(p):
            os.remove(p)
    apply_delta_lite(delta_sql, additions_db, hw_config)

    # 2) Mevcut DB'ye additions'i ATTACH edip filtreyi uygula
    total_start = time.time()
    _apply_pragmas(tgt, hw_config, is_target=True, journal_mode='WAL')  # mevcut WAL DB'yi koru
    tgt.execute(f"ATTACH DATABASE '{additions_db.replace(chr(92), '/')}' AS add_;")
    tgt.execute(f"PRAGMA add_.cache_size = {hw_config['src_cache_size']};")
    tgt.execute("PRAGMA add_.mmap_size = 0;")

    file_select = build_file_select(hash_types)

    # Yeni OS (filtreye uyan) — once sayalim
    print(f"\n[1/5] Yeni OS satirlari...")
    t = time.time()
    if os_list:
        tgt.execute("CREATE TEMP TABLE os_filter_list (name VARCHAR);")
        tgt.executemany("INSERT INTO temp.os_filter_list VALUES (?)", [(n,) for n in os_list])
        before_os = tgt.execute("SELECT COUNT(*) FROM main.OS").fetchone()[0]
        tgt.execute("""
            INSERT OR IGNORE INTO main.OS
            SELECT o.* FROM add_.OS o
            INNER JOIN temp.os_filter_list t ON o.name = t.name;
        """)
        tgt.execute("DROP TABLE temp.os_filter_list;")
    else:
        before_os = tgt.execute("SELECT COUNT(*) FROM main.OS").fetchone()[0]
        tgt.execute(f"INSERT OR IGNORE INTO main.OS SELECT * FROM add_.OS WHERE name LIKE '{os_filter}';")
    tgt.commit()
    after_os = tgt.execute("SELECT COUNT(*) FROM main.OS").fetchone()[0]
    print(f"  [OK] +{after_os - before_os} yeni OS ({format_time(time.time()-t)})")

    print(f"\n[2/5] Yeni MFG satirlari...")
    t = time.time()
    before_mfg = tgt.execute("SELECT COUNT(*) FROM main.MFG").fetchone()[0]
    tgt.execute("""
        INSERT OR IGNORE INTO main.MFG SELECT DISTINCT m.* FROM add_.MFG m
        WHERE m.manufacturer_id IN (SELECT DISTINCT manufacturer_id FROM main.OS);
    """)
    tgt.commit()
    after_mfg = tgt.execute("SELECT COUNT(*) FROM main.MFG").fetchone()[0]
    print(f"  [OK] +{after_mfg - before_mfg} yeni MFG ({format_time(time.time()-t)})")

    print(f"\n[3/5] Yeni PKG satirlari...")
    t = time.time()
    before_pkg = tgt.execute("SELECT COUNT(*) FROM main.PKG").fetchone()[0]
    if pkg_filter:
        tgt.execute(f"""
            INSERT OR IGNORE INTO main.PKG SELECT p.* FROM add_.PKG p
            INNER JOIN main.OS o
                ON p.operating_system_id = o.operating_system_id
                AND p.manufacturer_id = o.manufacturer_id
            WHERE p.name LIKE '{pkg_filter}';
        """)
    else:
        tgt.execute("""
            INSERT OR IGNORE INTO main.PKG SELECT p.* FROM add_.PKG p
            INNER JOIN main.OS o
                ON p.operating_system_id = o.operating_system_id
                AND p.manufacturer_id = o.manufacturer_id;
        """)
    tgt.execute("""
        INSERT OR IGNORE INTO main.MFG SELECT DISTINCT m.* FROM add_.MFG m
        WHERE m.manufacturer_id IN (SELECT DISTINCT manufacturer_id FROM main.PKG);
    """)
    tgt.commit()
    after_pkg = tgt.execute("SELECT COUNT(*) FROM main.PKG").fetchone()[0]
    print(f"  [OK] +{after_pkg - before_pkg} yeni PKG ({format_time(time.time()-t)})")

    if after_pkg == before_pkg and after_os == before_os:
        print(f"\n[UYARI] Delta'da filtreye uyan yeni OS/PKG yok — muhtemelen hic yeni FILE da olmayacak.")

    print(f"\n[4/5] Yeni FILE satirlari (additions tarama)...")
    t = time.time()
    tgt.execute("CREATE TEMP TABLE win_pkg_ids AS SELECT DISTINCT package_id FROM main.PKG;")
    tgt.execute("CREATE INDEX temp.idx_win_pkg ON win_pkg_ids(package_id);")
    before_file = tgt.execute("SELECT COUNT(*) FROM main.FILE").fetchone()[0]
    tgt.execute(f"""
        INSERT OR IGNORE INTO main.FILE (sha256, sha1, md5, crc32, file_name, file_size, package_id)
        SELECT {file_select} FROM add_.FILE f
        WHERE EXISTS (SELECT 1 FROM temp.win_pkg_ids w WHERE w.package_id = f.package_id);
    """)
    tgt.commit()
    after_file = tgt.execute("SELECT COUNT(*) FROM main.FILE").fetchone()[0]
    tgt.execute("DROP TABLE temp.win_pkg_ids;")
    print(f"  [OK] +{after_file - before_file:,} yeni FILE hash ({format_time(time.time()-t)})")

    print(f"\n[5/5] VERSION guncelle ve finalize...")
    t = time.time()
    tgt.execute("INSERT OR IGNORE INTO main.VERSION SELECT * FROM add_.VERSION;")
    tgt.commit()
    tgt.execute("DETACH DATABASE add_;")
    tgt.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    tgt.execute("PRAGMA optimize;")
    tgt.close()
    print(f"  [OK] ({format_time(time.time()-t)})")

    # Temizlik
    if not keep_additions:
        for suf in ['', '-wal', '-shm']:
            p = additions_db + suf
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass

    total_elapsed = time.time() - total_start
    print(f"\n{'='*70}")
    print(f"  APPEND TAMAMLANDI")
    print(f"{'='*70}")
    print(f"  Eklenen: +{after_os - before_os} OS, +{after_mfg - before_mfg} MFG, "
          f"+{after_pkg - before_pkg} PKG, +{after_file - before_file:,} FILE")
    print(f"  Sure   : {format_time(total_elapsed)}")
    print(f"{'='*70}\n")


# -------------------------- DATASET ROUTING --------------------------

NSRL_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "NSRL")
os.makedirs(NSRL_ROOT, exist_ok=True)


def find_dataset_files(sub):
    """
    NSRL/<sub>/ altini ozyinelemeli tarar; base .db ve delta .sql dosyalarini bulur.
    Ayrica isim eslesmeli kardes klasorler de delta icin taranir (orn: RDS_..._android_minimal_delta/).
    En buyuk .db ve en yeni delta .sql secilir.
    """
    search_dir = os.path.join(NSRL_ROOT, sub)
    if not os.path.isdir(search_dir):
        return None, None

    db_candidates = []
    delta_candidates = []  # (mtime, path)

    # Kendi klasoru
    for root, _, files in os.walk(search_dir):
        for fn in files:
            low = fn.lower()
            if low.endswith(('-wal', '-shm', '-journal')):
                continue
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if low.endswith('.db'):
                db_candidates.append((st.st_size, fp))
            elif low.endswith('.sql') and 'delta' in low and 'schema' not in low:
                delta_candidates.append((st.st_mtime, fp))

    # Kardes klasorler: NSRL/ altinda, sub ismini icerenler (DB olmayan delta klasorleri)
    try:
        for sib in os.listdir(NSRL_ROOT):
            if sib == sub:
                continue
            sib_dir = os.path.join(NSRL_ROOT, sib)
            if not os.path.isdir(sib_dir):
                continue
            if not (sub in sib or sib.startswith(sub)):
                continue
            # Kardes DB iceriyorsa atla (ayri dataset)
            has_db = False
            for root, _, files in os.walk(sib_dir):
                for fn in files:
                    if fn.lower().endswith('.db') and not fn.lower().endswith(('-wal', '-shm', '-journal')):
                        has_db = True
                        break
                if has_db:
                    break
            if has_db:
                continue
            for root, _, files in os.walk(sib_dir):
                for fn in files:
                    low = fn.lower()
                    if low.endswith('.sql') and 'delta' in low and 'schema' not in low:
                        fp = os.path.join(root, fn)
                        try:
                            st = os.stat(fp)
                            delta_candidates.append((st.st_mtime, fp))
                        except OSError:
                            continue
    except OSError:
        pass

    base_db = max(db_candidates)[1] if db_candidates else None
    delta_sql = max(delta_candidates)[1] if delta_candidates else None
    return base_db, delta_sql


def resolve_dataset_paths(dataset):
    base_db, delta_sql = find_dataset_files(dataset)
    return base_db, delta_sql


def main():
    p = argparse.ArgumentParser(
        description="NSRL Generic Extractor (Modern/iOS/Android/Legacy + delta)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ornekler:
  %(prog)s --dataset modern --os-filter "%%Windows 10%%"
  %(prog)s --dataset ios --os-list-file ios_filter.txt -o output/ios_hashes.db
  %(prog)s --dataset android --apply-delta --os-filter "%%Android%%"
  %(prog)s --source some.db --os-filter "%%Windows%%"   (manuel kaynak)
        """
    )
    p.add_argument("--dataset",
                   help="NSRL/ altindaki herhangi bir klasor adi (otomatik path discovery)")
    p.add_argument("--source", help="Manuel kaynak DB yolu (--dataset yerine)")
    p.add_argument("--delta", help="Manuel delta SQL yolu (--apply-delta ile --dataset kullanirsaniz otomatik)")
    p.add_argument("--apply-delta", action="store_true",
                   help="Delta SQL'i uygula. Varsayilan: 'delta-lite' (sadece INSERT'ler, base kopyalanmaz; 5-15 dk hizli)")
    p.add_argument("--full-merge", action="store_true",
                   help="Delta'yi eski/yavas yontemle uygula (base DB'yi kopyalar, DELETE/UPDATE'leri de uygular)")
    p.add_argument("--append-delta", action="store_true",
                   help="Yeni delta'yi MEVCUT bir filtrelenmis DB'ye ekle (--existing ile birlikte kullan). Hizli (~2-5 dk).")
    p.add_argument("--existing", help="--append-delta icin mevcut filtrelenmis DB yolu")
    p.add_argument("--keep-merged", action="store_true",
                   help="Merged/additions DB'yi islem sonunda silme (tekrar kullanilmak icin)")
    p.add_argument("--vacuum", action="store_true",
                   help="Sonda VACUUM calistir (varsayilan kapali, 3-10 dk kazanc; Axiom icin gerekli degil)")
    p.add_argument("-o", "--output", help="Cikti DB yolu")
    p.add_argument("--os-list-file", help="Her satirda bir OS ismi olan metin dosyasi")
    p.add_argument("--os-filter", default="%", help="SQL LIKE pattern (varsayilan: %%)")
    p.add_argument("--pkg-filter", help="Opsiyonel PKG name LIKE pattern")
    p.add_argument("--hash-types", default="sha256,sha1,md5,crc32",
                   help="Virgulle ayrilmis hash listesi (sha256,sha1,md5,crc32). Secilmeyenler '' olur.")
    p.add_argument("--append-to", metavar="DB_PATH",
                   help="Mevcut filtrelenmis DB'ye yeni kaynak ekle (INSERT OR IGNORE). Multi-dataset icin kullanilir.")
    p.add_argument("--job-file", metavar="JSON_PATH",
                   help="Coklu dataset isi: JSON dosyasi ile tum datasetleri sirali isleme al.")
    p.add_argument("--dedup-db", metavar="DB_PATH",
                   help="Mevcut filtrelenmis DB'deki duplicate hash satirlarini sil ve VACUUM uygula.")

    args = p.parse_args()

    # --dedup-db modu: sadece dedup islemini calistir
    if args.dedup_db:
        db_path = os.path.abspath(args.dedup_db)
        if not os.path.exists(db_path):
            print(f"[HATA] DB bulunamadi: {db_path}")
            sys.exit(1)
        dedup_file_table(db_path)
        return

    # --job-file modu: coklu dataset isini sirali calistir
    if args.job_file:
        import json as _json
        with open(args.job_file, 'r', encoding='utf-8') as _f:
            job_spec = _json.load(_f)
        target_db = job_spec['target_db']
        hash_types_job = set(job_spec.get('hash_types', ['md5']))
        pkg_filter_job = job_spec.get('pkg_filter', '') or None
        jobs = job_spec['jobs']
        out_dir = os.path.dirname(target_db)
        os.makedirs(out_dir, exist_ok=True)
        for i, j in enumerate(jobs):
            print(f"\n{'#'*70}")
            print(f"  Dataset {i+1}/{len(jobs)}: {j.get('dataset_label', j.get('dataset', ''))}")
            print(f"{'#'*70}")
            add_db = None
            delta_sql_j = j.get('delta_sql')
            if delta_sql_j and os.path.exists(delta_sql_j):
                tag_j = j.get('tag', f'job{i}')
                add_db = os.path.join(out_dir, f"_additions_{tag_j}.db")
                set_process_priority()
                tmp_hw = analyze_hardware(add_db)
                apply_delta_lite(delta_sql_j, add_db, tmp_hw)
            extract_filtered(
                source_db=j['source_db'],
                target_db=target_db,
                os_list_file=j.get('os_list_file'),
                os_filter=j.get('os_filter', '%'),
                pkg_filter=pkg_filter_job,
                hash_types=hash_types_job,
                additions_db=add_db,
                do_vacuum=False,
                dataset=j.get('dataset', ''),
                append_mode=(i > 0),
            )
            if add_db:
                for suf in ['', '-wal', '-shm']:
                    try:
                        if os.path.exists(add_db + suf):
                            os.remove(add_db + suf)
                    except Exception:
                        pass

        # Opsiyonel: tum joblar tamamlandiktan sonra duplicate hash temizligi
        if job_spec.get('do_dedup') and os.path.exists(target_db):
            print(f"\n{'#'*70}")
            print("  DEDUP: Duplicate hash satirlari temizleniyor...")
            print(f"{'#'*70}")
            dedup_file_table(target_db)

        return

    # --append-delta modu: mevcut DB'ye yeni delta ekle, diger tum akisi atla
    if args.append_delta:
        if not args.existing:
            p.error("--append-delta icin --existing <mevcut_filtered.db> gerekli")
        # Delta path cozumlemesi: --delta verilmisse direkt, yoksa --dataset'ten auto
        delta_sql = None
        if args.delta:
            delta_sql = os.path.abspath(args.delta)
        elif args.dataset:
            _, delta_sql = resolve_dataset_paths(args.dataset)
        if not delta_sql or not os.path.exists(delta_sql):
            p.error("Delta SQL bulunamadi. --delta <path> ya da --dataset <ad> ile belirtin.")
        existing = os.path.abspath(args.existing)
        append_delta_to_existing(existing, delta_sql, keep_additions=args.keep_merged)
        return

    # Kaynak ve delta path cozumlemesi
    if args.dataset:
        base_db, delta_sql = resolve_dataset_paths(args.dataset)
        if args.source:
            base_db = os.path.abspath(args.source)
        if args.delta:
            delta_sql = os.path.abspath(args.delta)
        if not base_db:
            expected = os.path.join(NSRL_ROOT, args.dataset)
            print(f"[HATA] '{args.dataset}' dataseti icin .db dosyasi bulunamadi.")
            print(f"       Aranan klasor: {expected}")
            print(f"       Bu klasorun altinda (ic ice olabilir) bir .db dosyasi olmali.")
            sys.exit(1)
    elif args.source:
        base_db = os.path.abspath(args.source)
        delta_sql = os.path.abspath(args.delta) if args.delta else None
    else:
        p.error("--dataset veya --source gerekli")

    # Cikti path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(base_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    if args.output:
        target_db = os.path.abspath(args.output)
    else:
        tag = args.dataset or "custom"
        suffix = "_with_delta" if args.apply_delta else ""
        target_db = unique_output_path(os.path.join(out_dir, f"{tag}_filtered{suffix}.db"))

    # Delta uygula (opsiyonel) — iki mod: lite (varsayilan, hizli) veya full-merge (yavas)
    source_for_extract = base_db
    additions_db = None
    merged_db = None
    cleanup_paths = []

    if args.apply_delta:
        if not delta_sql or not os.path.exists(delta_sql):
            print(f"[HATA] Delta SQL bulunamadi: {delta_sql}")
            sys.exit(1)
        set_process_priority()
        tag = args.dataset or 'custom'
        if args.full_merge:
            # Eski mod: 181 GB kopyala + tum ifadeleri uygula
            merged_db = os.path.join(out_dir, f"_merged_{tag}.db")
            hw_config = analyze_hardware(merged_db)
            apply_delta(base_db, delta_sql, merged_db, hw_config)
            source_for_extract = merged_db
            cleanup_paths.append(merged_db)
        else:
            # Yeni mod: sadece INSERT'leri kucuk additions.db'ye uygula
            additions_db = os.path.join(out_dir, f"_additions_{tag}.db")
            hw_config = analyze_hardware(additions_db)
            apply_delta_lite(delta_sql, additions_db, hw_config)
            cleanup_paths.append(additions_db)

    # Hash turu secenegi
    requested = {h.strip().lower() for h in (args.hash_types or "").split(",") if h.strip()}

    # --append-to: mevcut DB'ye ekle
    if args.append_to:
        target_db = os.path.abspath(args.append_to)
        if not os.path.exists(target_db):
            print(f"[HATA] --append-to: dosya bulunamadi: {target_db}")
            sys.exit(1)

    # Filtrele
    extract_filtered(
        source_db=source_for_extract,
        target_db=target_db,
        os_filter=args.os_filter,
        os_list_file=args.os_list_file,
        pkg_filter=args.pkg_filter,
        hash_types=requested,
        additions_db=additions_db,
        do_vacuum=args.vacuum,
        dataset=args.dataset,
        append_mode=bool(args.append_to),
    )

    # Ara dosyalari temizle (--keep-merged yoksa)
    if not args.keep_merged:
        for path in cleanup_paths:
            try:
                for suf in ['', '-wal', '-shm']:
                    p = path + suf
                    if os.path.exists(p):
                        os.remove(p)
                print(f"  [Temizlik] Silindi: {os.path.basename(path)}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
