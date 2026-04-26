# NSRL Hash Extractor — MANIFEST

> **Purpose:** Generate Magnet Axiom-compatible, filtered hash `.db` files from NIST NSRL RDSv3 Minimal databases. Creates dataset-specific, compact hash sets to quickly filter "known-good" files during forensic analysis.

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [Requirements and Setup](#2-requirements-and-setup)
3. [NSRL Database Schema](#3-nsrl-database-schema)
4. [Usage — GUI](#4-usage--gui)
5. [Usage — CLI](#5-usage--cli)
6. [Extraction Modes](#6-extraction-modes)
7. [Technical Architecture](#7-technical-architecture)
8. [Performance and Hardware Scaling](#8-performance-and-hardware-scaling)
9. [Output Format and Axiom Integration](#9-output-format-and-axiom-integration)
10. [Dataset Reference](#10-dataset-reference)
11. [Known Limitations](#11-known-limitations)

---

## 1. Project Structure

```
NSRLExtractor\
│
├── CLAUDE.md                      ← this file
├── nsrl_extractor.py              ← main extraction engine (CLI)
├── gui_app.py                     ← web server (port 8080, SSE progress)
├── index.html                     ← single-page dark theme UI
│
├── run_gui.bat                    ← launches GUI (auto-opens browser)
├── setup.bat                      ← installs portable Python 3.12
│
├── python_embed\                  ← portable Python 3.12 (no internet required)
│   └── python.exe
│
├── NSRL\                          ← databases downloaded from NIST go here
│   ├── RDS_2026.03.1_modern_minimal\
│   │   └── RDS_2026.03.1_modern_minimal.db        (181 GB)
│   ├── RDS_2026.03.1_modern_minimal_delta\
│   │   └── RDS_2026.03.1_modern_minimal_delta.sql (~1 GB)
│   ├── RDS_2026.03.1_ios_minimal\          (38 GB)
│   ├── RDS_2026.03.1_ios_minimal_delta\
│   ├── RDS_2026.03.1_android_minimal\      (40 GB)
│   ├── RDS_2026.03.1_android_minimal_delta\
│   ├── RDS_2026.03.1_legacy_minimal\       (67 GB)
│   └── RDS_2026.03.1_legacy_minimal_delta\
│
├── output\                        ← generated .db files land here
│   ├── win10-win11-delta.db       ← example output
│   └── _additions_*.db            ← delta-lite temp file (can be deleted)
│
├── axiom_nsrl_windows10_imaj.md   ← Win10 imajı için önerilen OS seçim kılavuzu
├── os_list_modern.md              ← modern minimal OS listesi (bölümlere ayrılmış)
├── os_list_android.md             ← android minimal OS listesi
├── os_list_ios.md                 ← ios minimal OS listesi (1 kayıt, not içerir)
└── os_list_legacy.md              ← legacy minimal OS listesi
```

**No folder name restrictions:** Any subfolder under `NSRL/` is auto-discovered. Folders containing a `.db` are treated as datasets; sibling folders with a matching name containing `.sql` are treated as deltas.

---

## 2. Requirements and Setup

### First-time setup (once)

```bat
setup.bat
```

- Downloads Python 3.12 embeddable zip (into `python_embed\`)
- Installs pip
- Requires internet connection (this step only)

### Subsequent runs

```bat
run_gui.bat
```

- Launches `gui_app.py` with portable Python
- Browser auto-opens `http://localhost:8080`
- **No internet required**

### Required disk space

| Dataset | Base DB | Delta SQL | Temp (_additions) | Example output |
|---|---|---|---|---|
| Modern | 181 GB | ~1 GB | ~1.5 GB | 1–10 GB (varies by selection) |
| iOS | 38 GB | ~0.5 GB | ~1.1 GB | 1–5 GB |
| Android | 40 GB | ~0.3 GB | ~0.6 GB | 0.5–3 GB |
| Legacy | 67 GB | ~0.2 GB | ~0.4 GB | 0.5–3 GB |

---

## 3. NSRL Database Schema

NIST distributes each dataset as a single SQLite file. The schema is identical across all datasets:

```
FILE      sha256, sha1, md5, crc32, file_name, file_size, package_id
PKG       package_id, name, version, operating_system_id, manufacturer_id, language, application_type
OS        operating_system_id, name, version, manufacturer_id
MFG       manufacturer_id, name
VERSION   version, build_set, build_date, release_date, description

VIEW DISTINCT_HASH  →  SELECT DISTINCT sha256, sha1, md5, crc32 FROM FILE
```

**Relationship chain:** `MFG → OS → PKG → FILE`

Filtering is always based on `OS.name`. The matching PKGs are identified, then all FILE rows belonging to those PKGs are extracted.

---

## 4. Usage — GUI

### Launch

```bat
run_gui.bat
```

Browser opens at `http://localhost:8080`.

### Step-by-step extraction

```
1. Select dataset(s)   →  Modern / iOS / Android / Legacy checkboxes (multi-select supported)
                          Each dataset has its own Δ toggle button next to it
2. Enable delta        →  Click the Δ button next to a dataset to activate its delta
3. Select OS(es)       →  Left sidebar: base OS entries grouped by dataset
                          Right sidebar: delta OS entries (visible only when Δ is active)
                          Modern dataset: Windows 7/8/10/11/Server entries pre-selected
                          (based on axiom_nsrl_windows10_imaj.md guide — ~132 entries)
4. Hash types          →  sha256 / sha1 / md5 / crc32 (multiple allowed)
5. Dedup uygula        →  Optional checkbox — removes duplicate hash rows after all jobs finish
6. Start               →  live progress log streams in real time
7. Output ready        →  file appears under output\ with auto-generated name
```

**OS sayisi display:** The terminal header shows the total checkbox count (e.g. 196) plus the unique OS name count if they differ (e.g. `196 (191 unique OS adı)`). The difference comes from OS entries that exist in both the base and delta panels with the same name — both are passed to the extractor and both source DBs are filtered; the displayed difference is purely informational.

### Multi-dataset mode

Multiple datasets can be selected simultaneously (e.g. Legacy + Modern):
- Each dataset runs as a separate job sequentially, all writing to the same output DB
- Second+ jobs use `append_mode=True` (INSERT OR IGNORE into the existing target)
- OS selections are split by dataset group prefix in the UI ("Legacy — Windows 7", "Modern — Windows 10")
- The Δ button is per-dataset; activating it shows that dataset's delta OS entries in the right sidebar

### Auto-generated filename

A meaningful slug is constructed from selected OS names:
- Windows 10 + Windows 11 + delta selected → `win10-win11-delta.db`
- Legacy + Modern combined → `win7-win10-win11-delta.db`
- Android 12 + Android 13 selected → `android12-android13.db`

### Delta panel (separate sidebar)

When a dataset's Δ button is active, delta OS entries appear in the right sidebar:
- **Left sidebar (base):** OS entries from the base DB, grouped by dataset label
- **Right sidebar (delta):** OS entries extracted from the delta SQL, grouped by dataset label
- Both sidebars are independently selectable; all selections are sent to the extractor
- Delta sidebar is hidden when no dataset has its Δ active
- Delta panel has no pre-selection (user manually selects delta OS entries)

### Hash Dedup (post-extraction)

"Hash Dedup" button opens a modal to run deduplication on an existing output DB:
- Removes FILE rows where `(sha256, sha1, md5, crc32)` combination is duplicated
- Keeps one row per unique hash combination (MIN file_name, MIN file_size, MIN package_id)
- Recreates DISTINCT_HASH view, runs VACUUM
- Duration: 5–20 min depending on DB size

"Dedup uygula" checkbox on the toolbar triggers the same operation automatically after all extraction jobs complete on the merged output DB.

### Apply New Delta (to existing DB)

A new NSRL delta can be appended to an already-generated `.db` file (~2–5 min):

```
"Apply New Delta" button
  → Select existing DB (files under output/ are listed)
  → Select delta SQL
  → Start
```

This mode adds only new records rather than re-running the full extraction.

### Shutting Down

"Shut Down Server" button → the entire process tree is safely terminated.

---

## 5. Usage — CLI

Can also be used from the command line instead of the GUI:

```bat
python_embed\python.exe nsrl_extractor.py [OPTIONS]
```

### Basic examples

```bat
:: Windows 10 and 11 — with delta, MD5 only
python_embed\python.exe nsrl_extractor.py ^
  --dataset RDS_2026.03.1_modern_minimal ^
  --os-list-file win_filter.txt ^
  --apply-delta ^
  --hash-types md5 ^
  -o output\win10-win11.db

:: Android all — with delta
python_embed\python.exe nsrl_extractor.py ^
  --dataset RDS_2026.03.1_android_minimal ^
  --os-filter "%Android%" ^
  --apply-delta ^
  -o output\android_all.db

:: iOS — OS filter doesn't work, take all
python_embed\python.exe nsrl_extractor.py ^
  --dataset RDS_2026.03.1_ios_minimal ^
  --os-filter "%" ^
  -o output\ios_all.db

:: Append new delta to existing DB
python_embed\python.exe nsrl_extractor.py ^
  --append-delta ^
  --existing output\win10-win11.db ^
  --delta "NSRL\RDS_2026.03.1_modern_minimal_delta\RDS_..._delta.sql"
```

### All parameters

| Parameter | Description |
|---|---|
| `--dataset <folder>` | Any folder name under NSRL/ (auto path discovery) |
| `--source <path>` | Manual source DB path (alternative to `--dataset`) |
| `--os-list-file <txt>` | Text file with one OS.name per line (exact match) |
| `--os-filter "pattern"` | SQL LIKE pattern (e.g. `%Windows 10%`) |
| `--pkg-filter "pattern"` | Optional package name filter |
| `--apply-delta` | Enable delta-lite mode (default fast mode) |
| `--full-merge` | Old/slow mode — base DB is copied, DELETE+UPDATE included |
| `--append-delta` | Append delta to existing DB (use with `--existing`) |
| `--existing <path>` | Target DB for `--append-delta` |
| `--delta <path>` | Manual delta SQL path |
| `--hash-types a,b,c` | Combination of sha256,sha1,md5,crc32 |
| `--vacuum` | Run VACUUM at end (off by default, not needed for Axiom) |
| `--keep-merged` | Do not delete `_additions.db` temp file |
| `--job-file <json>` | Multi-dataset mode: JSON file describing all jobs (GUI uses this internally) |
| `--dedup-db <path>` | Remove duplicate hash rows from an existing filtered DB and VACUUM |
| `-o <path>` | Output DB path |

---

## 6. Extraction Modes

### MODE 1 — Delta-Lite (default, fast)

Runs with `--apply-delta` flag. Base DB is **not copied**.

```
[1] Delta SQL → INSERT statements parsed (multi-line supported) → _additions.db (~1.5 GB)
[2] New target DB created, schema + _filter_meta saved
[3] Base DB ATTACH'd (read-only, not copied)
[4] VERSION → OS (filter) → MFG → PKG copied
[5] FILE: 4 parallel reader threads + 1 writer thread
    - From base DB → filtered FILE rows
    - From _additions.db → new delta FILE rows
[6] DISTINCT_HASH view created
[7] (optional) dedup_file_table() — if do_dedup=True
```

**Why fast:** Reads directly from 181 GB base without copying it. DELETE/UPDATE delta entries are skipped (harmless for hash sets).

**Multi-dataset:** Each dataset runs as a separate job writing to the same target DB (`append_mode=True` for second+ jobs). Dedup (step 7) runs once after all jobs on the fully merged DB.

**Delta SQL parsing:** Supports both single-line and multi-line INSERT statements. Each statement is executed individually with per-statement error recovery (bad rows are skipped with a warning, not a crash).

**Expected duration (SSD):** 25–60 minutes per dataset (depends on selection)

### MODE 2 — Full-Merge (slow, complete)

Runs with `--full-merge` flag.

```
[1] Base DB fully copied (181 GB → merged.db)   ← takes a long time
[2] Delta SQL applied line by line (DELETE + UPDATE + INSERT)
[3] Filtering and extraction
```

**When to use:** Only if DELETE/UPDATE entries in the delta are critical (rarely needed).

### MODE 3 — Append-Delta (~2–5 minutes)

Runs with `--append-delta --existing <db> --delta <sql>`.

Filter parameters are read from the existing filtered DB's `_filter_meta` table. The new delta is applied. Only records matching the filter are added.

```
[1] _filter_meta read (dataset, os_list, hash_types)
[2] Delta SQL → _new_additions.db
[3] New OS, PKG, FILE rows added to existing DB (OR IGNORE)
```

---

## 7. Technical Architecture

### Data flow — delta-lite

```
NSRL/dataset/base.db  (181 GB, read-only)
        │
        ├─ [2/8] ATTACH'd as read source
        ├─ [3/8] VERSION copied
        ├─ [4/8] OS copied (filter applied)
        ├─ [5/8] MFG copied
        ├─ [6/8] PKG copied (with OS join)
        │         → pkg_ids_set (Python frozenset)
        │
        ├─ [7/8] DETACH ← prevents lock contention with reader threads
        │
        └─ [7/8] _parallel_file_copy()
                  ├─ Thread-1: ROWID 1..108M → read FILE → filter → result_q
                  ├─ Thread-2: ROWID 108M..216M → read FILE → filter → result_q
                  ├─ Thread-3: ROWID 216M..324M → read FILE → filter → result_q
                  └─ Thread-4: ROWID 324M..432M → read FILE → filter → result_q
                              ↓
                  Writer (main thread): result_q → executemany → commit → throttle

_additions.db  (1–2 GB, delta INSERTs)
        └─ [7/8] add_ ATTACH → single SQL query → FILE INSERT OR IGNORE

output/win10-delta.db  ← grows (DELETE journal: each commit writes to .db)
```

### GUI — server architecture

```
run_gui.bat
    └─ python_embed\python.exe gui_app.py
            │
            ├─ ThreadingHTTPServer (port 8080)
            │       ├─ GET  /                    → index.html
            │       ├─ GET  /api/datasets         → scan NSRL/
            │       ├─ GET  /api/os_list          → OS list from source DB (multi-dataset)
            │       │       params: ?dataset=A&dataset=B  (multi-param)
            │       │       returns: {group: [{os_id, name, version, label, value, is_delta}]}
            │       │       group names prefixed with dataset label: "Modern — Windows 10"
            │       │       os_id: operating_system_id (None if schema lacks column)
            │       ├─ GET  /api/existing_dbs     → filtered DBs in output/
            │       ├─ GET  /api/delta_files      → delta SQL files
            │       ├─ GET  /api/progress         → SSE (Server-Sent Events) log stream
            │       ├─ POST /api/start            → writes _current_job.json, launches --job-file
            │       │       payload: {jobs:[{dataset,selected_os,apply_delta}], hash_types,
            │       │                 pkg_filter, do_dedup}
            │       ├─ POST /api/append_delta     → launches --append-delta subprocess
            │       ├─ POST /api/dedup_db         → launches --dedup-db subprocess
            │       ├─ POST /api/stop             → taskkill /F /T
            │       └─ POST /api/shutdown         → kills server + entire process tree
            │
            └─ subprocess.Popen(nsrl_extractor.py,
                                env=PYTHONUNBUFFERED=1,   ← progress lines arrive immediately
                                stdout=PIPE, bufsize=0)
```

### Multi-dataset job flow

```
/api/start  →  _current_job.json  →  nsrl_extractor.py --job-file _current_job.json
                  {
                    "target_db": "output/win7-win10-delta.db",
                    "hash_types": ["md5"],
                    "do_dedup": true,
                    "jobs": [
                      { "source_db": "NSRL/legacy/legacy.db",
                        "delta_sql": "NSRL/legacy_delta/delta.sql",
                        "os_list_file": "temp_os_filter_legacy_0.txt" },
                      { "source_db": "NSRL/modern/modern.db",
                        "delta_sql": "NSRL/modern_delta/delta.sql",
                        "os_list_file": "temp_os_filter_modern_1.txt" }
                    ]
                  }

Job loop (nsrl_extractor.py):
  Job 0 (Legacy):  apply_delta_lite → _additions_legacy_0.db → extract_filtered(append_mode=False)
  Job 1 (Modern):  apply_delta_lite → _additions_modern_1.db → extract_filtered(append_mode=True)
  [optional] do_dedup → dedup_file_table(target_db)   ← on merged DB, after all jobs
```

### Hash dedup — dedup_file_table()

```python
# Removes FILE rows with duplicate (sha256, sha1, md5, crc32) combinations.
# Called after all extraction jobs complete (do_dedup=True) OR via --dedup-db CLI.
_FILE_DEDUP = GROUP BY sha256, sha1, md5, crc32 → MIN(file_name, file_size, package_id)
DROP TABLE FILE → ALTER TABLE _FILE_DEDUP RENAME TO FILE → recreate DISTINCT_HASH view → VACUUM
```

Use case: Legacy's "Windows Generic" and Modern's "Windows Generic" may share some identical hashes. After both jobs write to the same DB, dedup removes cross-dataset duplicate hash rows while preserving hashes unique to each dataset.

### OS list API — pre-selection

`build_os_groups()` queries `SELECT DISTINCT operating_system_id, name, version FROM OS`. If this column doesn't exist (schema change in a future NSRL release), it silently falls back to `SELECT DISTINCT name, version` with `os_id=None`. In that case the GUI still works; pre-selection is simply skipped.

`MODERN_PRESELECT_IDS` in `index.html` holds ~132 OS IDs from `axiom_nsrl_windows10_imaj.md`. When modern dataset is loaded, checkboxes whose `os_id` is in this set are rendered with `checked`. Other datasets: no pre-selection. Delta panel: no pre-selection.

### Progress streaming

```
nsrl_extractor.py  →  stdout (PYTHONUNBUFFERED=1)
        ↓
gui_app.py  →  char-by-char read (\r / \n distinguished)
        ↓
SSE  →  JSON to browser: {"log": "\r  42.3% [366/866] Hash: 3,241,892 | ..."}
        ↓
index.html  →  \r lines update same line (terminal emulation)
```

### SQLite locking strategy

| Connection | Journal | Purpose |
|---|---|---|
| `tgt` (target DB) | DELETE | Each commit writes to `.db` → file grows, throttle works correctly |
| Reader thread connections | — (read-only) | `query_only=1`, `mmap_size` active |
| `_additions.db` | DELETE | Delta inserts |
| `append_delta` / `full-merge` | WAL | Preserves existing DB |

---

## 8. Performance and Hardware Scaling

### Automatic hardware analysis

At startup, disk type (SSD/HDD/USB) and available RAM are measured:

| Disk | Reader threads | Batch size | mmap | min_sleep |
|---|---|---|---|---|
| SSD / NVMe | 2 on OS disk, up to 3 off OS disk | 100,000 rows | RAM×25%, max 8 GB | 20-30ms |
| HDD | 1 | 30,000 rows | — | 0ms |
| USB | 1 | 20,000 rows | — | 0ms |
| Other/VM | cpu÷3 (max 4) | 100,000 rows | — | 10ms |

**GIL note:** Due to Python GIL, true parallel SQLite reads are capped at ~3–4×. 8 threads saturates an SSD without additional gain → maximum 4 threads.

### SQLite cache distribution (16 GB RAM example)

```
Budget   = 12 GB (free RAM) × 55%  = 6.6 GB
Target   = 6.6 × 35%               = 2.3 GB  (target DB cache)
Src/thr  = (6.6 − 2.3) / 4 thread = 1.1 GB  (per reader thread)
mmap     = 12 × 25%                = 3.0 GB  (virtual memory mapping)
```

### Throttle mechanism

After each commit, to prevent disk saturation:

```python
sleep = commit_duration × (1 / target_utilization − 1)
# Example: commit took 80ms, target 72% → sleep = 80ms × 0.39 = 31ms
time.sleep(max(min_sleep, min(sleep, 2.0)))
```

With DELETE journal mode, commit duration accurately reflects real disk write time → throttle formula works correctly.

**Target disk utilization:** 70–75%

### Expected durations (SSD, modern minimal, Win10+Win11)

| Phase | Duration |
|---|---|
| Delta-lite apply (_additions.db generation) | 15–30 min |
| VERSION + OS + MFG + PKG copy | 2–5 min |
| FILE parallel copy (432M rows, ~4% hit rate) | 20–45 min |
| **Total** | **~45–90 min** |
| Append-delta (add to existing DB) | ~2–5 min |

**First batch warning:** After [7/8] appears, the first progress line may be delayed 30–90 seconds. Cold read from a 181 GB DB is slow until the memory cache warms up. The system is working even if no progress appears — do not click Stop.

---

## 9. Output Format and Axiom Integration

### Output .db schema

The generated file is compatible with the original NSRL schema:

```sql
FILE      (sha256, sha1, md5, crc32, file_name, file_size, package_id)
PKG       (package_id, name, version, operating_system_id, ...)
OS        (operating_system_id, name, version, manufacturer_id)
MFG       (manufacturer_id, name)
VERSION   (version, build_set, build_date, ...)
DISTINCT_HASH VIEW  →  SELECT DISTINCT sha256, sha1, md5, crc32 FROM FILE
_filter_meta        →  filter parameters (dataset, os_list, hash_types...)
```

The `_filter_meta` table stores filter information for subsequent `--append-delta` runs. DBs without this table will not appear in the append menu.

### Importing into Magnet Axiom

1. Extraction completes → `output\win10-delta.db` is produced
2. Open Axiom → **Tools → Manage Custom Hash Sets**
3. **Add** → select the generated `.db` file
4. **Hash Set Type:** select Known Good
5. Axiom processes the hash set → `C:\ProgramData\Axiom\Hash Sets\Database\HashList.db` grows
6. In the next case analysis, known-good files are automatically filtered

**If Axiom's HashList.db is empty:** The hash set was removed from the Axiom profile. Repeat steps 2–5.

### Hash type selection

Generally **MD5 only** is sufficient for Axiom (speed/size balance). Selecting multiple types increases output size:

| Selection | Output size (Win10+Win11, with delta) |
|---|---|
| MD5 only | ~2–4 GB |
| SHA1+MD5 | ~4–7 GB |
| All | ~8–12 GB |

---

## 10. Dataset Reference

| Dataset | Folder | Base DB | File count | OS filter |
|---|---|---|---|---|
| **Modern** | `RDS_*_modern_minimal` | 181 GB | 432,866,777 | Works (~352 OS entries) |
| **iOS** | `RDS_*_ios_minimal` | 38 GB | ~200M | **Doesn't work** — use `--os-filter "%"` |
| **Android** | `RDS_*_android_minimal` | 40 GB | ~180M | Works (42 OS: Android 2.2–13) |
| **Legacy** | `RDS_*_legacy_minimal` | 67 GB | ~250M | Works (XP, Vista, Win7, Server 2003/2008) |

**Why iOS filter doesn't work:** In the iOS minimal DB, bulk content is under `operating_system_id=646` (unnamed), with only 1 named OS entry. Use `--os-filter "%"` to get all files.

**Full OS lists:** See `os_list_modern.md`, `os_list_android.md`, `os_list_ios.md`, `os_list_legacy.md`.

### Delta volumes (2026.03.1 release)

| Dataset | Delta SQL size | _additions.db (temp) | New FILE rows |
|---|---|---|---|
| Modern | ~1 GB | ~1.5 GB | ~3,600,000 |
| iOS | ~0.5 GB | ~1.1 GB | ~5,300,000 |
| Android | ~0.3 GB | ~0.6 GB | ~1,200,000 |
| Legacy | ~0.2 GB | ~0.4 GB | ~1,800,000 |

### Modern minimal — delta OS entries (2026.03.1)

The delta SQL for modern minimal contains 5 OS INSERT statements, all of which already exist in the base DB:
- Windows (Generic)
- Oracle Linux 10
- Windows 11 Version 25H2
- Windows Server Version 23H2
- Windows Server Version 23H2 64 bit

These appear in the delta OS panel (right panel) when the dataset's Δ button is active. Since these 5 names already exist in the base DB, the UI displays a note like `196 (191 unique OS adı)` if both base and delta versions are selected — both are passed to the extractor and both source DBs are filtered (no data is lost).

---

## 11. Known Limitations

**Delta DELETE/UPDATE skipped (delta-lite mode)**
`DELETE` and `UPDATE` statements in the delta SQL are not processed in delta-lite mode. For NSRL hash sets this is harmless: deleted records remaining as "known-good" causes no issues; updated records typically change metadata (file name) not the hash. Use `--full-merge` for full consistency (much slower).

**No package_id index on FILE table**
The source NSRL DB has no index on `FILE.package_id`. The entire table must be scanned via ROWID range scan and package_id filtering is applied via Python frozenset. A full scan of 432M rows is unavoidable.

**First progress delay**
After [7/8] starts, the first real progress line appears after the first read chunk is read and filtered. The extractor now prints periodic "[bekleniyor]" heartbeat lines while the first chunk is being read. On the OS disk, source reads are serialized and throttled toward 70-75% disk use to keep the machine responsive.

**No concurrent runs**
Multiple simultaneous extractions are not supported. The GUI shows "Process already running" warning.

**Portable Python limitation**
`python_embed\` is a Python 3.12 embeddable package. Additional library installation is possible via pip in `setup.bat`. No dependencies beyond the standard library are required.

**Pre-selection compatibility**
`MODERN_PRESELECT_IDS` in `index.html` is based on RDS_2026.03.1 OS IDs. If NIST changes OS IDs in a future release, pre-selection will silently skip non-matching entries — the GUI continues to work normally, just without pre-checks on new/renumbered entries.

**Dedup duration**
`dedup_file_table()` creates a new table via `GROUP BY`, drops the original, renames, and runs VACUUM. On a large merged DB (e.g. Legacy+Modern, ~10 GB) this can take 20–40 min. Progress is printed to the console. The operation is safe to interrupt (the original DB remains intact until the final RENAME step).
