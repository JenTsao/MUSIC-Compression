"""华为手表 MP3 批量压缩工具（CLI）。GUI 见 gui.py。"""

import argparse
import sys
from pathlib import Path

from core import (
    SUPPORTED_EXTS,
    JobOptions,
    ScanOptions,
    check_ffmpeg,
    compress_all,
    plan_tasks,
    scan_files,
)


def parse_keywords(text: str) -> list:
    return [k.strip() for k in text.split(',') if k.strip()]


def main():
    parser = argparse.ArgumentParser(description='华为手表 MP3 批量压缩工具')
    parser.add_argument('input_dirs', nargs='+', type=Path,
                        help='输入目录（可多个）或文件，目录会递归扫描')
    parser.add_argument('-o', '--output', type=Path, default=None,
                        help='输出目录，默认：第一个输入目录/compressed_huawei')
    parser.add_argument('-b', '--bitrate', default='128k',
                        help='MP3 码率，默认 128k。华为建议 128k-192k')
    parser.add_argument('-ar', '--sample-rate', type=int, default=44100,
                        help='采样率，默认 44100')
    parser.add_argument('-ac', '--channels', type=int, default=1, choices=[1, 2],
                        help='声道数，默认 1 单声道，体积更小')
    parser.add_argument('-j', '--jobs', type=int, default=max(1, __import__('os').cpu_count() or 4),
                        help='并行任务数')
    parser.add_argument('--keep-tree', action='store_true',
                        help='保持原目录结构；默认全部平铺到输出目录')
    parser.add_argument('--overwrite', action='store_true',
                        help='覆盖已存在文件；默认不覆盖，重名自动加序号')
    parser.add_argument('--keep-unicode', action='store_true',
                        help='文件名保留中文；默认只保留英文数字下划线，兼容性更好')
    parser.add_argument('--exclude-dir', default='',
                        help='排除目录名关键词，逗号分隔（如 temp,cover）')
    parser.add_argument('--include', default='',
                        help='文件名需包含的关键词，逗号分隔')
    parser.add_argument('--exclude', default='',
                        help='文件名包含即排除的关键词，逗号分隔')
    parser.add_argument('--ext', default=','.join(sorted(SUPPORTED_EXTS)),
                        help='输入扩展名，逗号分隔')

    args = parser.parse_args()

    if not check_ffmpeg():
        print('错误：未找到 ffmpeg/ffprobe，请先安装 ffmpeg 并加入 PATH。')
        sys.exit(1)

    roots = [r.resolve() for r in args.input_dirs]
    for r in roots:
        if not r.exists():
            print(f'错误：输入不存在：{r}')
            sys.exit(1)

    exts = set()
    for e in args.ext.split(','):
        e = e.strip().lower()
        if e:
            exts.add(e if e.startswith('.') else '.' + e)

    scan_opt = ScanOptions(
        exts=exts,
        exclude_dir_keywords=parse_keywords(args.exclude_dir),
        include_keywords=parse_keywords(args.include),
        exclude_keywords=parse_keywords(args.exclude),
    )

    output_dir = args.output.resolve() if args.output else roots[0] / 'compressed_huawei'

    files = scan_files(roots, scan_opt)
    if not files:
        print('没有找到可处理的音频文件。')
        return

    job_opts = JobOptions(
        output_dir=output_dir,
        bitrate=args.bitrate,
        sample_rate=args.sample_rate,
        channels=args.channels,
        jobs=args.jobs,
        keep_tree=args.keep_tree,
        overwrite=args.overwrite,
        keep_unicode=args.keep_unicode,
    )

    print(f'输入：{len(roots)} 个根，文件数量：{len(files)}')
    print(f'输出目录：{output_dir}')
    print(
        f'参数：码率={args.bitrate}, 采样率={args.sample_rate}, '
        f'声道={args.channels}, 并行={args.jobs}'
    )
    print('华为兼容：MP3 CBR / ID3v2.3+ID3v1 / 禁用 Xing / 安全文件名\n')

    tasks = plan_tasks(files, roots, job_opts)

    def progress(done, total, done_bytes, total_bytes, msg):
        pct = done_bytes * 100 // total_bytes if total_bytes else 0
        print(f'[{done}/{total} {pct:3d}%] {msg}')

    summary = compress_all(tasks, job_opts, progress_cb=progress)

    print('\n完成' + ('（已取消）' if summary['cancelled'] else ''))
    print(f"成功：{summary['ok']}，跳过：{summary['skip']}，失败：{summary['fail']}")

    if summary['total_src']:
        print(f"原始总大小：{summary['total_src'] / 1048576:.2f} MB")
        print(f"输出总大小：{summary['total_dst'] / 1048576:.2f} MB")
        print(f"总节省：{(1 - summary['total_dst'] / summary['total_src']) * 100:.1f}%")

    print(f"耗时：{summary['elapsed']:.1f}s")
    print(f'输出目录：{output_dir}')
    print('\n华为导入提示：')
    print('1. 把输出目录里的 mp3 复制到手机内部存储的 Music 文件夹；')
    print('2. 打开华为运动健康 App -> 设备 -> 音乐 -> 添加音乐/管理音乐；')
    print('3. 文件名已尽量处理为英文数字下划线，避免中文和特殊符号导致导入失败。')


if __name__ == '__main__':
    main()
