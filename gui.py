"""华为手表 MP3 批量压缩工具（PySide6 GUI）。运行：python gui.py"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from core import (
    SUPPORTED_EXTS,
    JobOptions,
    ScanOptions,
    check_ffmpeg,
    compress_all,
    plan_tasks,
    scan_files,
)

BITRATES = ['64k', '96k', '112k', '128k', '160k', '192k']
SAMPLE_RATES = ['44100', '32000', '22050', '16000']


def parse_keywords(text: str) -> list:
    return [k.strip() for k in text.split(',') if k.strip()]


def fmt_mb(n: int) -> str:
    return f'{n / 1048576:.2f} MB'


class Worker(QThread):
    """后台工作线程：依次执行任务队列中的每个预设。"""

    log = Signal(str)
    progress = Signal(int, int, int, int)   # done, total, done_bytes, total_bytes
    stage = Signal(str)                     # 当前阶段描述
    job_done = Signal(dict)                 # 单个预设汇总
    all_done = Signal(dict)                 # 全部完成汇总

    def __init__(self, presets: list):
        super().__init__()
        self.presets = presets              # list[dict]
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        grand = {'ok': 0, 'skip': 0, 'fail': 0, 'total_src': 0, 'total_dst': 0,
                 'elapsed': 0.0, 'cancelled': False, 'jobs': 0}

        for i, preset in enumerate(self.presets, 1):
            if self._cancel:
                grand['cancelled'] = True
                break

            name = preset['name']
            self.stage.emit(f'[{i}/{len(self.presets)}] {name}：扫描中...')

            scan_opt = ScanOptions(
                exts=set(preset['exts']),
                exclude_dir_keywords=preset['exclude_dirs'],
                include_keywords=preset['include_kw'],
                exclude_keywords=preset['exclude_kw'],
            )
            roots = [Path(p) for p in preset['roots']]
            files = scan_files(roots, scan_opt)

            if not files:
                self.log.emit(f'[{name}] 没有找到可处理的音频文件。')
                self.job_done.emit({'name': name, 'ok': 0, 'skip': 0, 'fail': 0,
                                    'total': 0, 'elapsed': 0})
                continue

            total_bytes = sum(
                f.stat().st_size for f in files
            )
            self.log.emit(f'[{name}] 找到 {len(files)} 个文件，共 {fmt_mb(total_bytes)}')

            opts = JobOptions(
                output_dir=Path(preset['output']),
                bitrate=preset['bitrate'],
                sample_rate=preset['sample_rate'],
                channels=preset['channels'],
                jobs=preset['jobs'],
                keep_tree=preset['keep_tree'],
                overwrite=preset['overwrite'],
                keep_unicode=preset['keep_unicode'],
            )
            tasks = plan_tasks(files, roots, opts)

            def cb(done, total, done_bytes, total_bytes_, msg, name=name):
                self.progress.emit(done, total, done_bytes, total_bytes_)
                self.log.emit(f'[{name}] {msg}')

            self.stage.emit(f'[{i}/{len(self.presets)}] {name}：压缩中...')
            summary = compress_all(tasks, opts, progress_cb=cb,
                                   cancel_event=self)

            self.log.emit(
                f"[{name}] 本任务完成：成功 {summary['ok']}，跳过 {summary['skip']}，"
                f"失败 {summary['fail']}，耗时 {summary['elapsed']:.1f}s"
            )
            self.job_done.emit({'name': name, **summary})

            grand['ok'] += summary['ok']
            grand['skip'] += summary['skip']
            grand['fail'] += summary['fail']
            grand['total_src'] += summary['total_src']
            grand['total_dst'] += summary['total_dst']
            grand['elapsed'] += summary['elapsed']
            grand['jobs'] += 1
            if summary['cancelled']:
                grand['cancelled'] = True
                break

        self.all_done.emit(grand)

    # compress_all 通过 is_set() 检查取消，兼容 threading.Event 接口
    def is_set(self) -> bool:
        return self._cancel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('华为手表 MP3 批量压缩工具')
        self.resize(880, 720)
        self.worker = None
        self.queue = []                     # list[dict]

        self._build_ui()
        self._check_ffmpeg()

    # ---------- UI 构建 ----------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        # 输入列表
        grp_in = QGroupBox('输入（目录 / 文件，可添加多个）')
        v = QVBoxLayout(grp_in)
        self.lst_inputs = QListWidget()
        self.lst_inputs.setSelectionMode(QListWidget.ExtendedSelection)
        v.addWidget(self.lst_inputs)

        h = QHBoxLayout()
        b = QPushButton('添加文件夹'); b.clicked.connect(self._add_folder)
        h.addWidget(b)
        b = QPushButton('添加文件'); b.clicked.connect(self._add_files)
        h.addWidget(b)
        b = QPushButton('移除选中'); b.clicked.connect(self._remove_selected)
        h.addWidget(b)
        b = QPushButton('清空'); b.clicked.connect(self.lst_inputs.clear)
        h.addWidget(b)
        h.addStretch()
        v.addLayout(h)
        layout.addWidget(grp_in)

        # 输出 + 参数
        grp_cfg = QGroupBox('输出与参数')
        form = QFormLayout(grp_cfg)

        h = QHBoxLayout()
        self.edt_output = QLineEdit()
        self.edt_output.setPlaceholderText('默认：第一个输入目录/compressed_huawei')
        h.addWidget(self.edt_output)
        b = QPushButton('浏览...')
        b.clicked.connect(self._pick_output)
        h.addWidget(b)
        form.addRow('输出目录：', h)

        h = QHBoxLayout()
        self.cmb_bitrate = QComboBox(); self.cmb_bitrate.addItems(BITRATES)
        self.cmb_bitrate.setCurrentText('128k')
        h.addWidget(self.cmb_bitrate)
        h.addWidget(QLabel('采样率：'))
        self.cmb_rate = QComboBox(); self.cmb_rate.addItems(SAMPLE_RATES)
        h.addWidget(self.cmb_rate)
        h.addWidget(QLabel('声道：'))
        self.cmb_channels = QComboBox(); self.cmb_channels.addItems(['1 (单声道，更小)', '2 (立体声)'])
        h.addWidget(self.cmb_channels)
        h.addWidget(QLabel('并行数：'))
        self.spn_jobs = QSpinBox()
        self.spn_jobs.setRange(1, (os.cpu_count() or 4) * 2)
        self.spn_jobs.setValue(os.cpu_count() or 4)
        h.addWidget(self.spn_jobs)
        h.addStretch()
        form.addRow('码率等：', h)

        h = QHBoxLayout()
        self.chk_keep_tree = QCheckBox('保持目录结构')
        self.chk_overwrite = QCheckBox('覆盖已存在文件')
        self.chk_unicode = QCheckBox('文件名保留中文')
        for c in (self.chk_keep_tree, self.chk_overwrite, self.chk_unicode):
            h.addWidget(c)
        h.addStretch()
        form.addRow('选项：', h)
        layout.addWidget(grp_cfg)

        # 过滤
        grp_filter = QGroupBox('过滤（关键词不区分大小写，多个用英文逗号分隔）')
        form = QFormLayout(grp_filter)
        self.edt_exclude_dirs = QLineEdit(); self.edt_exclude_dirs.setPlaceholderText('如：temp,cover,cached')
        self.edt_include = QLineEdit(); self.edt_include.setPlaceholderText('留空=全部；如：周杰伦')
        self.edt_exclude = QLineEdit(); self.edt_exclude.setPlaceholderText('如：伴奏,demo')
        form.addRow('排除目录名：', self.edt_exclude_dirs)
        form.addRow('只要文件含：', self.edt_include)
        form.addRow('排除文件含：', self.edt_exclude)
        layout.addWidget(grp_filter)

        # 任务队列
        grp_queue = QGroupBox('任务队列（把不同参数组合依次批量执行）')
        v = QVBoxLayout(grp_queue)
        self.lst_queue = QListWidget()
        v.addWidget(self.lst_queue)

        h = QHBoxLayout()
        b = QPushButton('➕ 把当前设置存为任务'); b.clicked.connect(self._queue_add)
        h.addWidget(b)
        b = QPushButton('移除选中'); b.clicked.connect(self._queue_remove)
        h.addWidget(b)
        b = QPushButton('清空队列'); b.clicked.connect(self._queue_clear)
        h.addWidget(b)
        h.addStretch()
        v.addLayout(h)
        layout.addWidget(grp_queue)

        # 进度 + 日志
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.lbl_stage = QLabel('就绪')
        layout.addWidget(self.lbl_stage)
        layout.addWidget(self.bar)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        layout.addWidget(self.txt_log, 1)

        # 底部按钮
        h = QHBoxLayout()
        self.btn_scan = QPushButton('🔍 仅扫描（统计文件数）')
        self.btn_scan.clicked.connect(self._scan_only)
        h.addWidget(self.btn_scan)
        self.btn_start = QPushButton('▶ 开始压缩（当前设置）')
        self.btn_start.clicked.connect(self._start_current)
        h.addWidget(self.btn_start)
        self.btn_run_queue = QPushButton('▶▶ 运行队列')
        self.btn_run_queue.clicked.connect(self._start_queue)
        h.addWidget(self.btn_run_queue)
        self.btn_cancel = QPushButton('⏹ 取消')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel)
        h.addWidget(self.btn_cancel)
        layout.addLayout(h)

        self.setCentralWidget(central)

    # ---------- 辅助 ----------

    def _check_ffmpeg(self):
        if not check_ffmpeg():
            QMessageBox.critical(
                self, '缺少 ffmpeg',
                '未找到 ffmpeg。\n请安装 ffmpeg 并加入 PATH 后重启程序。'
            )

    def _collect_roots(self) -> list:
        return [self.lst_inputs.item(i).text() for i in range(self.lst_inputs.count())]

    def _current_preset(self, name: str) -> dict:
        roots = self._collect_roots()
        if not roots:
            QMessageBox.warning(self, '提示', '请先添加输入目录或文件。')
            return None

        output = self.edt_output.text().strip()
        if not output:
            output = str(Path(roots[0]) / 'compressed_huawei' if Path(roots[0]).is_dir()
                         else Path(roots[0]).parent / 'compressed_huawei')

        return {
            'name': name,
            'roots': roots,
            'output': output,
            'bitrate': self.cmb_bitrate.currentText(),
            'sample_rate': int(self.cmb_rate.currentText()),
            'channels': self.cmb_channels.currentIndex() + 1,
            'jobs': self.spn_jobs.value(),
            'keep_tree': self.chk_keep_tree.isChecked(),
            'overwrite': self.chk_overwrite.isChecked(),
            'keep_unicode': self.chk_unicode.isChecked(),
            'exclude_dirs': parse_keywords(self.edt_exclude_dirs.text()),
            'include_kw': parse_keywords(self.edt_include.text()),
            'exclude_kw': parse_keywords(self.edt_exclude.text()),
            'exts': sorted(SUPPORTED_EXTS),
        }

    def _scan_options(self, preset: dict) -> ScanOptions:
        return ScanOptions(
            exts=set(preset['exts']),
            exclude_dir_keywords=preset['exclude_dirs'],
            include_keywords=preset['include_kw'],
            exclude_keywords=preset['exclude_kw'],
        )

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_run_queue.setEnabled(not running)
        self.btn_scan.setEnabled(not running)
        self.btn_cancel.setEnabled(running)

    # ---------- 输入列表 ----------

    def _add_folder(self):
        d = QFileDialog.getExistingDirectory(self, '选择文件夹')
        if d:
            self.lst_inputs.addItem(d)

    def _add_files(self):
        exts = ' '.join(f'*{e}' for e in sorted(SUPPORTED_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, '选择音频文件', '', f'音频文件 ({exts})'
        )
        for f in files:
            self.lst_inputs.addItem(f)

    def _remove_selected(self):
        for item in self.lst_inputs.selectedItems():
            self.lst_inputs.takeItem(self.lst_inputs.row(item))

    def _pick_output(self):
        d = QFileDialog.getExistingDirectory(self, '选择输出目录')
        if d:
            self.edt_output.setText(d)

    # ---------- 队列 ----------

    def _queue_add(self):
        name, ok = QInputDialog.getText(self, '任务名称', '给这组设置起个名字：')
        if not ok or not name.strip():
            return
        preset = self._current_preset(name.strip())
        if preset is None:
            return
        self.queue.append(preset)
        self.lst_queue.addItem(
            f"{preset['name']}  |  {preset['bitrate']} {preset['sample_rate']}Hz "
            f"ch{preset['channels']} x{preset['jobs']}  输出: {preset['output']}"
        )

    def _queue_remove(self):
        for item in self.lst_queue.selectedItems():
            row = self.lst_queue.row(item)
            self.lst_queue.takeItem(row)
            del self.queue[row]

    def _queue_clear(self):
        self.lst_queue.clear()
        self.queue.clear()

    # ---------- 扫描 / 运行 ----------

    def _scan_only(self):
        preset = self._current_preset('扫描')
        if preset is None:
            return
        files = scan_files([Path(p) for p in preset['roots']], self._scan_options(preset))
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        QMessageBox.information(
            self, '扫描结果',
            f'找到 {len(files)} 个音频文件\n总计 {fmt_mb(total)}'
        )

    def _start_current(self):
        preset = self._current_preset('当前设置')
        if preset is None:
            return
        self._run([preset])

    def _start_queue(self):
        if not self.queue:
            QMessageBox.warning(self, '提示', '队列为空，请先"把当前设置存为任务"。')
            return
        self._run(list(self.queue))

    def _run(self, presets: list):
        if self.worker is not None and self.worker.isRunning():
            return
        self.txt_log.appendPlainText(f'===== 开始执行 {len(presets)} 个任务 =====')
        self.bar.setValue(0)
        self._set_running(True)

        self.worker = Worker(presets)
        self.worker.log.connect(self.txt_log.appendPlainText)
        self.worker.progress.connect(self._on_progress)
        self.worker.stage.connect(self.lbl_stage.setText)
        self.worker.job_done.connect(self._on_job_done)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.start()

    def _on_progress(self, done, total, done_bytes, total_bytes):
        pct = int(done_bytes * 100 / total_bytes) if total_bytes else 0
        self.bar.setValue(pct)
        self.lbl_stage.setText(f'进度：{done}/{total} 个文件，{pct}%')

    def _on_job_done(self, summary: dict):
        self.txt_log.appendPlainText(
            f"✔ 任务 [{summary['name']}] 完成：成功 {summary['ok']}，"
            f"跳过 {summary['skip']}，失败 {summary['fail']}"
        )

    def _on_all_done(self, grand: dict):
        self._set_running(False)
        self.lbl_stage.setText('完成' + ('（已取消）' if grand['cancelled'] else ''))
        saved = (1 - grand['total_dst'] / grand['total_src']) * 100 if grand['total_src'] else 0
        self.txt_log.appendPlainText(
            f"\n===== 全部完成 =====\n"
            f"成功 {grand['ok']}，跳过 {grand['skip']}，失败 {grand['fail']}\n"
            f"原始 {fmt_mb(grand['total_src'])} -> 输出 {fmt_mb(grand['total_dst'])}"
            f"（节省 {saved:.1f}%）\n耗时 {grand['elapsed']:.1f}s"
        )
        QMessageBox.information(
            self, '完成',
            f"成功 {grand['ok']}，跳过 {grand['skip']}，失败 {grand['fail']}\n"
            f"节省 {saved:.1f}%，耗时 {grand['elapsed']:.1f}s"
        )

    def _cancel(self):
        if self.worker is not None:
            self.worker.cancel()
            self.lbl_stage.setText('正在取消（等待当前 ffmpeg 结束）...')
            self.btn_cancel.setEnabled(False)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
