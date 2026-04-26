import http.server
import socketserver
import json
import sqlite3
import os
import re
import subprocess
import threading
import sys
import difflib
from urllib.parse import urlparse, parse_qs

PORT = 8080
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NSRL_ROOT = os.path.join(BASE_DIR, "NSRL")
os.makedirs(NSRL_ROOT, exist_ok=True)


def _make_label(folder_name):
    n = folder_name.lower()
    if 'modern' in n:
        type_tag = "Modern"
    elif 'ios' in n or 'ipad' in n or 'iphone' in n:
        type_tag = "iOS"
    elif 'android' in n:
        type_tag = "Android"
    elif 'legacy' in n:
        type_tag = "Legacy"
    else:
        return folder_name
    # Klasor adi tip tagiyla ayni ise tekrar etme
    if folder_name.lower() == type_tag.lower():
        return type_tag
    return f"{type_tag} — {folder_name}"


def _infer_type(folder_name):
    """Dataset tipini klasör adından çıkar (OS gruplama için)."""
    n = folder_name.lower()
    if 'ios' in n or 'ipad' in n or 'iphone' in n:
        return 'ios'
    if 'android' in n:
        return 'android'
    if 'legacy' in n:
        return 'legacy'
    return 'modern'


def _scan_nsrl():
    """
    NSRL/ altındaki tüm first-level klasörleri tarar.
    Returns: { folder_name: {"dbs": [(size,path)], "deltas": [(mtime,path)]} }
    """
    os.makedirs(NSRL_ROOT, exist_ok=True)
    result = {}
    try:
        entries = sorted(os.listdir(NSRL_ROOT))
    except OSError:
        return result
    for entry in entries:
        folder = os.path.join(NSRL_ROOT, entry)
        if not os.path.isdir(folder):
            continue
        dbs, deltas = [], []
        for root, _, files in os.walk(folder):
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
                    dbs.append((st.st_size, fp))
                elif low.endswith('.sql') and 'delta' in low and 'schema' not in low:
                    deltas.append((st.st_mtime, fp))
        result[entry] = {"dbs": dbs, "deltas": deltas}
    return result


def discover_datasets():
    """
    NSRL/ altından .db içeren klasörleri dataset olarak döndürür.
    Delta: önce kendi klasöründe, yoksa isim eşleşen kardeş klasörlerde aranır.
    Returns: [{"id", "label", "base_db", "delta_sql"}]
    """
    tree = _scan_nsrl()
    has_db = {k: v for k, v in tree.items() if v["dbs"]}
    no_db  = {k: v["deltas"] for k, v in tree.items() if not v["dbs"]}

    datasets = []
    for fname, info in has_db.items():
        base_db = max(info["dbs"])[1]
        all_deltas = list(info["deltas"])
        for sib_name, sib_deltas in no_db.items():
            if fname in sib_name or sib_name.startswith(fname):
                all_deltas.extend(sib_deltas)
        delta_sql = max(all_deltas)[1] if all_deltas else None
        datasets.append({
            "id": fname,
            "label": _make_label(fname),
            "base_db": base_db,
            "delta_sql": delta_sql,
        })
    return sorted(datasets, key=lambda d: d["id"])


def _get_dataset_info(dataset_id):
    """Belirli bir dataset için (base_db, delta_sql) döndürür."""
    for d in discover_datasets():
        if d["id"] == dataset_id:
            return d["base_db"], d["delta_sql"]
    # Fallback: doğrudan klasörü tara
    folder = os.path.join(NSRL_ROOT, dataset_id)
    if not os.path.isdir(folder):
        return None, None
    dbs, deltas = [], []
    for root, _, files in os.walk(folder):
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
                dbs.append((st.st_size, fp))
            elif low.endswith('.sql') and 'delta' in low and 'schema' not in low:
                deltas.append((st.st_mtime, fp))
    return (max(dbs)[1] if dbs else None), (max(deltas)[1] if deltas else None)


