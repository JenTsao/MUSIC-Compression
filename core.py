"""压缩核心逻辑：扫描、任务规划、ffmpeg 调用。CLI 与 GUI 共用。"""

import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXTS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma'}

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def ffmpeg_exe() -> str:
    """打包运行时优先使用 exe 同目录下自带的 ffmpeg.exe，否则走 PATH。"""
    if getattr(sys, 'frozen', False):
        cand = Path(sys.executable).parent / 'ffmpeg.exe'
        if cand.is_file():
            return str(cand)
    return 'ffmpeg'


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用。"""
    exe = ffmpeg_exe()
    if os.path.isfile(exe):
        return True
    return shutil.which(exe) is not None


def safe_name(name: str, keep_unicode: bool = False, max_len: int = 80) -> str:
    """生成华为兼容的安全文件名。默认只保留 ASCII 字母数字下划线连字符。"""
    stem = Path(name).stem.replace(' ', '_')

    if keep_unicode:
        stem = re.sub(r'[^A-Za-z0-9_\-\u4e00-\u9fff]', '_', stem)
    else:
        stem = stem.encode('ascii', 'ignore').decode('ascii')
        stem = re.sub(r'[^A-Za-z0-9_\-]', '_', stem)

    stem = re.sub(r'_+', '_', stem).strip('_')[:max_len]
    return (stem or 'audio') + '.mp3'


def unique_path(path: Path, used: set, avoid_existing: bool = True) -> Path:
    """避免输出文件重名。用 normcase 代替 resolve()，避免每文件一次磁盘 I/O。"""
    key = os.path.normcase(str(path))

    if (not avoid_existing or not path.exists()) and key not in used:
        used.add(key)
        return path

    parent, stem, suffix = path.parent, path.stem, path.suffix
    i = 1
    while True:
        new_path = parent / f'{stem}_{i}{suffix}'
        key = os.path.normcase(str(new_path))
        if (not avoid_existing or not new_path.exists()) and key not in used:
            used.add(key)
            return new_path
        i += 1


@dataclass
class ScanOptions:
    exts: set = field(default_factory=lambda: set(SUPPORTED_EXTS))
    exclude_dir_keywords: list = field(default_factory=list)  # 目录名含任一关键词则跳过
    include_keywords: list = field(default_factory=list)      # 文件名需含任一关键词
    exclude_keywords: list = field(default_factory=list)      # 文件名含任一关键词则排除

    def _has_kw(self, name: str, kws: list) -> bool:
        low = name.lower()
        return any(k.lower() in low for k in kws if k)

    def match_file(self, p: Path) -> bool:
        if p.suffix.lower() not in self.exts:
            return False
        name = p.name
        if self.include_keywords and not self._has_kw(name, self.include_keywords):
            return False
        if self.exclude_keywords and self._has_kw(name, self.exclude_keywords):
            return False
        return True


def scan_files(roots: list, opt: ScanOptions) -> list:
    """扫描多个根目录/文件。用 os.walk 支持目录剪枝，比 rglob 更快。"""
    files, seen = [], set()

    for root in roots:
        root = Path(root)
        if root.is_file():
            if opt.match_file(root):
                key = os.path.normcase(str(root))
                if key not in seen:
                    seen.add(key)
                    files.append(root)
            continue

        if not root.is_dir():
            continue

        for dirpath, dirnames, filenames in os.walk(root):
            # 剪枝：跳过命中排除关键词的子目录（含隐藏目录如 .git）
            dirnames[:] = [
                d for d in dirnames
                if not (opt.exclude_dir_keywords and opt._has_kw(d, opt.exclude_dir_keywords))
            ]
            for fn in filenames:
                p = Path(dirpath) / fn
                if not opt.match_file(p):
                    continue
                key = os.path.normcase(str(p))
                if key not in seen:
                    seen.add(key)
                    files.append(p)

    files.sort(key=lambda p: os.path.normcase(str(p)))
    return files


@dataclass
class JobOptions:
    output_dir: Path = None
    bitrate: str = '128k'
    sample_rate: int = 44100
    channels: int = 1
    jobs: int = os.cpu_count() or 4
    keep_tree: bool = False
    overwrite: bool = False
    keep_unicode: bool = False


@dataclass
class Task:
    src: Path
    dst: Path
    src_size: int


def plan_tasks(files: list, roots: list, opts: JobOptions) -> list:
    """规划输出路径。keep_tree 时相对于各自所属根目录保持结构。"""
    output_dir = Path(opts.output_dir)
    used_paths = set()
    tasks = []

    for idx, src in enumerate(files, 1):
        root = None
        for r in roots:
            r = Path(r)
            try:
                src.relative_to(r if r.is_dir() else r.parent)
                root = r
                break
            except ValueError:
                continue

        if opts.keep_tree and root and root.is_dir():
            parts = []
            for part in src.relative_to(root).parts[:-1]:
                if opts.keep_unicode:
                    safe_part = re.sub(r'[^A-Za-z0-9_\-\u4e00-\u9fff]', '_', part)
                else:
                    safe_part = re.sub(
                        r'[^A-Za-z0-9_\-]', '_',
                        part.encode('ascii', 'ignore').decode('ascii')
                    )
                parts.append(safe_part.strip('_') or 'dir')

            dst = output_dir.joinpath(*parts, safe_name(src.name, opts.keep_unicode))
        else:
            base = safe_name(src.name, opts.keep_unicode)
            if base == 'audio.mp3' or not re.search(r'[A-Za-z0-9]', Path(base).stem):
                base = f'{idx:04d}_{base}'
            dst = output_dir / base

        dst = unique_path(dst, used_paths, avoid_existing=not opts.overwrite)
        try:
            src_size = src.stat().st_size
        except OSError:
            src_size = 0
        tasks.append(Task(src, dst, src_size))

    return tasks


def run_ffmpeg(task: Task, opts: JobOptions, threads: int = 0):
    """压缩单个文件。返回 (status, task, out_size_or_err)。

    status: 'ok' | 'skip' | 'fail'
    """
    src, dst = task.src, task.dst

    if dst.exists() and not opts.overwrite:
        return ('skip', task, 0)

    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_exe(),
        '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-y' if opts.overwrite else '-n',
        '-i', str(src),
        '-map', '0:a:0',                       # 只取第一个音频流，避免封面导致兼容问题
        '-map_metadata', '0',
        '-id3v2_version', '3',                 # 华为兼容性较好的 ID3v2.3 + ID3v1
        '-write_id3v1', '1',
        '-write_xing', '0',
        '-c:a', 'libmp3lame', '-b:a', opts.bitrate,
        '-ar', str(opts.sample_rate), '-ac', str(opts.channels),
    ]
    if threads > 0:
        cmd += ['-threads', str(threads)]
    cmd.append(str(dst))

    creationflags = CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        subprocess.run(
            cmd, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        return ('fail', task, err)
    except OSError as e:
        return ('fail', task, str(e))

    try:
        return ('ok', task, dst.stat().st_size)
    except OSError:
        return ('fail', task, '输出文件不存在')


def compress_all(
    tasks: list,
    opts: JobOptions,
    progress_cb=None,
    cancel_event=None,
) -> dict:
    """并行压缩。progress_cb(done, total, done_bytes, total_bytes, message)。

    返回汇总 dict：ok/skip/fail/total_src/total_dst/elapsed/cancelled。
    """
    total = len(tasks)
    total_bytes = sum(t.src_size for t in tasks)
    done = done_bytes = out_bytes = ok = skip = fail = 0
    start = time.time()

    # 并行时限制每个 ffmpeg 的线程数，避免线程过度订阅
    workers = max(1, min(opts.jobs, total))
    per_task_threads = max(1, (os.cpu_count() or 4) // workers) if workers > 1 else 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_ffmpeg, t, opts, per_task_threads): t for t in tasks
        }
        for fut in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                for f in futures:
                    f.cancel()
                break

            status, task, extra = fut.result()
            done += 1

            if status == 'ok':
                ok += 1
                done_bytes += task.src_size
                out_bytes += extra
                saved = (1 - extra / task.src_size) * 100 if task.src_size else 0
                msg = (
                    f"OK  {task.src.name} -> {task.dst.name}  "
                    f"{task.src_size / 1048576:.2f}MB -> {extra / 1048576:.2f}MB  省 {saved:.0f}%"
                )
            elif status == 'skip':
                skip += 1
                done_bytes += task.src_size
                msg = f'跳过：{task.dst.name} 已存在'
            else:
                fail += 1
                msg = f'失败：{task.src}\n{extra}'

            if progress_cb:
                progress_cb(done, total, done_bytes, total_bytes, msg)

    return {
        'ok': ok, 'skip': skip, 'fail': fail,
        'total': total,
        'total_src': total_bytes,
        'total_dst': out_bytes,
        'elapsed': time.time() - start,
        'cancelled': bool(cancel_event is not None and cancel_event.is_set()),
    }
