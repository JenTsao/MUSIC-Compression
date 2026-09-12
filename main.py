
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SUPPORTED_EXTS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma'}


def check_ffmpeg():
    for exe in ('ffmpeg', 'ffprobe'):
        if shutil.which(exe) is None:
            print(f"错误：未找到 {exe}，请先安装 ffmpeg 并加入 PATH。")
            sys.exit(1)


def safe_name(name: str, keep_unicode: bool = False, max_len: int = 80) -> str:
    """生成华为兼容的安全文件名。默认只保留 ASCII 字母数字下划线连字符。"""
    stem = Path(name).stem
    stem = stem.replace(' ', '_')

    if not keep_unicode:
        stem = stem.encode('ascii', 'ignore').decode('ascii')

    if keep_unicode:
        stem = re.sub(r'[^A-Za-z0-9_\-\u4e00-\u9fff]', '_', stem)
    else:
        stem = re.sub(r'[^A-Za-z0-9_\-]', '_', stem)

    stem = re.sub(r'_+', '_', stem).strip('_')
    if not stem:
        stem = 'audio'

    stem = stem[:max_len]
    return stem + '.mp3'


def unique_path(path: Path, used: set, avoid_existing: bool = True) -> Path:
    """避免输出文件重名。"""
    key = str(path.resolve()).lower()

    if (not avoid_existing or not path.exists()) and key not in used:
        used.add(key)
        return path

    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    i = 1

    while True:
        new_path = parent / f"{stem}_{i}{suffix}"
        key = str(new_path.resolve()).lower()
        if (not avoid_existing or not new_path.exists()) and key not in used:
            used.add(key)
            return new_path
        i += 1


def run_ffmpeg(
    src: Path,
    dst: Path,
    bitrate: str,
    sample_rate: int,
    channels: int,
    overwrite: bool,
):
    """调用 ffmpeg 压缩单个文件。"""
    if dst.exists() and not overwrite:
        return ('skip', src, dst, 0, 0)

    dst.parent.mkdir(parents=True, exist_ok=True)
    src_size = src.stat().st_size

    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel', 'error',
        '-y' if overwrite else '-n',
        '-i', str(src),

        # 只取第一个音频流，避免封面/多流导致兼容问题
        '-map', '0:a:0',

        # 复制元数据，并写成华为兼容性较好的 ID3v2.3 + ID3v1
        '-map_metadata', '0',
        '-id3v2_version', '3',
        '-write_id3v1', '1',
        '-write_xing', '0',

        # MP3 CBR 编码
        '-c:a', 'libmp3lame',
        '-b:a', bitrate,
        '-ar', str(sample_rate),
        '-ac', str(channels),

        str(dst),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode('utf-8', errors='ignore') if e.stderr else str(e)
        return ('fail', src, dst, src_size, err)

    dst_size = dst.stat().st_size if dst.exists() else 0
    return ('ok', src, dst, src_size, dst_size)


