# 华为手表 MP3 批量压缩工具

把各种音频（MP3 / FLAC / WAV / M4A / AAC / OGG / WMA）批量转成**华为手表/运动健康 App 兼容性最好**的 MP3：

- MP3 CBR 码率（默认 128k）
- ID3v2.3 + ID3v1 标签、禁用 Xing
- 安全文件名（默认去掉中文和特殊符号，可保留）
- 只取第一个音频流，避免封面导致导入失败

提供 **GUI（PySide6）** 和 **CLI** 两种用法，核心逻辑在 `core.py`。

## 下载成品

到 [Releases](../../releases) 下载 `MP3Compressor-windows-x64.zip`，解压后 `MP3Compressor.exe` 和 `ffmpeg.exe` 放同一目录即可，**无需安装 Python 和 ffmpeg**。

也可以自己构建（见下文「打包 exe」）。

## GUI 使用

```bash
pip install PySide6
python gui.py
```

功能：

- 输入列表可添加**多个文件夹和多个文件**，递归扫描
- 过滤：排除目录名关键词、只含/排除文件名关键词（不区分大小写）
- **任务队列**：把不同参数组合存为命名任务（如「车载 64k」「手表 128k」），一键按队列依次执行
- 实时进度条（按字节）、日志窗口、取消按钮、扫描预览（先看文件数和总大小）
- 启动时若 exe 同目录有 `ffmpeg.exe` 会自动使用，否则要求 ffmpeg 在 PATH 中

## CLI 使用

```bash
# 基本用法：压缩整个目录（递归），输出到 输入目录/compressed_huawei
python main.py "D:\Music"

# 多个输入目录 + 过滤
python main.py "D:\Music" "E:\Podcasts" --exclude-dir temp,cover --exclude 伴奏,demo -b 96k

# 常用参数
python main.py "D:\Music" -b 128k -ar 44100 -ac 1 -j 8 --keep-tree --keep-unicode
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `input_dirs` | 输入目录（可多个）或单个文件，目录递归扫描 | 必填 |
| `-o, --output` | 输出目录 | 第一个输入目录下 `compressed_huawei` |
| `-b, --bitrate` | MP3 码率，华为建议 128k-192k | `128k` |
| `-ar, --sample-rate` | 采样率 | `44100` |
| `-ac, --channels` | 声道数（1 单声道体积更小） | `1` |
| `-j, --jobs` | 并行任务数 | CPU 核数 |
| `--keep-tree` | 保持原目录结构（默认平铺） | 关 |
| `--overwrite` | 覆盖已存在文件（默认重名加序号） | 关 |
| `--keep-unicode` | 文件名保留中文 | 关 |
| `--exclude-dir` | 排除目录名关键词，逗号分隔 | 无 |
| `--include` | 文件名需包含的关键词，逗号分隔 | 无 |
| `--exclude` | 文件名包含即排除的关键词，逗号分隔 | 无 |
| `--ext` | 输入扩展名 | 支持的全部格式 |

## 导入华为手表

1. 把输出目录里的 mp3 复制到手机内部存储的 `Music` 文件夹
2. 打开华为运动健康 App → 设备 → 音乐 → 添加音乐/管理音乐
3. 默认文件名已处理为英文数字下划线，避免中文和特殊符号导致导入失败

## 依赖

- Python 3.9+（CLI）；GUI 额外需要 `pip install PySide6`
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) 在 PATH 中，或放在 exe 同目录

## 打包 exe

本仓库自带 GitHub Actions（[.github/workflows/build.yml](.github/workflows/build.yml)）：

- 每次推送 / PR 都会自动构建，产物 zip 在该次运行的 Artifacts 里可下载
- 推送 `v*` 标签（如 `git tag v1.0.0 && git push --tags`）时会额外创建 GitHub Release 并附上 zip
- 也可在 Actions 页面手动触发（workflow_dispatch）

本地打包：

```bash
pip install pyinstaller pyside6
pyinstaller --noconfirm --clean --onefile --windowed --name MP3Compressor gui.py
# 把 ffmpeg.exe 复制到 dist/MP3Compressor.exe 旁边即可脱离 PATH 运行
```