def make_output_slug(dataset, selected_os_names, apply_delta):
    """
    Seçilen OS listesinden anlaşılır kısa dosya adı üretir.
    Örn: [Windows 10, Windows 11] + delta → "win10-win11-delta"
    """
    parts = set()
    for name in selected_os_names:
        u = name.upper()
        if 'WINDOWS 11' in u:
            parts.add('win11')
        elif 'WINDOWS 10' in u:
            parts.add('win10')
        elif 'WINDOWS 8' in u:
            parts.add('win8')
        elif 'WINDOWS 7' in u:
            parts.add('win7')
        elif 'VISTA' in u:
            parts.add('vista')
        elif 'XP' in u:
            parts.add('xp')
        elif '2025' in u and 'SERVER' in u:
            parts.add('srv2025')
        elif '2022' in u and 'SERVER' in u:
            parts.add('srv2022')
        elif '2019' in u and 'SERVER' in u:
            parts.add('srv2019')
        elif '2016' in u and 'SERVER' in u:
            parts.add('srv2016')
        elif '2012' in u and 'SERVER' in u:
            parts.add('srv2012')
        elif 'SERVER' in u:
            parts.add('server')
        elif 'MACOS' in u or 'MAC OS' in u or 'OS X' in u:
            parts.add('macos')
        elif 'WINDOWS' in u:
            parts.add('windows')
        elif 'ANDROID' in u:
            m = re.search(r'(\d+)', name)
            parts.add('android' + m.group(1) if m else 'android')
        elif 'IOS' in u or 'IPAD' in u or 'IPHONE' in u:
            parts.add('ios')
        elif 'LINUX' in u:
            parts.add('linux')
        else:
            parts.add(_infer_type(dataset))

    if not parts:
        parts.add(_infer_type(dataset))

    order = ['xp', 'vista', 'win7', 'win8', 'win10', 'win11',
             'srv2012', 'srv2016', 'srv2019', 'srv2022', 'srv2025', 'server',
             'macos', 'windows', 'linux', 'ios']

    def _sort_key(p):
        if p.startswith('android') and p[7:].isdigit():
            return (len(order) + int(p[7:]), 0)
        try:
            return (order.index(p), 0)
        except ValueError:
            return (len(order) + 999, 0)

    sorted_parts = sorted(parts, key=_sort_key)
    if apply_delta:
        sorted_parts.append('delta')

    slug = re.sub(r'[^\w\-]', '', '-'.join(sorted_parts))
    return slug or _infer_type(dataset)


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