def main():
    parser = argparse.ArgumentParser(description='华为手表 MP3 批量压缩工具')
    parser.add_argument('input_dir', type=Path, help='输入目录，会递归扫描')
    parser.add_argument(
        '-o', '--output', type=Path, default=None,
        help='输出目录，默认：输入目录/compressed_huawei'
    )
    parser.add_argument(
        '-b', '--bitrate', default='128k',
        help='MP3 码率，默认 128k。华为建议 128k-192k'
    )
    parser.add_argument(
        '-ar', '--sample-rate', type=int, default=44100,
        help='采样率，默认 44100'
    )
    parser.add_argument(
        '-ac', '--channels', type=int, default=1, choices=[1, 2],
        help='声道数，默认 1 单声道，体积更小'
    )
    parser.add_argument(
        '-j', '--jobs', type=int, default=max(1, os.cpu_count() or 4),
        help='并行任务数'
    )
    parser.add_argument(
        '--keep-tree', action='store_true',
        help='保持原目录结构；默认全部平铺到输出目录'
    )
    parser.add_argument(
        '--overwrite', action='store_true',
        help='覆盖已存在文件；默认不覆盖，重名自动加序号'
    )
    parser.add_argument(
        '--keep-unicode', action='store_true',
        help='文件名保留中文；默认只保留英文数字下划线，兼容性更好'
    )
    parser.add_argument(
        '--ext', default=','.join(sorted(SUPPORTED_EXTS)),
        help='输入扩展名，逗号分隔'
    )

    args = parser.parse_args()
    check_ffmpeg()

    input_dir = args.input_dir.resolve()
    if not input_dir.is_dir():
        print(f"错误：输入目录不存在：{input_dir}")
        sys.exit(1)

    output_dir = args.output.resolve() if args.output else input_dir / 'compressed_huawei'

    exts = set()
    for e in args.ext.split(','):
        e = e.strip().lower()
        if e:
            exts.add(e if e.startswith('.') else '.' + e)

    files = [
        p for p in input_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in exts
    ]

    if not files:
        print('没有找到可处理的音频文件。')
        return

    print(f'输入目录：{input_dir}')
    print(f'输出目录：{output_dir}')
    print(f'文件数量：{len(files)}')
    print(
        f'参数：码率={args.bitrate}, 采样率={args.sample_rate}, '
        f'声道={args.channels}, 并行={args.jobs}'
    )
    print('华为兼容：MP3 CBR / ID3v2.3+ID3v1 / 禁用 Xing / 安全文件名\n')

    used_paths = set()
    tasks = []

    for idx, src in enumerate(files, 1):
        if args.keep_tree:
            rel = src.relative_to(input_dir)
            parts = []

            for part in rel.parts[:-1]:
                if args.keep_unicode:
                    safe_part = re.sub(
                        r'[^A-Za-z0-9_\-\u4e00-\u9fff]', '_', part
                    ).strip('_') or 'dir'
                else:
                    safe_part = re.sub(
                        r'[^A-Za-z0-9_\-]', '_',
                        part.encode('ascii', 'ignore').decode('ascii')
                    ).strip('_') or 'dir'
                parts.append(safe_part)

            out_name = safe_name(src.name, keep_unicode=args.keep_unicode)
            dst = output_dir.joinpath(*parts, out_name)
        else:
            base = safe_name(src.name, keep_unicode=args.keep_unicode)

            # 如果原文件名被清空成 audio.mp3，加序号方便管理
            if base == 'audio.mp3' or not re.search(r'[A-Za-z0-9]', Path(base).stem):
                base = f'{idx:04d}_{base}'

            dst = output_dir / base

        dst = unique_path(
            dst,
            used_paths,
            avoid_existing=not args.overwrite
        )
        tasks.append((src, dst))

    total_src = 0
    total_dst = 0
    ok = skip = fail = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(
                run_ffmpeg,
                src,
                dst,
                args.bitrate,
                args.sample_rate,
                args.channels,
                args.overwrite,
            )
            for src, dst in tasks
        ]

        for i, fut in enumerate(as_completed(futures), 1):
            status, src, dst, src_size, extra = fut.result()

            if status == 'ok':
                ok += 1
                total_src += src_size
                total_dst += extra
                saved = (1 - extra / src_size) * 100 if src_size else 0
                print(
                    f'[{i}/{len(tasks)}] OK  {src.name} -> {dst.name}  '
                    f'{src_size / 1024 / 1024:.2f}MB -> '
                    f'{extra / 1024 / 1024:.2f}MB  省 {saved:.0f}%'
                )
            elif status == 'skip':
                skip += 1
                print(f'[{i}/{len(tasks)}] 跳过：{dst.name} 已存在')
            else:
                fail += 1
                print(f'[{i}/{len(tasks)}] 失败：{src}\n{extra}')

    elapsed = time.time() - start

    print('\n完成')
    print(f'成功：{ok}，跳过：{skip}，失败：{fail}')

    if total_src:
        print(f'原始总大小：{total_src / 1024 / 1024:.2f} MB')
        print(f'输出总大小：{total_dst / 1024 / 1024:.2f} MB')
        print(f'总节省：{(1 - total_dst / total_src) * 100:.1f}%')

    print(f'耗时：{elapsed:.1f}s')
    print(f'输出目录：{output_dir}')
    print('\n华为导入提示：')
    print('1. 把输出目录里的 mp3 复制到手机内部存储的 Music 文件夹；')
    print('2. 打开华为运动健康 App -> 设备 -> 音乐 -> 添加音乐/管理音乐；')
    print('3. 文件名已尽量处理为英文数字下划线，避免中文和特殊符号导致导入失败。')


if __name__ == '__main__':
    main()