def list_delta_files(dataset_id):
    """
    Verilen dataset'e ait tüm delta .sql dosyalarını listeler (en yeniden eskiye).
    dataset_id klasörü içinde ve isim eşleşen kardeş klasörlerde arar.
    """
    tree = _scan_nsrl()
    out_paths = set()

    # Kendi klasörü
    if dataset_id in tree:
        for _, fp in tree[dataset_id]["deltas"]:
            out_paths.add(fp)

    # Kardeş klasörler (dataset_id ismini içeren, ama DB'si olmayan)
    for sib_name, info in tree.items():
        if sib_name == dataset_id:
            continue
        if not info["dbs"] and (dataset_id in sib_name or sib_name.startswith(dataset_id)):
            for _, fp in info["deltas"]:
                out_paths.add(fp)

    out = []
    for fp in out_paths:
        try:
            st = os.stat(fp)
            out.append({
                "path": fp,
                "name": os.path.basename(fp),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
        except OSError:
            continue
    out.sort(key=lambda x: x['mtime'], reverse=True)
    return out


def list_existing_filtered_dbs():
    """output/ klasorundeki _filter_meta'li DB'leri listele.
    Her biri icin: path, size, dataset, os count, pkg count, file count."""
    out_dir = os.path.join(BASE_DIR, "output")
    if not os.path.isdir(out_dir):
        return []
    result = []
    for fn in sorted(os.listdir(out_dir)):
        if not fn.lower().endswith(".db") or fn.startswith("_"):
            continue
        fp = os.path.join(out_dir, fn)
        try:
            size = os.path.getsize(fp)
            conn = sqlite3.connect(f"file:{fp}?mode=ro", uri=True, timeout=1.0)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='_filter_meta'")
            if not cur.fetchone():
                conn.close()
                continue
            meta = dict(cur.execute("SELECT key, value FROM _filter_meta").fetchall())
            os_cnt = cur.execute("SELECT COUNT(*) FROM OS").fetchone()[0]
            pkg_cnt = cur.execute("SELECT COUNT(*) FROM PKG").fetchone()[0]
            file_cnt = cur.execute("SELECT COUNT(*) FROM FILE").fetchone()[0]
            conn.close()
            status = meta.get("extraction_status", "")
            if file_cnt <= 0:
                continue
            if status and status != "completed":
                continue
            result.append({
                "path": fp,
                "name": fn,
                "size": size,
                "dataset": meta.get("dataset", ""),
                "hash_types": meta.get("hash_types", ""),
                "pkg_filter": meta.get("pkg_filter", ""),
                "status": status or "legacy",
                "os_count": os_cnt,
                "pkg_count": pkg_cnt,
                "file_count": file_cnt,
            })
        except Exception:
            continue
    return result


def dataset_status():
    """NSRL/ altını otomatik tarayıp hazır datasetleri döndürür."""
    return [
        {
            "id": d["id"],
            "label": d["label"],
            "db_exists": bool(d["base_db"]) and os.path.exists(d["base_db"]),
            "db_size": os.path.getsize(d["base_db"]) if d["base_db"] and os.path.exists(d["base_db"]) else 0,
            "delta_exists": bool(d["delta_sql"]) and os.path.exists(d["delta_sql"]),
            "delta_size": os.path.getsize(d["delta_sql"]) if d["delta_sql"] and os.path.exists(d["delta_sql"]) else 0,
        }
        for d in discover_datasets()
    ]


def parse_os_from_delta_sql(delta_sql_path):
    """
    Delta SQL dosyasini satir satir tarayip INSERT INTO OS satirlarini ayrıstirir.
    Sadece az sayida OS satiri oldugundan tum dosya okunsa bile hizlidir.
    """
    entries = []
    # re.IGNORECASE kullanilmaz: [^V] ile [^Vv] olur, 'version' sutunundaki 'v' eslesmeyi kirdirir.
    # SQL dosyasinda INSERT INTO OS buyuk harfle yaziliyor, IGNORECASE gerek yok.
    pat = re.compile(
        r"INSERT INTO OS[^V]*VALUES\s*\(\d+,\d+,'((?:[^']|'')*)',\s*'((?:[^']|'')*)'\)"
    )
    try:
        with open(delta_sql_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if 'INSERT INTO OS' not in line.upper():
                    continue
                m = pat.search(line)
                if m:
                    name = m.group(1).replace("''", "'")
                    ver  = m.group(2).replace("''", "'")
                    entries.append((name, ver))
    except Exception:
        pass
    return entries


def build_os_groups(dataset):
    """
    Secilen datasetin OS listesini okur, mantikli gruplara ayirir.
    Base DB + delta SQL birlestirilir; delta'ya ozel girişler [delta] etiketiyle gosterilir.
    """
    base_db, delta_sql = _get_dataset_info(dataset)
    if not base_db or not os.path.exists(base_db):
        folder = os.path.join(NSRL_ROOT, dataset)
        return {"error": f"'{dataset}' dataseti için .db bulunamadı. Aranan klasör: {folder}"}

    try:
        conn = sqlite3.connect(f"file:{base_db}?mode=ro", uri=True)
        cur = conn.cursor()
        try:
            cur.execute("SELECT DISTINCT operating_system_id, name, version FROM OS ORDER BY name, version")
            base_rows = cur.fetchall()  # (os_id, name, ver)
        except Exception:
            # Schema degistiyse os_id olmadan devam et
            cur.execute("SELECT DISTINCT name, version FROM OS ORDER BY name, version")
            base_rows = [(None, n, v) for n, v in cur.fetchall()]
        conn.close()
    except Exception as e:
        return {"error": str(e)}

    # Delta SQL'deki tum OS girisleri (base DB'de olsa bile) delta panelinde gosterilir
    delta_only = []
    if delta_sql and os.path.exists(delta_sql):
        for name, ver in parse_os_from_delta_sql(delta_sql):
            delta_only.append((name, ver))

    groups = {}

    def mk(name, ver, os_id=None, is_delta=False):
        n = (name or "").strip()
        v = (ver or "").strip()
        if not v or v == n or v.lower() in n.lower():
            label = n
        else:
            sim = difflib.SequenceMatcher(None, n.lower(), v.lower()).ratio()
            label = n if sim >= 0.70 else f"{n} {v}"
        return {"os_id": os_id, "name": n, "version": v, "label": label, "value": f"{n}|{v}", "is_delta": is_delta}

    def add(g, item):
        groups.setdefault(g, []).append(item)

    dtype = _infer_type(dataset)

    def _place(name, ver, os_id=None, is_delta=False):
        it = mk(name, ver, os_id=os_id, is_delta=is_delta)
        u = (name or "").upper()
        if dtype == "modern":
            if "WINDOWS 11" in u or (u.startswith("WINDOWS") and "11" in u):
                add("Windows 11", it)
            elif "WINDOWS 10" in u:
                add("Windows 10", it)
            elif "WINDOWS 8" in u:
                add("Windows 8 / 8.1", it)
            elif "WINDOWS 7" in u:
                add("Windows 7", it)
            elif any(y in u for y in ("2012", "2016", "2019", "2022", "2025", "SERVER")):
                add("Windows Server", it)
            elif "MACOS" in u or "MAC OS" in u or "OS X" in u:
                add("macOS", it)
            elif "WINDOWS" in u:
                add("Diger Windows", it)
            else:
                add("Diger", it)
        elif dtype == "ios":
            major = (ver or "").split(".")[0].strip()
            if "IPAD" in u or "IPADOS" in u:
                g = f"iPadOS {major}" if major else "iPadOS"
            elif "WATCH" in u:
                g = f"watchOS {major}" if major else "watchOS"
            elif "TVOS" in u:
                g = f"tvOS {major}" if major else "tvOS"
            elif "IOS" in u:
                g = f"iOS {major}" if major else "iOS"
            else:
                g = name or "Diger"
            add(g, it)
        elif dtype == "android":
            major = (ver or "").split(".")[0].strip()
            g = f"Android {major}" if major.isdigit() else (name or "Android")
            add(g, it)
        elif dtype == "legacy":
            if "XP" in u:
                add("Windows XP", it)
            elif "VISTA" in u:
                add("Windows Vista", it)
            elif "2000" in u:
                add("Windows 2000", it)
            elif "98" in u:
                add("Windows 98", it)
            elif "95" in u:
                add("Windows 95", it)
            elif "NT" in u:
                add("Windows NT", it)
            elif "2003" in u or "2008" in u:
                add("Windows Server (Eski)", it)
            else:
                add("Diger Eski", it)

    for os_id, name, ver in base_rows:
        _place(name, ver, os_id=os_id, is_delta=False)
    for name, ver in delta_only:
        _place(name, ver, os_id=None, is_delta=True)

    return {k: v for k, v in groups.items() if v}


def build_os_groups_multi(dataset_ids):
    """Birden fazla dataset'in OS listesini birlestirir; grup adlarina dataset etiketi ekler."""
    merged = {}
    for ds_id in dataset_ids:
        single = build_os_groups(ds_id)
        if "error" in single:
            continue
        dtype = _infer_type(ds_id).title()
        label = dtype  # Modern, Legacy, Android, Ios
        for group_name, items in single.items():
            tagged_name = f"{label} — {group_name}"
            merged.setdefault(tagged_name, []).extend(items)
    return merged


class Handler(http.server.SimpleHTTPRequestHandler):
    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            with open(os.path.join(BASE_DIR, 'index.html'), 'rb') as f:
                self.wfile.write(f.read())
            return

        if path == '/api/datasets':
            return self._json(dataset_status())

        if path == '/api/os_list':
            dataset_ids = [d for d in qs.get('dataset', []) if d]
            if len(dataset_ids) == 1:
                groups = build_os_groups(dataset_ids[0])
            elif len(dataset_ids) > 1:
                groups = build_os_groups_multi(dataset_ids)
            else:
                groups = {"error": "Dataset belirtilmedi"}
            return self._json(groups)

        if path == '/api/existing_dbs':
            # output/ altinda _filter_meta tablosu olan .db'leri listele
            return self._json(list_existing_filtered_dbs())

        if path == '/api/delta_files':
            dataset = qs.get('dataset', [''])[0] or ''
            return self._json(list_delta_files(dataset))

        if path == '/api/progress':
            self.send_response(200)
            self.send_header('Content-type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            global current_process
            if current_process is None:
                self.wfile.write(b"data: finished\n\n")
                return

            try:
                buffer = b""
                while current_process and current_process.poll() is None:
                    char = current_process.stdout.read(1)
                    if not char:
                        break
                    if char == b'\r' or char == b'\n':
                        if buffer:
                            s = buffer.decode('utf-8', errors='replace').strip()
                            if s:
                                prefix = "\r" if char == b'\r' else ""
                                data = json.dumps({"log": prefix + s})
                                self.wfile.write(f"data: {data}\n\n".encode('utf-8'))
                                self.wfile.flush()
                        buffer = b""
                    else:
                        buffer += char
                if current_process:
                    rc = current_process.wait()
                    if rc == 0:
                        msg = 'ISLEM TAMAMLANDI!'
                    elif rc == 130:
                        msg = 'ISLEM IPTAL EDILDI. Olusan DB tamamlanmamis kabul edilir.'
                    else:
                        msg = f'ISLEM HATA ILE BITTI (kod {rc}). Logdaki [HATA] satirini kontrol edin.'
                    self.wfile.write(f"data: {json.dumps({'log': msg})}\n\n".encode('utf-8'))
                    self.wfile.flush()
                self.wfile.write(b"data: finished\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return

        super().do_GET()

    def do_POST(self):
        global current_process

        if self.path == '/api/start':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))

            if current_process is not None and current_process.poll() is None:
                return self._json({"error": "Islem zaten calisiyor."}, 400)

            valid_hashes = {"sha256", "sha1", "md5", "crc32"}
            hash_types_in = data.get("hash_types") or ["sha256", "sha1", "md5", "crc32"]
            hash_types = [h for h in hash_types_in if h in valid_hashes] or ["md5"]
            pkg_filter = (data.get("pkg_filter") or "").strip()

            # --- Tek dataset (eski format) veya coklu dataset (jobs dizisi) ---
            raw_jobs = data.get("jobs")
            if not raw_jobs:
                # Eski tek-dataset formati: geriye donuk uyumluluk
                raw_jobs = [{
                    "dataset":     data.get("dataset", "modern"),
                    "selected_os": data.get("selected_os", []),
                    "apply_delta": bool(data.get("apply_delta")),
                    "delta_path":  (data.get("delta_path") or "").strip(),
                }]

            # Dogrulama + job spec olustur
            job_specs = []
            all_os_names = []
            any_delta = False
            for ji, jd in enumerate(raw_jobs):
                ds_id      = jd.get("dataset", "")
                sel_os     = jd.get("selected_os", [])
                apply_dl   = bool(jd.get("apply_delta"))
                delta_path = (jd.get("delta_path") or "").strip()

                if not sel_os:
                    return self._json({"error": f"Dataset '{ds_id}': hicbir OS secilmedi."}, 400)

                base_db, auto_delta = _get_dataset_info(ds_id)
                if not base_db or not os.path.exists(base_db):
                    expected = os.path.join(NSRL_ROOT, ds_id)
                    return self._json({"error": f"'{ds_id}' dataseti icin .db bulunamadi: {expected}"}, 400)

                delta_sql = delta_path if delta_path else (auto_delta if apply_dl else None)
                if apply_dl and (not delta_sql or not os.path.exists(delta_sql)):
                    return self._json({"error": f"'{ds_id}' delta SQL bulunamadi: {delta_sql or '(yok)'}"}, 400)

                # OS filtre dosyasi — name bazinda unique (base+delta cakismalari tek satira indirgenir)
                filter_path = os.path.join(BASE_DIR, f"temp_os_filter_{ds_id}_{ji}.txt")
                seen = set()
                with open(filter_path, "w", encoding="utf-8") as ff:
                    for item in sel_os:
                        nm = item.split("|", 1)[0].strip()
                        if nm and nm not in seen:
                            seen.add(nm)
                            ff.write(nm + "\n")
                if len(sel_os) != len(seen):
                    print(f"  [{ds_id}] {len(sel_os)} OS secildi -> {len(seen)} unique OS adi (base+delta cakismalari birlestirildi)", flush=True)

                names_for_slug = [it.split("|", 1)[0].strip() for it in sel_os]
                all_os_names.extend(names_for_slug)
                if apply_dl:
                    any_delta = True

                job_specs.append({
                    "source_db":     base_db,
                    "delta_sql":     delta_sql if apply_dl else None,
                    "os_list_file":  filter_path,
                    "dataset":       ds_id,
                    "dataset_label": f"{_infer_type(ds_id).title()} ({ds_id})",
                    "tag":           f"{ds_id}_{ji}",
                })

            # Cikti DB ismi
            out_dir = os.path.join(BASE_DIR, "output")
            os.makedirs(out_dir, exist_ok=True)
            combined_dataset = "+".join(jd.get("dataset", "") for jd in raw_jobs)
            slug = make_output_slug(combined_dataset, all_os_names, any_delta)
            output_db = unique_output_path(os.path.join(out_dir, f"{slug}.db"))

            # Job dosyasi yaz
            do_dedup = bool(data.get("do_dedup"))
            job_file = os.path.join(BASE_DIR, "_current_job.json")
            with open(job_file, "w", encoding="utf-8") as jf:
                json.dump({
                    "target_db":  output_db,
                    "hash_types": hash_types,
                    "pkg_filter": pkg_filter,
                    "do_dedup":   do_dedup,
                    "jobs":       job_specs,
                }, jf, ensure_ascii=False, indent=2)

            python_exe = sys.executable
            extractor  = os.path.join(BASE_DIR, "nsrl_extractor.py")
            cmd = [python_exe, extractor, "--job-file", job_file]

            current_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, universal_newlines=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            )
            return self._json({"status": "started", "output": output_db})

        if self.path == '/api/append_delta':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            existing_db = (data.get("existing_db") or "").strip()
            dataset = (data.get("dataset") or "").strip()   # lower() YOK — klasör adı case-sensitive
            delta_path = (data.get("delta_path") or "").strip()

            if current_process is not None and current_process.poll() is None:
                return self._json({"error": "Baska bir islem calisiyor."}, 400)
            if not existing_db or not os.path.exists(existing_db):
                return self._json({"error": f"Mevcut DB bulunamadi: {existing_db}"}, 400)

            # Delta path: UI'dan gelen oncelikli, yoksa dataset'ten auto
            delta_sql = delta_path if delta_path else None
            if not delta_sql and dataset:
                _, delta_sql = _get_dataset_info(dataset)
            if not delta_sql or not os.path.exists(delta_sql):
                return self._json({"error": f"Delta .sql bulunamadi: {delta_sql or '(secilmedi)'}. NSRL/{dataset}/ altina koyun ve listeden secin."}, 400)

            python_exe = sys.executable
            extractor = os.path.join(BASE_DIR, "nsrl_extractor.py")
            cmd = [python_exe, extractor,
                   "--append-delta",
                   "--existing", existing_db,
                   "--delta", delta_sql]

            current_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, universal_newlines=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            )
            return self._json({"status": "started", "output": existing_db})

        if self.path == '/api/dedup_db':
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            db_path = (data.get("db_path") or "").strip()

            if current_process is not None and current_process.poll() is None:
                return self._json({"error": "Baska bir islem calisiyor."}, 400)
            if not db_path or not os.path.exists(db_path):
                return self._json({"error": f"DB bulunamadi: {db_path}"}, 400)

            python_exe = sys.executable
            extractor  = os.path.join(BASE_DIR, "nsrl_extractor.py")
            cmd = [python_exe, extractor, "--dedup-db", db_path]

            current_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, universal_newlines=False,
                env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            )
            return self._json({"status": "started", "output": db_path})

        if self.path == '/api/stop':
            if current_process is not None and current_process.poll() is None:
                try:
                    if sys.platform == 'win32':
                        subprocess.call(
                            ['taskkill', '/F', '/T', '/PID', str(current_process.pid)],
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        current_process.kill()
                except Exception:
                    pass
                current_process = None
            return self._json({"status": "stopped"})

        if self.path == '/api/shutdown':
            if current_process is not None and current_process.poll() is None:
                try:
                    if sys.platform == 'win32':
                        # /T: child process'leri de öldür (tüm process tree)
                        subprocess.call(
                            ['taskkill', '/F', '/T', '/PID', str(current_process.pid)],
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        current_process.kill()
                except Exception:
                    pass
            # Content-Length ile tarayici response'un bittigini kesin bilir,
            # Connection: close ile soket temiz kapanir
            body = b'{"status":"shutdown"}'
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except Exception:
                pass
            threading.Timer(0.8, lambda: os._exit(0)).start()
            return

        self._json({"error": "not found"}, 404)


current_process = None


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True       # thread'ler main ile birlikte kapanabilsin
    allow_reuse_address = True


def run_server():
    with ThreadingHTTPServer(("", PORT), Handler) as httpd:
        print(f"Sunucu baslatildi: http://localhost:{PORT}")
        httpd.serve_forever()


if __name__ == '__main__':
    run_server()
