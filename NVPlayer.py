import sys
import os
import json
import random
import traceback
import sqlite3
import hashlib
import time
from collections import OrderedDict
import concurrent.futures
from PyQt5.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QPushButton,
    QFileDialog,
    QTableWidget,
    QTableWidgetItem,
    QLabel,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QAction,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QGroupBox,
    QSlider,
    QLineEdit,
    QAbstractItemView,
    QMessageBox,
    QTextEdit,
    QTabWidget,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsRectItem,
    QMenuBar,
    QDialog,
    QComboBox,
    QVBoxLayout as QDialogVBoxLayout,
    QTextBrowser,
    QDialogButtonBox,
)
from PyQt5.QtGui import (
    QPixmap,
    QFont,
    QColor,
    QPainter,
    QPen,
    QPainterPath,
    QIcon,
    QFontDatabase,
)
from PyQt5.QtCore import Qt, QTimer, QSize, QRectF, QObject, pyqtSignal, QThread
import pygame
from mutagen import File
from mutagen.flac import FLAC
import locale

try:
    import soundfile as sf
    import sounddevice as sd
    import numpy as np

    SOUNDDEVICE_AVAILABLE = True
except Exception:
    SOUNDDEVICE_AVAILABLE = False

import threading
import math


def read_json_file(path, default=None):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return default
    return default


def write_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def merge_json_file(path, updates):
    try:
        existing = read_json_file(path, {}) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing.update(updates or {})
        return write_json_file(path, existing)
    except Exception:
        return False


class ScanWorker(QObject):
    """파일 스캔 및 DB 업데이트를 백그라운드에서 수행"""

    finished = pyqtSignal(int, int)  # (added_count, deleted_count)
    progress = pyqtSignal(str)
    batch = pyqtSignal(int, int)  # (added_so_far, total_new)
    error = pyqtSignal(str)

    def __init__(self, root_folder, db_path):
        super().__init__()
        self.root_folder = root_folder
        self.db_path = db_path
        self.supported_formats = [".flac", ".mp3", ".ogg", ".wav"]
        self.progress_throttle = 0.2  # seconds between progress emits

    def run(self):
        conn = None
        added_count = 0
        deleted_paths = set()
        try:
            # Use WAL mode for better concurrency and set a longer timeout
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass

            # 1. DB에 있는 모든 파일 경로 가져오기
            cursor.execute("SELECT path FROM tracks")
            db_paths = set(row[0] for row in cursor.fetchall())

            # 2. 파일 시스템을 순회하며 DB에 없는 파일만 수집 (메모리 절약)
            seen_paths = set()
            new_paths = []
            for dirpath, _, filenames in os.walk(self.root_folder):
                for filename in filenames:
                    if any(
                        filename.lower().endswith(fmt) for fmt in self.supported_formats
                    ):
                        full_path = os.path.join(dirpath, filename)
                        seen_paths.add(full_path)
                        if full_path not in db_paths:
                            new_paths.append(full_path)

            # 삭제된 파일 경로(이전에 DB에 있었으나 현재 파일시스템에서 보이지 않는 것)
            deleted_paths = db_paths - seen_paths

            # 4. 삭제된 파일 DB에서 제거
            if deleted_paths:
                self.progress.emit(f"{len(deleted_paths)}개 파일 삭제 중...")
                try:
                    cursor.executemany(
                        "DELETE FROM tracks WHERE path=?",
                        [(path,) for path in deleted_paths],
                    )
                    # also remove from FTS table if present
                    try:
                        cursor.executemany(
                            "DELETE FROM tracks_fts WHERE path=?",
                            [(path,) for path in deleted_paths],
                        )
                    except Exception:
                        pass
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            # 5. 새로 추가된 파일 메타데이터 병렬 추출 후 DB에 일괄 삽입
            total_new = len(new_paths)
            last_emit = 0
            track_results = []
            if new_paths:
                max_workers = min(8, (os.cpu_count() or 4))
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers
                ) as ex:
                    futures = {
                        ex.submit(self._extract_metadata, p): p for p in new_paths
                    }
                    processed = 0
                    for fut in concurrent.futures.as_completed(futures):
                        processed += 1
                        try:
                            track_info = fut.result()
                        except Exception:
                            track_info = None
                        if track_info:
                            track_results.append(track_info)
                        # throttled progress update
                        now = time.time()
                        if now - last_emit >= self.progress_throttle:
                            self.progress.emit(
                                f"새 파일 스캔 중 ({min(processed, total_new)}/{total_new})..."
                            )
                            last_emit = now

                # insert results into DB in chunks with retry to avoid sqlite locked errors
                chunk_size = 50
                inserted_so_far = 0
                insert_values = [
                    (
                        t.get("path"),
                        t.get("artist"),
                        t.get("date"),
                        t.get("album"),
                        t.get("title"),
                        t.get("track"),
                        t.get("duration"),
                        t.get("raw_duration"),
                        t.get("lyrics"),
                    )
                    for t in track_results
                ]

                for i in range(0, len(insert_values), chunk_size):
                    chunk = insert_values[i : i + chunk_size]
                    attempts = 0
                    success = False
                    while attempts < 4 and not success:
                        try:
                            cursor.executemany(
                                "INSERT OR IGNORE INTO tracks (path, artist, date, album, title, track, duration, raw_duration, lyrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                chunk,
                            )
                            conn.commit()
                            success = True
                            inserted_so_far += len(chunk)
                            added_count += len(chunk)
                            # emit batch progress for UI partial update
                            try:
                                self.batch.emit(inserted_so_far, total_new)
                            except Exception:
                                pass
                            # also update FTS table for this chunk if available
                            try:
                                fts_vals = [(c[0], c[1], c[3], c[4]) for c in chunk]
                                cursor.executemany(
                                    "INSERT OR REPLACE INTO tracks_fts (path, artist, album, title) VALUES (?, ?, ?, ?)",
                                    fts_vals,
                                )
                                conn.commit()
                            except Exception:
                                # ignore if FTS not supported
                                pass
                        except sqlite3.OperationalError:
                            attempts += 1
                            time.sleep(0.1 * attempts)
                        except Exception:
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            attempts = 4
                    if not success:
                        # log but continue
                        self.progress.emit(
                            f"일부 파일 추가 중 오류 발생 (스킵): {i}-{i+len(chunk)}"
                        )

        except Exception as e:
            tb = traceback.format_exc()
            self.error.emit(f"파일 처리 중 오류 발생:\n{e}\n\n{tb}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            # ensure finished emits even on errors so UI can re-enable controls
            try:
                self.finished.emit(added_count, len(deleted_paths))
            except Exception:
                pass

    def _extract_metadata(self, file_path):
        """단일 파일에서 메타데이터를 추출하여 dict로 반환"""
        try:
            audio = File(file_path)
            if audio is None:
                return None

            tags_obj = {}
            if hasattr(audio, "tags") and audio.tags:
                try:
                    for k in audio.tags.keys():
                        tags_obj[k] = audio.tags.get(k)
                except Exception:
                    try:
                        tags_obj = dict(audio.tags)
                    except Exception:
                        tags_obj = {}

            def pick(keys):
                for k in keys:
                    if k in tags_obj:
                        v = tags_obj[k]
                        try:
                            if isinstance(v, (list, tuple)):
                                return str(v[0])
                            if hasattr(v, "text"):
                                return str(v.text[0]) if v.text else str(v)
                            return str(v)
                        except Exception:
                            return str(v)
                return None

            artist = (
                pick(["artist", "ARTIST", "TPE1", "albumartist", "ALBUMARTIST"])
                or "Unknown Artist"
            )
            album = pick(["album", "ALBUM", "TALB"]) or "Unknown Album"
            title = pick(["title", "TITLE", "TIT2"]) or os.path.basename(file_path)
            date = pick(["date", "DATE", "TDRC", "TYER", "year"]) or "0000"
            tracknumber_raw = pick(["tracknumber", "TRCK", "track"]) or "0"
            lyrics = pick(["lyrics", "USLT::", "USLT"]) or ""
            raw_duration = audio.info.length if hasattr(audio.info, "length") else 0
            duration = self._format_duration(raw_duration)

            tracknumber = str(tracknumber_raw).split("/")[0].strip()
            if not tracknumber.isdigit():
                tracknumber = "0"

            return {
                "path": file_path,
                "artist": str(artist),
                "date": str(date).split("-")[0],
                "album": str(album),
                "title": str(title),
                "track": tracknumber,
                "duration": duration,
                "raw_duration": raw_duration,
                "lyrics": lyrics,
            }
        except Exception:
            return None

    @staticmethod
    def _format_duration(seconds):
        if seconds is None or seconds == 0:
            return "--:--"
        minutes, seconds = int(seconds // 60), int(seconds % 60)
        return f"{minutes:02d}:{seconds:02d}"


class ClickableLabel(QLabel):
    """클릭 이벤트를 발생시키는 QLabel"""

    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SoundDevicePlayer:
    """Simple player using soundfile + sounddevice for native FLAC playback."""

    def __init__(self):
        self.file = None
        self.stream = None
        self.lock = threading.Lock()
        self.volume = 1.0
        self.paused = False
        self.samplerate = None
        self.channels = None
        self.frames_read = 0
        self.length_frames = 0
        # 10-band EQ default (dB): [31.25, 62.5, 125, 250, 500, 1k, 2k, 4k, 8k, 16k]
        self.eq_gains = [0] * 10

    def load(self, path):
        self.stop()
        self.file = sf.SoundFile(path)
        self.samplerate = self.file.samplerate
        self.channels = self.file.channels
        try:
            self.length_frames = len(self.file)
        except Exception:
            self.length_frames = 0
        self.frames_read = 0

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if self.file is None:
                outdata.fill(0)
                return
            data = self.file.read(frames, dtype="float32", always_2d=True)
            if data.shape[0] == 0:
                outdata.fill(0)
                raise sd.CallbackStop()
            # apply equalizer before volume if configured
            if data.shape[0] > 0 and any(g != 0 for g in self.eq_gains):
                try:
                    data = self.apply_eq(data)
                except Exception:
                    pass
            if data.shape[0] < frames:
                out = np.zeros((frames, self.channels), dtype="float32")
                out[: data.shape[0], :] = data
                outdata[:] = out * self.volume
                self.frames_read += data.shape[0]
                raise sd.CallbackStop()
            else:
                outdata[:] = data * self.volume
                self.frames_read += data.shape[0]

    def play(self, start=0.0):
        if self.file is None:
            return
        try:
            start_frame = int(start * self.samplerate)
            self.file.seek(start_frame)
            self.frames_read = start_frame
        except Exception:
            pass
        self.paused = False
        self.stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            callback=self._callback,
            dtype="float32",
        )
        self.stream.start()
        print(
            f"SoundDevicePlayer: started playback (samplerate={self.samplerate}, channels={self.channels})"
        )

    def pause(self):
        if self.stream and not self.paused:
            try:
                self.stream.stop()
                self.paused = True
            except Exception:
                pass

    def resume(self):
        if self.stream and self.paused:
            try:
                self.stream.start()
                self.paused = False
            except Exception:
                pass

    def stop(self):
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.file:
            try:
                self.file.close()
            except Exception:
                pass
            self.file = None
        self.frames_read = 0

    def set_volume(self, vol):
        try:
            self.volume = float(vol)
        except Exception:
            pass

    def set_eq_gains(self, gains):
        """gains: list of ten dB values"""
        try:
            if gains is None:
                self.eq_gains = [0] * 10
            else:
                self.eq_gains = list(gains)
        except Exception:
            pass

    def apply_eq(self, data):
        """Apply simple graphic EQ to numpy array of samples.
        data shape: (frames, channels) float32
        """
        # compute frequency bins for current block length
        n = data.shape[0]
        freqs = np.fft.rfftfreq(n, 1.0 / self.samplerate)

        # 10-band centers (Hz)
        centers = [31.25, 62.5, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        # compute edges as geometric mean between centers, with 0 and Nyquist bounds
        edges = [0.0]
        for i in range(len(centers) - 1):
            edges.append(math.sqrt(centers[i] * centers[i + 1]))
        edges.append(self.samplerate / 2.0)

        gains = np.ones_like(freqs)
        for idx, g_db in enumerate(self.eq_gains):
            if idx >= len(centers):
                break
            try:
                if g_db == 0:
                    continue
                lin = 10 ** (g_db / 20.0)
                low = edges[idx]
                high = edges[idx + 1]
                mask = (freqs >= low) & (freqs < high)
                gains[mask] *= lin
            except Exception:
                continue

        # apply gains in frequency domain
        data_fft = np.fft.rfft(data, axis=0)
        data_fft *= gains[:, None]
        data = np.fft.irfft(data_fft, n=n, axis=0)
        return data

    def get_pos_ms(self):
        if not self.samplerate or self.samplerate == 0:
            return 0
        return int((self.frames_read / float(self.samplerate)) * 1000)

    def get_length_ms(self):
        if not self.samplerate or self.samplerate == 0:
            return 0
        return int((self.length_frames / float(self.samplerate)) * 1000)

    def is_busy(self):
        return self.stream is not None and not self.paused


class AudioController:
    """Encapsulate audio backend initialization and volume handling.

    This keeps pygame/sounddevice setup out of the main UI class so
    audio concerns are easier to test and maintain.
    """

    def __init__(self, parent=None, eq_gains=None):
        self.parent = parent
        self.pygame_initialized = False
        self._mixer_settings = None
        self.use_sounddevice = SOUNDDEVICE_AVAILABLE
        self.audio_player = None
        # attempt to init pygame mixer (best-effort)
        try:
            pygame.mixer.init()
            self.pygame_initialized = bool(pygame.mixer.get_init())
            try:
                self._mixer_settings = pygame.mixer.get_init()
            except Exception:
                self._mixer_settings = None
            if not self.pygame_initialized and parent is not None:
                try:
                    QMessageBox.critical(parent, "오류", "Pygame Mixer 초기화 실패.")
                except Exception:
                    pass
        except Exception:
            self.pygame_initialized = False
            if parent is not None:
                try:
                    QMessageBox.critical(parent, "오류", "Pygame Mixer 초기화 불가.")
                except Exception:
                    pass

        # sounddevice backend (preferred for native FLAC playback)
        if self.use_sounddevice:
            try:
                self.audio_player = SoundDevicePlayer()
                if eq_gains:
                    try:
                        self.audio_player.set_eq_gains(eq_gains)
                    except Exception:
                        pass
            except Exception:
                self.use_sounddevice = False
                self.audio_player = None
        else:
            self.audio_player = None

    def apply_volume_to_backend(self, value_float):
        # apply volume to whichever backend is active
        if getattr(self, "use_sounddevice", False) and getattr(
            self, "audio_player", None
        ):
            try:
                self.audio_player.set_volume(value_float)
            except Exception:
                pass

        if getattr(self, "pygame_initialized", False):
            try:
                try:
                    pygame.mixer.music.set_volume(value_float)
                except Exception:
                    try:
                        for ch in range(pygame.mixer.get_num_channels()):
                            ch_obj = pygame.mixer.Channel(ch)
                            try:
                                ch_obj.set_volume(value_float)
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception:
                pass


class LibraryManager:
    """Handles database initialization and cache maintenance.

    Separated from the UI class so DB and scanning responsibilities
    are centralized for easier testing and future refactor.
    """

    def __init__(self, db_path, cache_path, fts=True):
        self.db_path = db_path
        self.cache_path = cache_path
        self.fts = fts
        # ensure cache path exists
        try:
            if not os.path.exists(self.cache_path):
                os.makedirs(self.cache_path)
        except Exception:
            pass
        # initialize DB and cleanup cache
        self.init_database()

    def init_database(self):
        """Create tracks table and optional FTS index."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                """
        CREATE TABLE IF NOT EXISTS tracks (
            path TEXT PRIMARY KEY,
            artist TEXT,
            date TEXT,
            album TEXT,
            title TEXT,
            track TEXT,
            duration TEXT,
            raw_duration REAL,
            lyrics TEXT
        )"""
            )
            # create an FTS5 virtual table for fast full-text search if supported
            try:
                cursor.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(path, artist, album, title, content='')"
                )
                # populate FTS from existing tracks if empty
                cursor.execute("SELECT count(*) FROM tracks_fts")
                cnt = cursor.fetchone()[0]
                if cnt == 0:
                    cursor.execute(
                        "INSERT INTO tracks_fts (rowid, path, artist, album, title) SELECT NULL, path, artist, album, title FROM tracks"
                    )
            except Exception:
                # FTS5 may not be available; ignore and fallback to LIKE searches
                pass
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # cleanup old caches
        self.cleanup_old_cache_files()

    def cleanup_old_cache_files(self, days=30):
        """Remove cache files older than `days` days."""
        try:
            current_time = time.time()
            cutoff_time = current_time - (days * 24 * 3600)
            if not os.path.exists(self.cache_path):
                return
            for filename in os.listdir(self.cache_path):
                file_path = os.path.join(self.cache_path, filename)
                if os.path.isfile(file_path):
                    try:
                        file_mtime = os.path.getmtime(file_path)
                        if file_mtime < cutoff_time:
                            try:
                                os.remove(file_path)
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass

    def start_scan(self, ui_obj, folder_path):
        """Start background scan using ScanWorker.

        ui_obj should have methods:
        - on_scan_finished
        - update_progress_label
        - on_scan_batch
        - on_scan_error
        and attributes open_folder_button, label.
        """
        # guard against duplicate scans
        if (
            hasattr(self, "scan_thread")
            and self.scan_thread
            and self.scan_thread.isRunning()
        ):
            try:
                QMessageBox.warning(
                    ui_obj, "스캔 중", "이미 라이브러리 스캔이 진행 중입니다."
                )
            except Exception:
                pass
            return
        try:
            ui_obj.open_folder_button.setEnabled(False)
            ui_obj.label.setText("음악 라이브러리 업데이트 중...")
        except Exception:
            pass

        self.scan_thread = QThread()
        self.scan_worker = ScanWorker(folder_path, self.db_path)
        self.scan_worker.moveToThread(self.scan_thread)

        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.finished.connect(ui_obj.on_scan_finished)
        self.scan_worker.progress.connect(ui_obj.update_progress_label)
        self.scan_worker.batch.connect(ui_obj.on_scan_batch)
        self.scan_worker.error.connect(ui_obj.on_scan_error)

        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.finished.connect(lambda: setattr(self, "scan_thread", None))
        self.scan_thread.finished.connect(lambda: setattr(self, "scan_worker", None))
        self.scan_thread.start()


class UIManager:
    """Builds and wires the UI for `MetadataMusicPlayer`.

    Keeps layout and widget creation outside the main class to reduce
    responsibilities of the UI controller.
    """

    def __init__(self):
        self.parent = None

    def setup_ui(self, parent):
        p = parent
        self.parent = p
        overall_vbox = QVBoxLayout(p)
        overall_vbox.setContentsMargins(0, 0, 0, 0)
        overall_vbox.setSpacing(0)

        p.setup_menubar()
        overall_vbox.addLayout(p.main_layout_for_menubar)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(10, 0, 10, 10)
        main_layout.setSpacing(10)
        overall_vbox.addLayout(main_layout)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        p.tab_widget = QTabWidget()
        p.apply_font(p.tab_widget, "SemiBold")
        p.artists_page = QWidget()
        p.all_tracks_page = QWidget()
        p.history_page = QWidget()
        p.tab_widget.addTab(p.artists_page, "아티스트")
        p.tab_widget.addTab(p.all_tracks_page, "전체 트랙")
        p.tab_widget.addTab(p.history_page, "재생 기록")
        p.setup_artists_page()
        p.setup_all_tracks_page()
        p.setup_history_page()

        folder_layout = QHBoxLayout()
        p.label = QLabel("음악 폴더를 선택하세요.", p)
        p.apply_font(p.label, "Medium")
        p.open_folder_button = QPushButton("음악 폴더 열기", p)
        p.apply_font(p.open_folder_button, "SemiBold")
        folder_layout.addWidget(p.label)
        folder_layout.addWidget(p.open_folder_button)
        left_layout.addLayout(folder_layout)
        left_layout.addWidget(p.tab_widget)

        right_panel_top_layout = QVBoxLayout()
        playlist_box = QGroupBox("")  # 재생목록
        p.apply_font(playlist_box, "Bold")
        playlist_layout = QVBoxLayout()
        p.playlist_view = QTableWidget()
        p.playlist_view.setShowGrid(False)
        p.playlist_view.setAlternatingRowColors(True)
        p.playlist_view.setColumnCount(3)
        p.playlist_view.setHorizontalHeaderLabels(["제목", "아티스트", "길이"])
        p.playlist_view.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        p.apply_font(p.playlist_view.horizontalHeader(), "Bold")
        p.playlist_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        p.playlist_view.setEditTriggers(QTableWidget.NoEditTriggers)
        p.playlist_view.verticalHeader().setVisible(False)
        p.playlist_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        p.playlist_view.itemDoubleClicked.connect(p.play_music_from_playlist)
        p.playlist_view.setContextMenuPolicy(Qt.CustomContextMenu)
        p.playlist_view.customContextMenuRequested.connect(p.show_context_menu_playlist)
        playlist_layout.addWidget(p.playlist_view)
        playlist_box.setLayout(playlist_layout)

        lyrics_box = QGroupBox("")  # 가사
        p.apply_font(lyrics_box, "Bold")
        lyrics_layout = QVBoxLayout()
        p.lyrics_text = QTextEdit()
        p.lyrics_text.setReadOnly(True)
        p.apply_font(p.lyrics_text, "Medium")
        lyrics_layout.addWidget(p.lyrics_text)
        lyrics_box.setLayout(lyrics_layout)
        right_panel_top_layout.addWidget(playlist_box, stretch=2)
        right_panel_top_layout.addWidget(lyrics_box, stretch=1)

        player_control_box = QGroupBox("")  # 현재 재생 중
        p.apply_font(player_control_box, "Bold")
        player_control_layout = QVBoxLayout()
        p.cover_label = QLabel()
        p.cover_label.setAlignment(Qt.AlignCenter)
        p.cover_label.setFixedSize(200, 200)
        p.cover_label.setStyleSheet(
            "border: 1px solid gray; background-color: #F8F8FF; border-radius: 10px;"
        )
        p.cover_label.setText("앨범 커버 없음")
        p.now_playing_label = QLabel("재생 중인 곡이 없습니다.")
        p.now_playing_label.setAlignment(Qt.AlignCenter)
        p.now_playing_label.setWordWrap(True)
        p.apply_font(p.now_playing_label, "ExtraBold")
        p.now_playing_label.setStyleSheet("font-size: 15px; color: #111111;")
        p.audio_quality_label = QLabel("")
        p.audio_quality_label.setAlignment(Qt.AlignCenter)
        p.apply_font(p.audio_quality_label, "Medium")
        p.audio_quality_label.setStyleSheet("font-size: 11px; color: #555;")

        p.visualizer_view = QGraphicsView(p.visualizer_scene)
        p.visualizer_view.setFixedSize(200, 50)
        p.visualizer_view.setStyleSheet("border: none; background-color: transparent;")
        p.visualizer_view.setRenderHint(QPainter.Antialiasing)
        p.setup_visualizer()
        p.progress_slider = QSlider(Qt.Horizontal)
        p.progress_slider.setRange(0, 0)
        p.progress_slider.setEnabled(False)
        p.time_label = QLabel("00:00 / 00:00")
        p.time_label.setAlignment(Qt.AlignCenter)
        p.apply_font(p.time_label, "Medium")

        control_button_layout = QHBoxLayout()

        p.repeat_button = QPushButton()
        p.prev_button = QPushButton()
        p.play_pause_button = QPushButton()
        p.next_button = QPushButton()
        p.stop_button = QPushButton()
        p.shuffle_button = QPushButton()
        p.eq_button = QPushButton()
        p.mini_mode_button = QPushButton()

        p.play_pause_button.setIcon(p.play_icon)
        p.prev_button.setIcon(p.prev_icon)
        p.next_button.setIcon(p.next_icon)
        p.stop_button.setIcon(p.stop_icon)
        p.shuffle_button.setIcon(p.shuffle_icon)
        p.repeat_button.setIcon(p.repeat_icon)
        p.eq_button.setIcon(p.eq_icon)
        p.mini_mode_button.setIcon(p.mini_player_icon)

        p.repeat_button.setToolTip("반복 재생")
        p.shuffle_button.setToolTip("랜덤 재생")
        p.eq_button.setToolTip("이퀄라이저")
        p.mini_mode_button.setToolTip("미니 플레이어 모드")

        p.shuffle_button.setCheckable(True)

        icon_size = QSize(24, 24)
        for btn in [
            p.repeat_button,
            p.shuffle_button,
            p.prev_button,
            p.play_pause_button,
            p.next_button,
            p.stop_button,
            p.eq_button,
            p.mini_mode_button,
        ]:
            btn.setFixedSize(40, 40)
            btn.setIconSize(icon_size)
            btn.setFlat(True)

        control_button_layout.addStretch()
        for btn in [
            p.repeat_button,
            p.shuffle_button,
            p.prev_button,
            p.play_pause_button,
            p.next_button,
            p.stop_button,
            p.eq_button,
            p.mini_mode_button,
        ]:
            control_button_layout.addWidget(btn)
        control_button_layout.addStretch()

        volume_layout = QHBoxLayout()
        p.volume_icon = ClickableLabel()
        p.volume_icon.setPixmap(p.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio))
        p.volume_icon.setToolTip("음소거/음소거 해제")
        p.volume_slider = QSlider(Qt.Horizontal)
        p.volume_slider.setRange(0, 100)
        p.volume_label = QLabel("0%")
        p.volume_label.setFixedWidth(40)
        p.apply_font(p.volume_label, "Medium")
        volume_layout.addStretch(1)
        volume_layout.addWidget(p.volume_icon)
        volume_layout.addWidget(p.volume_slider, 1)
        volume_layout.addWidget(p.volume_label)
        volume_layout.addStretch(1)

        player_control_layout.addWidget(p.cover_label, alignment=Qt.AlignCenter)
        player_control_layout.addWidget(p.visualizer_view, alignment=Qt.AlignCenter)
        player_control_layout.addWidget(p.now_playing_label)
        player_control_layout.addWidget(p.audio_quality_label)
        player_control_layout.addWidget(p.progress_slider)
        player_control_layout.addWidget(p.time_label)
        player_control_layout.addLayout(control_button_layout)
        player_control_layout.addLayout(volume_layout)
        player_control_box.setLayout(player_control_layout)

        right_layout.addLayout(right_panel_top_layout, stretch=1)
        right_layout.addWidget(player_control_box)
        main_layout.addWidget(left_widget, stretch=2)
        main_layout.addWidget(right_widget, stretch=1)

        p.setLayout(overall_vbox)

        p.tab_widget.currentChanged.connect(p.on_tab_changed)
        p.open_folder_button.clicked.connect(p.open_folder)
        p.play_all_tracks_button.clicked.connect(p.play_all_tracks)
        p.play_pause_button.clicked.connect(p.play_pause_music)
        p.prev_button.clicked.connect(p.play_prev)
        p.next_button.clicked.connect(p.play_next)
        p.stop_button.clicked.connect(p.stop_music)
        p.shuffle_button.clicked.connect(p.toggle_shuffle)
        p.repeat_button.clicked.connect(p.toggle_repeat)
        p.eq_button.clicked.connect(p.show_eq_dialog)
        p.mini_mode_button.clicked.connect(p.toggle_mini_player)
        p.progress_slider.sliderReleased.connect(p.set_playback_position)
        p.progress_slider.sliderMoved.connect(p.update_time_label_on_move)
        p.volume_slider.valueChanged.connect(p.set_volume)
        p.volume_slider.valueChanged.connect(p.update_volume_label)
        p.volume_icon.clicked.connect(p.toggle_mute)


class AlbumIconWidget(QWidget):
    def __init__(
        self, album_cover_path, album_title, artist_name, release_date, parent_player
    ):
        super().__init__()
        self.parent_player = parent_player
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)

        self.cover_label = QLabel()
        self.cover_label.setFixedSize(200, 200)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setStyleSheet(
            "background-color: #F8F8FF; border-radius: 10px;"
        )
        self.cover_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        # 개선된 캐싱 함수 호출
        rounded_pixmap = self.parent_player.create_rounded_pixmap(
            album_cover_path, 200, 200, 10
        )
        if rounded_pixmap:
            self.cover_label.setPixmap(rounded_pixmap)
        else:
            self.cover_label.setText("커버 없음")

        self.title_label = QLabel(self.parent_player.truncate_text(album_title, 20))
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        if "ExtraBold" in self.parent_player.pretendard_fonts:
            self.title_label.setFont(self.parent_player.pretendard_fonts["ExtraBold"])
            self.title_label.setStyleSheet("font-size: 13px;")
        else:
            self.title_label.setStyleSheet("font-weight: bold;")
        self.title_label.setToolTip(album_title)

        self.artist_label = QLabel(self.parent_player.truncate_text(artist_name, 20))
        self.artist_label.setAlignment(Qt.AlignCenter)
        self.artist_label.setWordWrap(True)
        if "SemiBold" in self.parent_player.pretendard_fonts:
            self.artist_label.setFont(self.parent_player.pretendard_fonts["SemiBold"])
        self.artist_label.setToolTip(artist_name)

        self.date_label = QLabel(release_date)
        self.date_label.setAlignment(Qt.AlignCenter)
        if "Regular" in self.parent_player.pretendard_fonts:
            self.date_label.setFont(self.parent_player.pretendard_fonts["Regular"])

        layout.addWidget(self.cover_label, 0, Qt.AlignCenter)
        layout.addWidget(self.title_label, 0, Qt.AlignCenter)
        if artist_name:
            layout.addWidget(self.artist_label, 0, Qt.AlignCenter)
        if release_date:
            layout.addWidget(self.date_label, 0, Qt.AlignCenter)

        self.setLayout(layout)
        self.setFixedSize(220, 300)
        self.setStyleSheet("background-color: transparent;")


class MiniPlayer(QWidget):
    def __init__(self, parent_player):
        super().__init__()
        self.parent_player = parent_player
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setGeometry(100, 100, 400, 150)
        self.setObjectName("miniplayer")
        self.setStyleSheet(
            """
            #miniplayer {
                background-color: #E6E6FA;
                border: 1px solid #CFCFCF;
                border-radius: 10px;
            }
        """
        )
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(5)
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        self.close_button = QPushButton("X")
        self.close_button.setFixedSize(20, 20)

        if "Medium" in self.parent_player.pretendard_fonts:
            self.close_button.setFont(self.parent_player.pretendard_fonts["Medium"])
        else:
            self.close_button.setFont(QFont("Arial", 10))
        self.close_button.setFlat(True)
        self.close_button.setStyleSheet("QPushButton { color: #555; }")
        top_layout.addStretch()
        top_layout.addWidget(self.close_button)
        info_control_layout = QHBoxLayout()
        info_control_layout.setContentsMargins(0, 0, 0, 0)
        self.cover_label = QLabel()
        self.cover_label.setText("앨범 커버 없음")
        self.cover_label.setFixedSize(80, 80)
        self.cover_label.setStyleSheet(
            "border: 1px solid #ccc; background-color: #F8F8FF; border-radius: 5px;"
        )
        self.cover_label.setPixmap(QPixmap())
        info_control_layout.addWidget(self.cover_label)
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(10, 0, 10, 0)

        self.title_label = QLabel("재생 중인 곡 없음")
        if "ExtraBold" in self.parent_player.pretendard_fonts:
            self.title_label.setFont(self.parent_player.pretendard_fonts["ExtraBold"])
            self.title_label.setStyleSheet("color: #333; font-size: 14px;")
        else:
            self.title_label.setStyleSheet(
                "color: #333; font-weight: bold; font-size: 14px;"
            )

        self.artist_label = QLabel("아티스트")
        if "SemiBold" in self.parent_player.pretendard_fonts:
            self.artist_label.setFont(self.parent_player.pretendard_fonts["SemiBold"])
            self.artist_label.setStyleSheet("color: #666; font-size: 12px;")
        else:
            self.artist_label.setStyleSheet("color: #666; font-size: 12px;")

        text_layout.addStretch()
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.artist_label)
        text_layout.addStretch()
        info_control_layout.addLayout(text_layout)
        control_layout = QHBoxLayout()

        # 미니 플레이어 아이콘 설정 (부모 플레이어의 아이콘 참조)
        self.prev_button = QPushButton()
        self.play_pause_button = QPushButton()
        self.next_button = QPushButton()
        self.mode_button = QPushButton()

        self.prev_button.setIcon(self.parent_player.prev_icon)
        self.play_pause_button.setIcon(self.parent_player.play_icon)
        self.next_button.setIcon(self.parent_player.next_icon)
        self.mode_button.setIcon(self.parent_player.mini_player_icon)

        for btn in [
            self.prev_button,
            self.play_pause_button,
            self.next_button,
            self.mode_button,
        ]:
            btn.setFixedSize(30, 30)
            btn.setIconSize(QSize(16, 16))
            btn.setFlat(True)
            btn.setStyleSheet("QPushButton { color: #555; }")

        control_layout.addWidget(self.prev_button)
        control_layout.addWidget(self.play_pause_button)
        control_layout.addWidget(self.next_button)
        control_layout.addWidget(self.mode_button)
        info_control_layout.addLayout(control_layout)
        main_layout.addLayout(top_layout)
        main_layout.addLayout(info_control_layout)
        self.setLayout(main_layout)
        self.prev_button.clicked.connect(self.parent_player.play_prev)
        self.play_pause_button.clicked.connect(self.parent_player.play_pause_music)
        self.next_button.clicked.connect(self.parent_player.play_next)
        self.mode_button.clicked.connect(self.parent_player.toggle_mini_player)
        self.close_button.clicked.connect(self.parent_player.close)

    def mousePressEvent(self, event):
        self.old_pos = event.globalPos()

    def mouseMoveEvent(self, event):
        delta = event.globalPos() - self.old_pos
        self.move(self.x() + delta.x(), self.y() + delta.y())
        self.old_pos = event.globalPos()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_player = parent
        self.setWindowTitle("설정")
        self.setFixedSize(400, 250)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QDialogVBoxLayout(self)

        backend_label = QLabel("오디오 백엔드:")
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("자동 선택 (권장)", "auto")
        if SOUNDDEVICE_AVAILABLE:
            self.backend_combo.addItem("sounddevice (48kHz 원음)", "sounddevice")
        self.backend_combo.addItem("pygame (호환성)", "pygame")
        backend_layout = QHBoxLayout()
        backend_layout.addWidget(backend_label)
        backend_layout.addWidget(self.backend_combo)
        layout.addLayout(backend_layout)

        device_label = QLabel("재생 장치:")
        self.device_combo = QComboBox()
        if SOUNDDEVICE_AVAILABLE:
            try:
                devices = sd.query_devices()
                default_dev = sd.default.device
                default_out = (
                    default_dev[1] if isinstance(default_dev, tuple) else default_dev
                )
                for idx, dev in enumerate(devices):
                    if dev["max_output_channels"] > 0:
                        self.device_combo.addItem(f"{idx}: {dev['name']}", idx)
                        if idx == default_out:
                            self.device_combo.setCurrentIndex(
                                self.device_combo.count() - 1
                            )
            except Exception:
                pass
        device_layout = QHBoxLayout()
        device_layout.addWidget(device_label)
        device_layout.addWidget(self.device_combo)
        layout.addLayout(device_layout)

        info_label = QLabel("폰트: Pretendard | FLAC는 48kHz로 재생")
        if parent and "Medium" in parent.pretendard_fonts:
            info_label.setFont(parent.pretendard_fonts["Medium"])
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #666; font-size: 10px;")
        layout.addWidget(info_label)
        layout.addStretch()

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        self.setLayout(layout)
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists("player_settings.json"):
                with open("player_settings.json", "r", encoding="utf-8") as f:
                    settings = json.load(f)
                    backend = settings.get("audio_backend", "auto")
                    device = settings.get("audio_device", None)
                    idx = self.backend_combo.findData(backend)
                    if idx >= 0:
                        self.backend_combo.setCurrentIndex(idx)
                    if device is not None and self.device_combo.count() > 0:
                        dev_idx = self.device_combo.findData(device)
                        if dev_idx >= 0:
                            self.device_combo.setCurrentIndex(dev_idx)
        except Exception:
            pass

    def get_settings(self):
        return {
            "audio_backend": self.backend_combo.currentData(),
            "audio_device": (
                self.device_combo.currentData()
                if self.device_combo.count() > 0
                else None
            ),
        }


class EQDialog(QDialog):
    """Simple graphic equalizer dialog with ten bands."""

    def __init__(self, parent=None, initial_gains=None):
        super().__init__(parent)
        self.setWindowTitle("이퀄라이저")
        self.setFixedSize(370, 370)
        layout = QVBoxLayout(self)
        # presets
        self.presets = {
            "Flat": [0] * 10,
            "Bass Boost": [6, 4, 2, 0, -1, -1, 0, 1, 2, 2],
            "Treble Boost": [0, 0, 0, 0, 1, 2, 3, 4, 6, 6],
            "Vocal": [-1, -1, 0, 1, 2, 3, 2, 1, 0, -1],
            "Rock": [4, 2, 1, 0, 0, 1, 2, 3, 3, 2],
        }
        preset_layout = QHBoxLayout()
        preset_label = QLabel("프리셋")
        self.preset_combo = QComboBox()
        for name in self.presets.keys():
            self.preset_combo.addItem(name)
        self.preset_combo.addItem("Custom")
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.setFixedWidth(120)

        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset_combo)
        layout.addLayout(preset_layout)
        self.sliders = []
        band_labels = [
            "31.25 Hz",
            "62.5 Hz",
            "125 Hz",
            "250 Hz",
            "500 Hz",
            "1 kHz",
            "2 kHz",
            "4 kHz",
            "8 kHz",
            "16 kHz",
        ]
        for idx, label_text in enumerate(band_labels):
            hbox = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setFixedWidth(70)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            slider = QSlider(Qt.Horizontal)
            slider.setRange(-12, 12)
            value_label = QLabel()
            value_label.setFixedWidth(50)
            value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            val = 0
            if initial_gains and idx < len(initial_gains):
                try:
                    val = int(initial_gains[idx])
                except Exception:
                    val = 0
            slider.setValue(val)
            value_label.setText(f"{val} dB")

            # update label and mark preset as Custom when user adjusts sliders
            def _on_slider_change(v, lbl=value_label, self_ref=None):
                lbl.setText(f"{v} dB")
                # mark custom if current slider set doesn't match selected preset
                try:
                    sel = self.preset_combo.currentText()
                    if sel != "Custom":
                        preset_vals = self.presets.get(sel, None)
                        # check current sliders vs preset later after event loop
                        self.preset_combo.setCurrentText("Custom")
                except Exception:
                    pass

            slider.valueChanged.connect(_on_slider_change)
            self.sliders.append(slider)
            hbox.addWidget(lbl)
            hbox.addWidget(slider)
            hbox.addWidget(value_label)
            layout.addLayout(hbox)
        layout.addStretch()
        button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Reset
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        button_box.button(QDialogButtonBox.Reset).clicked.connect(
            lambda: [s.setValue(0) for s in self.sliders]
        )
        layout.addWidget(button_box)
        self.setLayout(layout)

        # when preset changes, update sliders
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)

        # apply initial gains or preset
        if initial_gains and len(initial_gains) >= len(self.sliders):
            # try to find preset match
            for name, vals in self.presets.items():
                if all(int(vals[i]) == int(initial_gains[i]) for i in range(len(vals))):
                    self.preset_combo.setCurrentText(name)
                    break
            else:
                self.preset_combo.setCurrentText("Custom")
        else:
            self._apply_selected_preset(0)

    def get_gains(self):
        return [s.value() for s in self.sliders]

    def _apply_selected_preset(self, idx):
        try:
            name = self.preset_combo.currentText()
            if name in self.presets:
                vals = self.presets[name]
                for i, s in enumerate(self.sliders):
                    s.setValue(int(vals[i]))
        except Exception:
            pass


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("NVPlayer 정보...")
        self.setFixedSize(400, 300)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QDialogVBoxLayout(self)

        logo_label = QLabel(self)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "icons", "icon.png")
        logo_pixmap = QPixmap(icon_path)
        if not logo_pixmap.isNull():
            logo_pixmap = logo_pixmap.scaled(
                64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            logo_label.setPixmap(logo_pixmap)
        else:
            logo_label.setText("아이콘 로드 실패")
        logo_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo_label, alignment=Qt.AlignCenter)

        info_text_browser = QTextBrowser(self)
        info_text_browser.setOpenExternalLinks(True)
        info_text_browser.setReadOnly(True)
        if parent and "Medium" in parent.pretendard_fonts:
            info_text_browser.setFont(parent.pretendard_fonts["Medium"])
        else:
            info_text_browser.setFont(QFont("Segoe UI", 10))
        info_text_browser.setHtml(
            """
            <p align="center"><b>NVPlayer v1.0.0</b></p>
            <p align="center">2025-2026 NVP / MY</p> <p align="center">Qt 5.15.2 기반</p>
            <p align="center">음악 감상을 위한 강력한 플레이어</p>
            <p align="center"><a href="https://github.com/ningning-voice">개발자 GitHub</p>
            <p align="center">아이디어 제공: wns0377@naver.com</p> <p align="center">이용해주셔서 감사합니다.</p>
            <p align="center">라이선스: MIT License</p> <p align="center">폰트: Pretendard</p>
            """
        )
        layout.addWidget(info_text_browser)
        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        button_box.accepted.connect(self.accept)
        layout.addWidget(button_box)


class MetadataMusicPlayer(QWidget):
    pretendard_fonts = {}

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NVPlayer")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        app_icon_path = os.path.join(script_dir, "icons", "app_icon.png")
        self.setWindowIcon(QIcon(app_icon_path))
        self.setGeometry(300, 300, 1200, 700)

        self.define_icons()  # 아이콘 정의 메서드 호출

        # --- DB 및 캐시 경로 설정 ---
        self.db_path = "library.db"
        self.cache_path = os.path.join(os.path.expanduser("~"), ".NVPlayerCache")
        if not os.path.exists(self.cache_path):
            os.makedirs(self.cache_path)
        # delegate DB initialization and cache maintenance to LibraryManager
        self.library = LibraryManager(self.db_path, self.cache_path)
        # ---------------------------

        # Reduced font weight set for better legibility and simpler maintenance.
        # Keep a small, useful subset: Regular, Medium, SemiBold, ExtraBold.
        # Thinner weights (Thin/ExtraLight/Light) and Black are omitted
        # because they reduce readability at typical UI sizes.
        font_definitions = [
            ("Pretendard-Regular.otf", "Regular"),
            ("Pretendard-Medium.otf", "Medium"),
            ("Pretendard-SemiBold.otf", "SemiBold"),
            ("Pretendard-ExtraBold.otf", "ExtraBold"),
        ]
        for filename, weight_name in font_definitions:
            font_file_path = os.path.join(script_dir, "fonts", filename)
            if os.path.exists(font_file_path):
                font_id = QFontDatabase.addApplicationFont(font_file_path)
                if font_id != -1:
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        self.pretendard_fonts[weight_name] = QFont(families[0])

        if "Medium" in self.pretendard_fonts:
            QApplication.instance().setFont(self.pretendard_fonts["Medium"])

        # LRU 캐시 (최대 100개 항목)
        self.pixmap_cache = OrderedDict()
        self.MAX_PIXMAP_CACHE = 100
        self.current_view = "artists"
        self.current_artist = None
        self.current_album = None
        self.selected_flac = None
        self.current_row = -1
        self.is_paused = False
        self.music_data = []
        self.current_playlist = []
        self.user_playlist = []
        self.artist_albums_covers = {}
        self.current_sort_column = 0
        self.sort_ascending = True
        self.folder_path = ""
        self.playback_history = []
        # 미니 플레이어 토글 시 메인 윈도우 위치 저장
        self.main_window_geometry = None
        # 사용자 설정: 곡마다 mixer를 강제 재초기화할지 여부 (True면 항상 재초기화, False면 필요시만 재초기화)
        self.force_mixer_reinit = False  # 기존 동작 유지하려면 True로 설정
        self.history_max_size = 100
        self.is_muted = False
        self.previous_volume = 0.5
        # equalizer gains (in dB) for 10-band graphic EQ: [31.25Hz,62.5Hz,125Hz,250Hz,500Hz,1kHz,2kHz,4kHz,8kHz,16kHz]
        self.eq_gains = [0] * 10
        # Increased base font-size to improve readability across the UI.
        self.default_stylesheet = """
            QWidget { background-color: #E6E6FA; color: #1b1b1b; font-size: 13px; }
            QGroupBox { background-color: rgba(230,230,250,0.9); border: 1px solid #CFCFCF; border-radius: 10px; margin-top: 10px; padding: 10px; color: #1b1b1b; }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 3px; background-color: transparent; color: #1b1b1b; }
            QTableWidget, QListWidget { background-color: rgba(255,255,255,0.95); border: 1px solid #CFCFCF; border-radius: 10px; color: #111111; selection-background-color: #8f66d9; selection-color: #ffffff; }
            QHeaderView::section { background-color: #F3E8FF; color: #111111; padding: 6px; border: 1px solid #E2D6F5; }
            QPushButton { background-color: #F7F5FA; border: 1px solid #D6CFE6; border-radius: 10px; padding: 6px; color: #111111; }
            QPushButton:hover { background-color: #E7DDF6; } QPushButton:pressed { background-color: #CDBAF0; } QPushButton:checked { background-color: #A078D6; color: #ffffff; }
            QToolTip { font-size: 10px; border: 1px solid #CFCFCF; background-color: #FFFFF0; color: #1b1b1b; }
            QSlider::groove:horizontal { border: 1px solid #DCDCDC; height: 8px; background: #F0F0F0; border-radius: 4px; }
            QSlider::handle:horizontal { background: #8f66d9; border: 1px solid #6e41c8; width: 18px; margin: -5px 0; border-radius: 9px; }
            QSlider::sub-page:horizontal { background: #8f66d9; }
            QTextEdit, QLineEdit { background-color: #FFFFFF; border: 1px solid #DCDCDC; border-radius: 10px; color: #111111; padding: 6px; }
            QScrollBar:vertical { border: none; background: #f0f0f0; width: 10px; margin: 15px 0 15px 0; border-radius: 5px; }
            QScrollBar::handle:vertical { background: #8f66d9; min-height: 20px; border-radius: 5px; margin: 0px 1px; }
        """
        self.is_shuffled = False
        self.repeat_mode = 0
        self.current_volume = 0.5
        self.playback_start_offset_ms = 0
        self.visualizer_timer = QTimer(self)
        self.visualizer_timer.timeout.connect(self.update_visualizer)
        self.visualizer_bars = []
        self.visualizer_scene = QGraphicsScene(self)
        self.mini_player = MiniPlayer(self)
        try:
            locale.setlocale(locale.LC_COLLATE, "ko_KR.UTF-8")
        except locale.Error:
            try:
                locale.setlocale(locale.LC_COLLATE, "Korean_Korea.949")
            except locale.Error:
                pass
        # initialize audio controller to manage pygame/sounddevice backends
        self.audio_ctrl = AudioController(self, eq_gains=self.eq_gains)
        self.pygame_initialized = self.audio_ctrl.pygame_initialized
        self.use_sounddevice = self.audio_ctrl.use_sounddevice
        self.audio_player = self.audio_ctrl.audio_player
        # mirror mixer settings if available
        self._mixer_settings = getattr(self.audio_ctrl, "_mixer_settings", None)
        print(f"SoundDevice available: {self.use_sounddevice}")
        self.setup_ui()
        self.load_library()
        self.load_playlist()
        self.load_history()
        self.load_playback_state()

        self.check_playback_timer = QTimer(self)
        if self.pygame_initialized:
            self.check_playback_timer.setInterval(1000)
            self.check_playback_timer.timeout.connect(self.check_playback_status)
        self.setStyleSheet(self.default_stylesheet)
        try:
            self.volume_label.setText(f"{self.volume_slider.value()}%")
        except Exception:
            self.volume_label.setText(f"{int(self.current_volume * 100)}%")

    # helper methods for Pretendard font handling -----------------------------
    def get_font(self, weight):
        """Return the requested Pretendard font weight if it has been loaded."""
        return self.pretendard_fonts.get(weight)

    def apply_font(self, widget, weight):
        """Apply a Pretendard font weight to a widget/item if available.

        `widget` may be any object with a `setFont` method (QWidget, QAction,
        QTableWidgetItem, etc.).
        """
        font = self.get_font(weight)
        if font:
            widget.setFont(font)

    def define_icons(self):
        """애플리케이션에서 사용할 모든 아이콘을 정의합니다."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icons_dir = os.path.join(script_dir, "icons")

        self.play_icon = QIcon(os.path.join(icons_dir, "play.png"))
        self.pause_icon = QIcon(os.path.join(icons_dir, "pause.png"))
        self.prev_icon = QIcon(os.path.join(icons_dir, "previous.png"))
        self.next_icon = QIcon(os.path.join(icons_dir, "next.png"))
        self.stop_icon = QIcon(os.path.join(icons_dir, "stop.png"))
        self.shuffle_icon = QIcon(os.path.join(icons_dir, "shuffle.png"))
        self.repeat_icon = QIcon(os.path.join(icons_dir, "repeat.png"))
        self.repeat_one_icon = QIcon(os.path.join(icons_dir, "repeat_one.png"))
        self.mute_pixmap = QPixmap(os.path.join(icons_dir, "mute.png"))
        self.volume_pixmap = QPixmap(os.path.join(icons_dir, "volume.png"))
        self.eq_icon = QIcon(os.path.join(icons_dir, "eq.png"))
        self.mini_player_icon = QIcon(os.path.join(icons_dir, "mini_player.png"))

    # Database initialization moved to `LibraryManager`.

    def setup_menubar(self):
        self.menubar = QMenuBar(self)
        file_menu = self.menubar.addMenu("파일(&F)")

        settings_action = QAction("설정(&S)", self)
        self.apply_font(settings_action, "Medium")
        settings_action.triggered.connect(self.show_settings_dialog)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()

        exit_action = QAction("종료(&X)", self)
        self.apply_font(exit_action, "Medium")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        help_menu = self.menubar.addMenu("도움말(&H)")
        about_action = QAction("NVPlayer 정보...(&A)", self)
        self.apply_font(about_action, "Medium")
        about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(about_action)

        self.main_layout_for_menubar = QHBoxLayout()
        self.main_layout_for_menubar.setContentsMargins(0, 0, 0, 0)
        self.main_layout_for_menubar.addWidget(self.menubar)
        self.main_layout_for_menubar.addStretch()

    def setup_ui(self):
        # Delegate UI construction to UIManager to keep this class focused.
        self.ui = UIManager()
        self.ui.setup_ui(self)

    def show_settings_dialog(self):
        dialog = SettingsDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            settings = dialog.get_settings()
            self.save_settings(settings)
            backend = settings.get("audio_backend")
            device = settings.get("audio_device")
            if backend == "pygame":
                self.use_sounddevice = False
            elif backend == "sounddevice" and SOUNDDEVICE_AVAILABLE:
                self.use_sounddevice = True
            elif backend == "auto":
                self.use_sounddevice = SOUNDDEVICE_AVAILABLE
            if device is not None and SOUNDDEVICE_AVAILABLE:
                try:
                    sd.default.device = device
                except Exception:
                    pass
            QMessageBox.information(
                self, "설정", "설정이 저장되었습니다.\n다음 곡부터 적용됩니다."
            )

    def show_eq_dialog(self):
        dialog = EQDialog(self, initial_gains=self.eq_gains)
        if dialog.exec_() == QDialog.Accepted:
            new_gains = dialog.get_gains()
            self.eq_gains = new_gains
            # propagate to backend player if in use
            if self.use_sounddevice and self.audio_player:
                self.audio_player.set_eq_gains(self.eq_gains)
            # save EQ gains and selected preset to settings file
            preset_name = (
                dialog.preset_combo.currentText()
                if hasattr(dialog, "preset_combo")
                else None
            )
            settings_to_save = {"eq_gains": self.eq_gains}
            if preset_name:
                settings_to_save["eq_preset"] = preset_name
            merge_json_file("player_settings.json", settings_to_save)
            QMessageBox.information(
                self,
                "이퀄라이저",
                "이퀄라이저 설정이 저장되었습니다.\n(사운드디바이스 백엔드 사용 시에만 적용됩니다)",
            )

    def save_settings(self, settings):
        try:
            merge_json_file("player_settings.json", settings)
        except Exception as e:
            print(f"Error saving settings: {e}")

    def show_about_dialog(self):
        AboutDialog(self).exec_()

    def update_volume_label(self, value):
        self.volume_label.setText(f"{value}%")
        if value > 0 and self.is_muted:
            self.is_muted = False
            self.volume_icon.setPixmap(
                self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )

    def _apply_volume_to_backend(self, value_float):
        # apply volume to whichever backend is active
        if getattr(self, "use_sounddevice", False) and self.audio_player:
            try:
                self.audio_player.set_volume(value_float)
            except Exception:
                pass
        if self.pygame_initialized:
            try:
                pygame.mixer.music.set_volume(value_float)
            except Exception:
                pass

    def setup_visualizer(self):
        self.visualizer_scene.clear()
        self.visualizer_bars = []
        bar_width, bar_spacing, num_bars = 8, 4, 10
        total_width = num_bars * (bar_width + bar_spacing) - bar_spacing
        start_x = -total_width / 2
        for i in range(num_bars):
            bar = QGraphicsRectItem(
                start_x + i * (bar_width + bar_spacing), 0, bar_width, 1
            )
            bar.setBrush(QColor("#8f66d9"))
            bar.setPen(QPen(Qt.NoPen))
            self.visualizer_scene.addItem(bar)
            self.visualizer_bars.append(bar)
        self.visualizer_view.setScene(self.visualizer_scene)

    def update_visualizer(self):
        if not self.pygame_initialized or not self.visualizer_view.isVisible():
            return
        for bar in self.visualizer_bars:
            new_height = random.randint(5, 45)
            new_y = (50 - new_height) / 2
            bar.setRect(bar.rect().x(), new_y, bar.rect().width(), new_height)

    def toggle_mute(self):
        if not self.pygame_initialized:
            return
        if self.is_muted:
            target_volume = self.previous_volume if self.previous_volume > 0 else 0.5
            self._apply_volume_to_backend(target_volume)
            self.volume_slider.setValue(int(target_volume * 100))
            self.volume_icon.setPixmap(
                self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )
            self.is_muted = False
        else:
            self.previous_volume = self.current_volume
            self._apply_volume_to_backend(0)
            self.volume_slider.setValue(0)
            self.volume_icon.setPixmap(
                self.mute_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )
            self.is_muted = True
        self.update_volume_label(self.volume_slider.value())

    def setup_artists_page(self):
        page_layout = QVBoxLayout(self.artists_page)
        toolbar_layout = QHBoxLayout()
        self.back_button = QPushButton("← 뒤로가기", self)
        self.back_button.setEnabled(False)
        self.apply_font(self.back_button, "SemiBold")
        self.artists_search_box = QLineEdit(self)
        self.artists_search_box.setPlaceholderText("검색 (아티스트, 앨범)")
        self.apply_font(self.artists_search_box, "Medium")
        self.artists_search_box.textChanged.connect(
            lambda text: self.search_music_in_view(text, "artists")
        )
        toolbar_layout.addWidget(self.back_button)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self.artists_search_box)
        self.artists_list_view = QListWidget()
        self.artists_list_view.setResizeMode(QListWidget.Adjust)
        self.artists_list_view.setFlow(QListWidget.LeftToRight)
        self.artists_list_view.setWrapping(True)
        self.artists_list_view.setViewMode(QListWidget.IconMode)
        self.artists_list_view.setIconSize(QSize(220, 300))
        self.artists_list_view.setGridSize(QSize(230, 310))
        self.artists_list_view.setSpacing(10)
        self.artists_list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.artists_list_view.verticalScrollBar().setSingleStep(20)
        self.artists_list_view.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.artists_list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.artists_list_view.customContextMenuRequested.connect(
            self.show_context_menu
        )
        page_layout.addLayout(toolbar_layout)
        page_layout.addWidget(self.artists_list_view)
        self.back_button.clicked.connect(self.go_back)

    def setup_all_tracks_page(self):
        page_layout = QVBoxLayout(self.all_tracks_page)
        toolbar_layout = QHBoxLayout()
        self.play_all_tracks_button = QPushButton("전체 곡 재생", self)
        self.apply_font(self.play_all_tracks_button, "SemiBold")
        self.tracks_search_box = QLineEdit(self)
        self.tracks_search_box.setPlaceholderText("검색 (제목, 아티스트, 앨범)")
        self.apply_font(self.tracks_search_box, "Medium")
        self.tracks_search_box.textChanged.connect(
            lambda text: self.search_music_in_view(text, "all_tracks")
        )
        toolbar_layout.addWidget(self.play_all_tracks_button)
        toolbar_layout.addStretch()
        toolbar_layout.addWidget(self.tracks_search_box)
        self.track_table = QTableWidget()
        self.track_table.setShowGrid(False)
        self.track_table.setAlternatingRowColors(True)
        self.track_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.track_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setColumnCount(3)
        self.track_table.setHorizontalHeaderLabels(["제목", "아티스트", "길이"])
        self.apply_font(self.track_table.horizontalHeader(), "Bold")
        self.track_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.track_table.horizontalHeader().sectionClicked.connect(
            self.sort_all_tracks_table
        )
        self.track_table.itemDoubleClicked.connect(self.play_music_from_table)
        self.track_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.track_table.customContextMenuRequested.connect(self.show_context_menu)
        page_layout.addLayout(toolbar_layout)
        page_layout.addWidget(self.track_table)

    def setup_history_page(self):
        page_layout = QVBoxLayout(self.history_page)
        self.history_list = QListWidget()
        page_layout.addWidget(self.history_list)
        self.show_history_view()

    def load_library(self):
        """DB에서 라이브러리를 먼저 로드하고, 백그라운드에서 업데이트를 확인합니다."""
        self.load_library_from_db()
        try:
            if os.path.exists("player_settings.json"):
                with open("player_settings.json", "r", encoding="utf-8") as f:
                    settings = json.load(f)
                    self.folder_path = settings.get("last_folder", "")
                    if self.folder_path and os.path.exists(self.folder_path):
                        self.label.setText("라이브러리 업데이트 확인 중...")
                        self.library.start_scan(self, self.folder_path)
        except Exception as e:
            QMessageBox.critical(
                self,
                "설정 파일 오류",
                f"설정 파일을 불러오는 중 오류가 발생했습니다: {e}",
            )

    def load_library_from_db(self):
        """데이터베이스에서 모든 트랙 정보를 불러와 UI에 표시합니다."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM tracks ORDER BY artist, date, album, track")
            rows = cursor.fetchall()

            music_data = [dict(row) for row in rows]
            artist_albums_covers = {}
            for track_info in music_data:
                artist, album, date, path = (
                    track_info["artist"],
                    track_info["album"],
                    track_info["date"],
                    track_info["path"],
                )
                if artist not in artist_albums_covers:
                    artist_albums_covers[artist] = {}
                if album not in artist_albums_covers[
                    artist
                ] or date > artist_albums_covers[artist][album].get("date", "0000"):
                    artist_albums_covers[artist][album] = {"date": date, "path": path}

            self.music_data = music_data
            self.artist_albums_covers = artist_albums_covers

            self.label.setText(
                f"총 {len(self.music_data)}곡 불러옴."
                if self.music_data
                else "음악 폴더를 선택하세요."
            )
            self.switch_view("artists")
        except sqlite3.Error as e:
            print(f"DB 로딩 오류: {e}")
        finally:
            conn.close()

    def open_folder(self):
        folder = default_dir = (
            self.folder_path if self.folder_path else os.path.expanduser("~")
        )
        folder = QFileDialog.getExistingDirectory(self, "음악 폴더 선택", default_dir)
        if folder:
            self.save_last_folder(folder)
            self.folder_path = folder
            # 새 폴더 선택 시, DB를 초기화하고 다시 스캔
            msg = QMessageBox.question(
                self,
                "라이브러리 재구성",
                "새 폴더를 선택했습니다. 기존 라이브러리 정보를 지우고 새로 구성하시겠습니까?",
            )
            if msg == QMessageBox.Yes:
                # clear database via library manager
                try:
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM tracks")
                    conn.commit()
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
                self.music_data = []
                self.artist_albums_covers = {}
                self.switch_view("artists")
                # delegate scan setup to library manager
                self.library.start_scan(self, folder)

    def on_scan_finished(self, added_count, deleted_count):
        """스캔 완료 후 UI를 업데이트하고 결과를 알립니다."""
        if added_count > 0 or deleted_count > 0:
            QMessageBox.information(
                self,
                "라이브러리 업데이트",
                f"{added_count}곡 추가, {deleted_count}곡 삭제 완료.",
            )
            self.load_library_from_db()  # 변경된 라이브러리 다시 로드
        else:
            self.label.setText(
                f"라이브러리가 최신 상태입니다. (총 {len(self.music_data)}곡)"
            )
        self.open_folder_button.setEnabled(True)

    def update_progress_label(self, message):
        self.label.setText(self.truncate_text(message, 100))

    def on_scan_error(self, error_message):
        QMessageBox.critical(self, "스캔 오류", error_message)
        self.label.setText("오류 발생. 다른 폴더를 선택해주세요.")
        self.open_folder_button.setEnabled(True)

    def on_scan_batch(self, added_so_far, total_new):
        try:
            self.label.setText(
                self.truncate_text(
                    f"라이브러리 업데이트 중... 추가됨: {added_so_far}/{total_new}", 100
                )
            )
        except Exception:
            pass

    def create_rounded_pixmap(self, file_path, width, height, radius):
        """디스크 캐시를 확인/생성하고 둥근 QPixmap 객체를 반환합니다."""
        if not file_path:
            return None
        try:
            mtime = os.path.getmtime(file_path)
        except Exception:
            mtime = None
        cache_key = hashlib.md5(
            f"{file_path}:{width}x{height}:{mtime}".encode()
        ).hexdigest()
        cached_thumb_path = os.path.join(self.cache_path, f"{cache_key}.png")
        if cached_thumb_path in self.pixmap_cache:
            return self.pixmap_cache[cached_thumb_path]

        if os.path.exists(cached_thumb_path):
            pixmap = QPixmap(cached_thumb_path)
        else:
            image_data = self._get_cover_data_from_file(file_path)
            if not image_data:
                return None

            source_pixmap = QPixmap()
            if not source_pixmap.loadFromData(image_data):
                return None

            pixmap = source_pixmap.scaled(
                width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            # write thumbnail to disk (best-effort)
            try:
                pixmap.save(cached_thumb_path, "PNG")
            except Exception:
                pass

        rounded = QPixmap(pixmap.size())
        rounded.fill(Qt.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rounded.rect()), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()

        # LRU 캐시 유지: 최대 크기 초과 시 가장 오래된 항목 제거
        self.pixmap_cache[cached_thumb_path] = rounded
        if len(self.pixmap_cache) > self.MAX_PIXMAP_CACHE:
            # 가장 오래된 항목(첫 번째) 제거
            oldest_key = next(iter(self.pixmap_cache))
            del self.pixmap_cache[oldest_key]

        return rounded

    def show_album_cover(self, file_path, target_label=None):
        label = target_label if target_label else self.cover_label
        label.clear()
        rounded_pixmap = self.create_rounded_pixmap(
            file_path, label.width(), label.height(), 10
        )
        if rounded_pixmap:
            label.setPixmap(rounded_pixmap)
        else:
            label.setText("앨범 커버 없음")

    # Cache cleanup has been moved to `LibraryManager.cleanup_old_cache_files`

    def _get_cover_data_from_file(self, file_path):
        """파일에서 직접 커버 이미지 바이너리를 추출합니다."""
        try:
            audio = File(file_path)
            if not audio:
                return None
            if isinstance(audio, FLAC) and getattr(audio, "pictures", None):
                return audio.pictures[0].data
            elif hasattr(audio, "tags"):
                for key in audio.tags.keys():
                    if key.startswith("APIC"):
                        return audio.tags[key].data
            return None
        except Exception:
            return None

    def truncate_text(self, text, max_len):
        return text[:max_len] + "..." if len(text) > max_len else text

    def _update_button_ui(self):
        self.shuffle_button.setChecked(self.is_shuffled)

        # 반복 버튼 상태 업데이트
        if self.repeat_mode == 0:  # 끄기
            self.repeat_button.setIcon(self.repeat_icon)
            self.repeat_button.setStyleSheet("")
            self.repeat_button.setToolTip("반복 재생 (끄기)")
        elif self.repeat_mode == 1:  # 전체 반복
            self.repeat_button.setIcon(self.repeat_icon)
            self.repeat_button.setStyleSheet(
                "QPushButton { background-color: #A078D6; }"
            )
            self.repeat_button.setToolTip("전체 반복")
        else:  # 한 곡 반복
            self.repeat_button.setIcon(self.repeat_one_icon)
            self.repeat_button.setStyleSheet(
                "QPushButton { background-color: #A078D6; }"
            )
            self.repeat_button.setToolTip("한 곡 반복")

    def save_playback_state(self):
        try:
            state = {
                "is_shuffled": self.is_shuffled,
                "repeat_mode": self.repeat_mode,
                "last_played_row": self.current_row,
                "current_volume": self.current_volume,
                "is_muted": self.is_muted,
                "previous_volume": self.previous_volume,
            }
            write_json_file("playback_state.json", state)
        except Exception as e:
            print(f"Error saving playback state: {e}")

    def load_playback_state(self):
        try:
            state = read_json_file("playback_state.json", {}) or {}
            self.is_shuffled = state.get("is_shuffled", False)
            self.repeat_mode = state.get("repeat_mode", 0)
            self.current_row = state.get("last_played_row", -1)
            self.current_volume = state.get("current_volume", 0.5)
            self.is_muted = state.get("is_muted", False)
            self.previous_volume = state.get("previous_volume", 0.5)
            # Load audio/settings (including EQ)
            settings = read_json_file("player_settings.json", {}) or {}
            backend = settings.get("audio_backend", "auto")
            device = settings.get("audio_device", None)
            self.eq_gains = settings.get("eq_gains", [0] * 10)
            # propagate to player if available
            if self.use_sounddevice and self.audio_player:
                self.audio_player.set_eq_gains(self.eq_gains)
            if backend == "pygame":
                self.use_sounddevice = False
            elif backend == "sounddevice" and SOUNDDEVICE_AVAILABLE:
                self.use_sounddevice = True
            elif backend == "auto":
                self.use_sounddevice = SOUNDDEVICE_AVAILABLE
            if device is not None and SOUNDDEVICE_AVAILABLE:
                try:
                    sd.default.device = device
                except Exception:
                    pass

            if self.pygame_initialized:
                if self.is_muted:
                    self._apply_volume_to_backend(0)
                    self.volume_slider.setValue(0)
                    self.volume_icon.setPixmap(
                        self.mute_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
                    )
                else:
                    self._apply_volume_to_backend(self.current_volume)
                    self.volume_slider.setValue(int(self.current_volume * 100))
                    self.volume_icon.setPixmap(
                        self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
                    )
            self._update_button_ui()
            if self.user_playlist and 0 <= self.current_row < len(self.user_playlist):
                self.update_now_playing()
        except (FileNotFoundError, json.JSONDecodeError):
            # 파일이 없거나 손상된 경우 기본값 적용
            self.is_shuffled = False
            self.repeat_mode = 0
            self.current_row = -1
            self.current_volume = 0.5
            self.is_muted = False
            self.previous_volume = 0.5

            if self.pygame_initialized:
                pygame.mixer.music.set_volume(self.current_volume)
            self.volume_slider.setValue(int(self.current_volume * 100))
            self.volume_icon.setPixmap(
                self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )
            # 버튼 상태 초기화
            self._update_button_ui()

    def toggle_shuffle(self):
        self.is_shuffled = not self.is_shuffled
        self.shuffle_button.setChecked(self.is_shuffled)
        self.save_playback_state()

    def toggle_repeat(self):
        self.repeat_mode = (self.repeat_mode + 1) % 3
        self._update_button_ui()
        self.save_playback_state()

    def toggle_mini_player(self):
        if self.isVisible():
            # 메인 윈도우 숨기고 미니 플레이어 표시
            self.main_window_geometry = self.geometry()  # 메인 윈도우 위치/크기 저장
            self.hide()
            self.update_now_playing()
            # 미니 플레이어를 메인 윈도우의 현재 위치에 배치
            self.mini_player.move(
                self.main_window_geometry.x(), self.main_window_geometry.y()
            )
            self.mini_player.show()
        else:
            # 메인 윈도우 표시하고 미니 플레이어 숨기기
            self.mini_player.hide()
            # 저장된 위치에서 메인 윈도우 복원
            if self.main_window_geometry:
                self.setGeometry(self.main_window_geometry)
            self.show()

    def set_volume(self, value):
        self.current_volume = value / 100.0
        # apply to backend(s)
        self._apply_volume_to_backend(self.current_volume)
        if value > 0 and self.is_muted:
            self.is_muted = False
            self.volume_icon.setPixmap(
                self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )
        elif value == 0 and not self.is_muted:
            self.is_muted = True
            self.volume_icon.setPixmap(
                self.mute_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )

    def keyPressEvent(self, event):
        if not self.pygame_initialized:
            return
        actions = {
            Qt.Key_Space: self.play_pause_music,
            Qt.Key_Right: self.play_next,
            Qt.Key_Left: self.play_prev,
        }
        if event.key() in actions:
            actions[event.key()]()
        elif event.key() == Qt.Key_Up:
            self.volume_slider.setValue(min(self.volume_slider.value() + 5, 100))
        elif event.key() == Qt.Key_Down:
            self.volume_slider.setValue(max(self.volume_slider.value() - 5, 0))
        super().keyPressEvent(event)

    def play_pause_music(self):
        if not self.user_playlist:
            return
        # prefer sounddevice backend if available
        if getattr(self, "use_sounddevice", False) and self.audio_player:
            try:
                if self.is_paused:
                    self.audio_player.resume()
                    self.is_paused = False
                    self.play_pause_button.setIcon(self.pause_icon)
                    self.mini_player.play_pause_button.setIcon(self.pause_icon)
                    self.visualizer_timer.start(100)
                elif self.audio_player.is_busy():
                    self.audio_player.pause()
                    self.is_paused = True
                    self.play_pause_button.setIcon(self.play_icon)
                    self.mini_player.play_pause_button.setIcon(self.play_icon)
                    self.visualizer_timer.stop()
                else:
                    self.play_music()
            except Exception as e:
                QMessageBox.critical(
                    self, "재생 오류", f"일시정지/재개 중 오류 발생: {e}"
                )
                self.stop_music()
            return

        # fallback to pygame
        if not self.pygame_initialized or not pygame.mixer.get_init():
            QMessageBox.warning(
                self, "오류", "오디오 장치가 비활성화되어 재생할 수 없습니다."
            )
            return self.stop_music()
        try:
            if self.is_paused:
                pygame.mixer.music.unpause()
                self.is_paused = False
                self.play_pause_button.setIcon(self.pause_icon)
                self.mini_player.play_pause_button.setIcon(self.pause_icon)
                self.visualizer_timer.start(100)
            elif pygame.mixer.music.get_busy():
                pygame.mixer.music.pause()
                self.is_paused = True
                self.play_pause_button.setIcon(self.play_icon)
                self.mini_player.play_pause_button.setIcon(self.play_icon)
                self.visualizer_timer.stop()
            else:
                self.play_music()
        except pygame.error as e:
            QMessageBox.critical(self, "재생 오류", f"일시정지/재개 중 오류 발생: {e}")
            self.stop_music()

    def play_music(self, start_pos=0.0):
        if not self.user_playlist or not (
            0 <= self.current_row < len(self.user_playlist)
        ):
            return self.stop_music()

        track_info = self.user_playlist[self.current_row]
        self.selected_flac = track_info.get("path")
        if not self.selected_flac or not os.path.exists(self.selected_flac):
            QMessageBox.warning(
                self, "파일 없음", f"파일을 찾을 수 없습니다:\n{self.selected_flac}"
            )
            return self.play_next()

        # If sounddevice backend available, use it for native playback
        if getattr(self, "use_sounddevice", False) and self.audio_player:
            try:
                try:
                    # 엔진 이름 설정
                    # sounddevice가 활성화된 상태라면 'Hi-Fi' 또는 'SoundDevice'로 표시
                    engine_name = "Hi-Fi"
                    # Hi-Fi 글자만 색상과 굵기를 다르게 적용
                    engine_name = engine_name.replace(
                        "Hi-Fi",
                        '<span style="color: #A078D6; font-weight: bold;">Hi-Fi</span>',
                    )

                    audio_file_info = File(self.selected_flac, easy=False).info
                    samplerate, channels = (
                        audio_file_info.sample_rate,
                        audio_file_info.channels,
                    )
                    samplerate_str = (
                        f"{samplerate / 1000.0:.1f}kHz"
                        if samplerate >= 1000
                        else f"{samplerate} Hz"
                    )
                    bits_str = (
                        f"{audio_file_info.bits_per_sample} Bit"
                        if hasattr(audio_file_info, "bits_per_sample")
                        else ""
                    )
                    channels_str = "Stereo" if channels == 2 else "Mono"
                    quality_info = f"[{engine_name}] " + " / ".join(
                        filter(None, [samplerate_str, bits_str, channels_str])
                    )
                    self.audio_quality_label.setText(quality_info)
                except Exception:
                    self.audio_quality_label.setText("음질 정보 없음")

                print(f"Using sounddevice backend to play: {self.selected_flac}")
                self.audio_player.load(self.selected_flac)
                self.audio_player.set_volume(self.current_volume)
                self.audio_player.play(start_pos)
                self.playback_start_offset_ms = int(float(start_pos) * 1000)
                self.is_paused = False
                self.update_playback_history(track_info)
                self.show_history_view()
                self.progress_slider.setMaximum(
                    int(track_info.get("raw_duration", 0) * 1000)
                )
                self.progress_slider.setEnabled(True)
                self.check_playback_timer.start()
                self.visualizer_timer.start(100)
                self.play_pause_button.setIcon(self.pause_icon)
                self.mini_player.play_pause_button.setIcon(self.pause_icon)
                self.update_now_playing()
                self.playlist_view.selectRow(self.current_row)
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "재생 오류",
                    f"음악 재생 중 오류 발생:\n{os.path.basename(self.selected_flac)}\n{e}",
                )
                self.stop_music()
                self.play_next()
            return

        # fallback to pygame-based playback
        if not self.pygame_initialized:
            return self.stop_music()
        try:
            try:
                audio_file_info = File(self.selected_flac, easy=False).info
                samplerate, channels = (
                    audio_file_info.sample_rate,
                    audio_file_info.channels,
                )
                bit_depth = (
                    8
                    if hasattr(audio_file_info, "bits_per_sample")
                    and audio_file_info.bits_per_sample == 8
                    else -16
                )
                desired_mixer = (samplerate, bit_depth, channels)
                try:
                    current_mixer = pygame.mixer.get_init()
                except Exception:
                    current_mixer = None
                if self.force_mixer_reinit or current_mixer != desired_mixer:
                    try:
                        pygame.mixer.quit()
                        pygame.mixer.init(
                            frequency=samplerate, size=bit_depth, channels=channels
                        )
                    except Exception as e:
                        print(f"mixer reinit failed: {e}")
                        try:
                            pygame.mixer.quit()
                            pygame.mixer.init()
                        except Exception:
                            pass
                try:
                    self._mixer_settings = pygame.mixer.get_init()
                except Exception:
                    self._mixer_settings = None
                samplerate_str = (
                    f"{samplerate / 1000.0:.1f}kHz"
                    if samplerate >= 1000
                    else f"{samplerate} Hz"
                )
                bits_str = (
                    f"{audio_file_info.bits_per_sample} Bit"
                    if hasattr(audio_file_info, "bits_per_sample")
                    else ""
                )
                channels_str = "Stereo" if channels == 2 else "Mono"
                quality_info = " / ".join(
                    filter(None, [samplerate_str, bits_str, channels_str])
                )
                self.audio_quality_label.setText(quality_info)
            except Exception as e:
                print(f"오디오 메타데이터 로드/mixer 재초기화 실패: {e}")
                try:
                    pygame.mixer.quit()
                    pygame.mixer.init()
                except Exception:
                    pass
                try:
                    self._mixer_settings = pygame.mixer.get_init()
                except Exception:
                    self._mixer_settings = None
                self.audio_quality_label.setText("음질 정보 없음")

            pygame.mixer.music.load(self.selected_flac)
            pygame.mixer.music.play(start=start_pos)
            self.playback_start_offset_ms = int(float(start_pos) * 1000)
            self.is_paused = False
            self.update_playback_history(track_info)
            self.show_history_view()
            self.progress_slider.setMaximum(
                int(track_info.get("raw_duration", 0) * 1000)
            )
            self.progress_slider.setEnabled(True)
            self.check_playback_timer.start()
            self.visualizer_timer.start(100)
            self.play_pause_button.setIcon(self.pause_icon)
            self.mini_player.play_pause_button.setIcon(self.pause_icon)
            self.update_now_playing()
            self.playlist_view.selectRow(self.current_row)
            if self.is_muted:
                pygame.mixer.music.set_volume(self.current_volume)
            else:
                pygame.mixer.music.set_volume(self.current_volume)

        except pygame.error as e:
            QMessageBox.critical(
                self,
                "재생 오류",
                f"음악 재생 중 오류 발생:\n{os.path.basename(self.selected_flac)}\n{e}",
            )
            self.stop_music()
            self.play_next()

    def update_now_playing(self):
        if self.selected_flac and 0 <= self.current_row < len(self.user_playlist):
            track = self.user_playlist[self.current_row]
            full_text = (
                f"{track.get('artist', 'Unknown')} - {track.get('title', 'Unknown')}"
            )
            self.now_playing_label.setText(self.truncate_text(full_text, 40))
            self.now_playing_label.setToolTip(full_text)
            self.lyrics_text.setText(
                track.get("lyrics", "가사 없음").strip() or "가사 없음"
            )
            self.show_album_cover(track.get("path"))
            self.show_album_cover(
                track.get("path"), target_label=self.mini_player.cover_label
            )
            self.mini_player.title_label.setText(
                self.truncate_text(track.get("title", "곡 정보 없음"), 25)
            )
            self.mini_player.title_label.setToolTip(track.get("title", ""))
            self.mini_player.artist_label.setText(
                self.truncate_text(track.get("artist", "아티스트 정보 없음"), 25)
            )
            self.mini_player.artist_label.setToolTip(track.get("artist", ""))
        else:
            self.now_playing_label.setText("재생 중인 곡이 없습니다.")
            self.now_playing_label.setToolTip("")
            self.lyrics_text.setText("재생 중인 곡이 없습니다.")
            self.audio_quality_label.setText("")
            self.mini_player.title_label.setText("재생 중인 곡 없음")
            self.mini_player.artist_label.setText("아티스트")
            self.mini_player.cover_label.clear()
            self.mini_player.cover_label.setText("")

    def play_prev(self):
        if not self.user_playlist:
            return
        if self.is_shuffled:
            self.current_row = random.randrange(len(self.user_playlist))
        else:
            self.current_row = (self.current_row - 1 + len(self.user_playlist)) % len(
                self.user_playlist
            )
        self.play_music()

    def play_next(self):
        if not self.user_playlist:
            return

        # 한 곡 반복 모드에서는 셔플 여부와 상관없이 현재 곡 재생
        if self.repeat_mode == 2:
            if self.pygame_initialized and pygame.mixer.music.get_busy():
                pygame.mixer.music.fadeout(500)
                QTimer.singleShot(500, self.play_music)
            else:
                self.play_music()
            return

        # 다음 곡 결정: 셔플이면 랜덤, 아니면 순차
        if self.is_shuffled:
            next_row = random.randrange(len(self.user_playlist))
        else:
            next_row = self.current_row + 1
            if next_row >= len(self.user_playlist):
                if self.repeat_mode == 1:
                    next_row = 0
                else:
                    return self.stop_music()

        self.current_row = next_row
        if self.pygame_initialized and pygame.mixer.music.get_busy():
            pygame.mixer.music.fadeout(500)
            QTimer.singleShot(500, self.play_music)
        else:
            self.play_music()

    def stop_music(self):
        # stop active backend
        if getattr(self, "use_sounddevice", False) and self.audio_player:
            try:
                self.audio_player.stop()
            except Exception:
                pass
        if self.pygame_initialized:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        if self.check_playback_timer.isActive():
            self.check_playback_timer.stop()
        if self.visualizer_timer.isActive():
            self.visualizer_timer.stop()
        self.is_paused = False
        self.play_pause_button.setIcon(self.play_icon)
        self.mini_player.play_pause_button.setIcon(self.play_icon)
        self.update_now_playing()
        self.cover_label.clear()
        self.cover_label.setText("앨범 커버 없음")
        self.progress_slider.setValue(0)
        self.progress_slider.setEnabled(False)
        self.time_label.setText("00:00 / 00:00")
        self.audio_quality_label.setText("")
        self.playback_start_offset_ms = 0
        if not self.is_muted:
            self.volume_icon.setPixmap(
                self.volume_pixmap.scaled(24, 24, Qt.KeepAspectRatio)
            )

    def closeEvent(self, event):
        self.save_playlist()
        self.save_history()
        self.save_playback_state()
        if self.pygame_initialized:
            pygame.mixer.quit()
        if getattr(self, "use_sounddevice", False) and self.audio_player:
            try:
                self.audio_player.stop()
            except Exception:
                pass
        QApplication.quit()
        event.accept()

    def save_playlist(self):
        try:
            write_json_file("user_playlist.json", self.user_playlist)
        except Exception as e:
            print(f"Error saving playlist: {e}")

    def load_playlist(self):
        try:
            if os.path.exists("user_playlist.json"):
                data = read_json_file("user_playlist.json", []) or []
                self.user_playlist = [
                    track for track in data if os.path.exists(track.get("path", ""))
                ]
                self.show_playlist_view()
        except (FileNotFoundError, json.JSONDecodeError):
            self.user_playlist = []

    def save_history(self):
        try:
            write_json_file("history.json", self.playback_history)
        except Exception as e:
            print(f"Error saving history: {e}")

    def load_history(self):
        try:
            data = read_json_file("history.json", []) or []
            self.playback_history = data
        except (FileNotFoundError, json.JSONDecodeError):
            self.playback_history = []

    def save_last_folder(self, folder):
        try:
            merge_json_file("player_settings.json", {"last_folder": folder})
        except Exception as e:
            print(f"Error saving last folder: {e}")

    def on_tab_changed(self, index):
        if index == 2:
            self.show_history_view()
        else:
            self.switch_view({0: "artists", 1: "all_tracks"}.get(index))

    def update_time_label_on_move(self, position):
        if self.selected_flac and 0 <= self.current_row < len(self.user_playlist):
            track = self.user_playlist[self.current_row]
            duration = track.get("raw_duration", 0)
            if duration > 0:
                current_time = ScanWorker._format_duration(position / 1000.0)
                total_time = ScanWorker._format_duration(duration)
                self.time_label.setText(f"{current_time} / {total_time}")

    def set_playback_position(self):
        if self.selected_flac:
            self.play_music(start_pos=self.progress_slider.value() / 1000.0)

    def update_progress_slider(self):
        try:
            if getattr(self, "use_sounddevice", False) and self.audio_player:
                if not self.audio_player.is_busy():
                    return
                pos_ms = self.audio_player.get_pos_ms()
            else:
                if not self.pygame_initialized or not pygame.mixer.music.get_busy():
                    return
                pos_ms = pygame.mixer.music.get_pos()

            if pos_ms >= 0:
                absolute_ms = int(pos_ms + self.playback_start_offset_ms)
                if not self.progress_slider.isSliderDown():
                    self.progress_slider.setValue(absolute_ms)
                self.update_time_label_on_move(absolute_ms)
        except Exception:
            self.stop_music()

    def check_playback_status(self):
        if not self.user_playlist or self.current_row == -1:
            return
        try:
            if getattr(self, "use_sounddevice", False) and self.audio_player:
                busy = self.audio_player.is_busy()
            else:
                busy = (
                    pygame.mixer.music.get_busy() if self.pygame_initialized else False
                )

            if not busy and not self.is_paused and self.selected_flac:
                self.play_next()
        except Exception:
            self.stop_music()
        self.update_progress_slider()

    def update_playback_history(self, track_info):
        if track_info in self.playback_history:
            self.playback_history.remove(track_info)
        self.playback_history.insert(0, track_info)
        self.playback_history = self.playback_history[: self.history_max_size]
        self.save_history()

    def search_music_in_view(self, text, view_type):
        if not text:
            if view_type == "artists":
                return self.show_artists_view()
            elif view_type == "all_tracks":
                return self.show_all_tracks_view()
        # prefer DB-backed search (FTS) for performance on large libraries
        results = self.search_tracks_db(text)

        if view_type == "artists":
            unique_artists = {
                track["artist"]: track for track in results if "artist" in track
            }
            filtered_artists = sorted(list(unique_artists.keys()), key=locale.strxfrm)
            self.artists_list_view.clear()
            for artist_name in filtered_artists:
                item = QListWidgetItem()
                item.setSizeHint(QSize(220, 270))
                newest_album_path = self.find_newest_album_cover_path(artist_name)
                widget = AlbumIconWidget(newest_album_path, artist_name, "", "", self)
                item.setData(Qt.UserRole, artist_name)
                self.artists_list_view.addItem(item)
                self.artists_list_view.setItemWidget(item, widget)
        elif view_type == "all_tracks":
            self.current_playlist = results
            self.display_all_tracks_table()

    def search_tracks_db(self, text):
        """Search tracks using FTS if available, otherwise fallback to LIKE queries."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # try FTS search first
            try:
                cursor.execute(
                    "SELECT t.* FROM tracks t JOIN tracks_fts f ON t.path = f.path WHERE tracks_fts MATCH ? ORDER BY artist, date, album",
                    (text,),
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
            except Exception:
                # fallback to simple LIKE search
                like = f"%{text}%"
                cursor.execute(
                    "SELECT * FROM tracks WHERE artist LIKE ? OR album LIKE ? OR title LIKE ? ORDER BY artist, date, album",
                    (like, like, like),
                )
                rows = cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def play_music_from_playlist(self, item):
        self.current_row = self.playlist_view.row(item)
        self.play_music()

    def play_music_from_table(self, item):
        track_info = self.track_table.item(item.row(), 0).data(Qt.UserRole)
        if not track_info:
            return
        if track_info not in self.user_playlist:
            self.user_playlist.append(track_info)
        self.current_row = self.user_playlist.index(track_info)
        self.show_playlist_view()
        self.save_playlist()
        self.play_music()

    def show_context_menu_playlist(self, position):
        menu = QMenu(self)
        if self.playlist_view.selectedItems():
            remove_action = QAction("재생목록에서 삭제", self)
            self.apply_font(remove_action, "Medium")
            remove_action.triggered.connect(self.remove_selected_from_playlist)
            menu.addAction(remove_action)
        clear_action = QAction("재생목록 비우기", self)
        self.apply_font(clear_action, "Medium")
        clear_action.triggered.connect(self.clear_playlist)
        menu.addAction(clear_action)
        menu.exec_(self.playlist_view.mapToGlobal(position))

    def clear_playlist(self):
        self.stop_music()
        self.user_playlist = []
        self.current_row = -1
        self.selected_flac = None
        self.show_playlist_view()
        self.save_playlist()

    def show_context_menu(self, position):
        sender = self.sender()
        menu = QMenu(self)
        if not sender.selectedItems():
            return

        add_action = QAction("재생목록에 추가", self)
        self.apply_font(add_action, "Medium")

        tracks_to_add = []
        if sender == self.artists_list_view:
            item_data = sender.selectedItems()[0].data(Qt.UserRole)
            if isinstance(item_data, str):  # 아티스트 뷰
                artist_name = item_data
                tracks_to_add = sorted(
                    [t for t in self.music_data if t.get("artist") == artist_name],
                    key=lambda x: (
                        x.get("date", "0000"),
                        locale.strxfrm(x.get("album", "")),
                        int(x.get("track", "0")),
                    ),
                )
            elif isinstance(item_data, dict):  # 앨범 뷰
                album_info = item_data
                tracks_to_add = sorted(
                    [
                        t
                        for t in self.music_data
                        if t.get("artist") == album_info.get("artist")
                        and t.get("album") == album_info.get("album")
                    ],
                    key=lambda x: int(x.get("track", "0")),
                )
        elif sender == self.track_table:
            unique_tracks = {item.row() for item in sender.selectedItems()}
            for row in unique_tracks:
                track_info = sender.item(row, 0).data(Qt.UserRole)
                if track_info:
                    tracks_to_add.append(track_info)

        if tracks_to_add:
            add_action.triggered.connect(
                lambda: self.add_tracks_to_playlist(tracks_to_add)
            )
            menu.addAction(add_action)
            menu.exec_(sender.mapToGlobal(position))

    def add_tracks_to_playlist(self, tracks):
        existing_paths = {t["path"] for t in self.user_playlist}
        new_tracks = [t for t in tracks if t.get("path") not in existing_paths]
        self.user_playlist.extend(new_tracks)
        self.show_playlist_view()
        self.save_playlist()

    def remove_selected_from_playlist(self):
        selected_rows = sorted(
            set(index.row() for index in self.playlist_view.selectedIndexes()),
            reverse=True,
        )
        for row in selected_rows:
            del self.user_playlist[row]
        self.show_playlist_view()
        self.save_playlist()

    def sort_all_tracks_table(self, logical_index):
        if self.current_sort_column == logical_index:
            self.sort_ascending = not self.sort_ascending
        else:
            self.current_sort_column, self.sort_ascending = logical_index, True

        sort_key = ["title", "artist", "duration"][logical_index]
        self.current_playlist.sort(
            key=lambda x: locale.strxfrm(str(x.get(sort_key, ""))),
            reverse=not self.sort_ascending,
        )
        self.display_all_tracks_table()

    def switch_view(self, view_type):
        self.current_view = view_type
        self.current_artist, self.current_album = None, None
        self.back_button.setEnabled(False)
        if view_type == "artists":
            self.show_artists_view()
        elif view_type == "all_tracks":
            self.show_all_tracks_view()

    def show_artists_view(self):
        self.current_view = "artists"
        self.artists_list_view.clear()
        unique_artists = sorted(
            list(self.artist_albums_covers.keys()), key=locale.strxfrm
        )
        for artist_name in unique_artists:
            item = QListWidgetItem()
            item.setSizeHint(QSize(220, 270))
            newest_album_path = self.find_newest_album_cover_path(artist_name)
            widget = AlbumIconWidget(newest_album_path, artist_name, "", "", self)
            item.setData(Qt.UserRole, artist_name)
            self.artists_list_view.addItem(item)
            self.artists_list_view.setItemWidget(item, widget)

    def find_newest_album_cover_path(self, artist_name):
        if artist_name not in self.artist_albums_covers:
            return None
        newest_album = max(
            self.artist_albums_covers[artist_name].values(),
            key=lambda x: x.get("date", "0000"),
            default=None,
        )
        return newest_album.get("path") if newest_album else None

    def show_all_tracks_view(self, data_to_show=None):
        self.current_view = "all_tracks"
        self.back_button.setEnabled(False)
        self.current_playlist = (
            data_to_show if data_to_show is not None else self.music_data
        )
        self.sort_all_tracks_table(1)  # Default sort by artist

    def display_all_tracks_table(self):
        self.track_table.setRowCount(0)
        self.track_table.setSortingEnabled(False)
        current_album_artist = None
        for track in self.current_playlist:
            row_pos = self.track_table.rowCount()
            album_artist = (track.get("album"), track.get("artist"))
            if album_artist != current_album_artist:
                current_album_artist = album_artist
                self.track_table.insertRow(row_pos)
                album_item = QTableWidgetItem(track.get("album", "Unknown Album"))
                if "ExtraBold" in self.pretendard_fonts:
                    album_font = self.pretendard_fonts["ExtraBold"]
                    album_font.setPointSize(12)
                    album_item.setFont(album_font)
                album_item.setForeground(QColor("#6E41C8"))
                album_item.setBackground(QColor(245, 240, 250))
                album_item.setFlags(Qt.ItemIsEnabled)
                self.track_table.setSpan(row_pos, 0, 1, 3)
                self.track_table.setItem(row_pos, 0, album_item)
                row_pos += 1

            self.track_table.insertRow(row_pos)
            title = QTableWidgetItem("  " + track.get("title", ""))
            artist = QTableWidgetItem(track.get("artist", ""))
            duration = QTableWidgetItem(track.get("duration", "--:--"))
            for itm in (title, artist, duration):
                self.apply_font(itm, "Medium")
            title.setData(Qt.UserRole, track)
            self.track_table.setItem(row_pos, 0, title)
            self.track_table.setItem(row_pos, 1, artist)
            self.track_table.setItem(row_pos, 2, duration)
        self.track_table.setSortingEnabled(False)

    def show_history_view(self):
        self.history_list.clear()
        if not self.playback_history:
            return self.history_list.addItem("재생 기록이 없습니다.")
        for track in self.playback_history:
            item = QListWidgetItem(
                f"{track.get('artist', '')} - {track.get('title', '')}"
            )
            self.apply_font(item, "Medium")
            self.history_list.addItem(item)

    def show_playlist_view(self):
        self.playlist_view.setRowCount(0)
        for i, track in enumerate(self.user_playlist):
            self.playlist_view.insertRow(i)
            title = QTableWidgetItem(track.get("title", ""))
            artist = QTableWidgetItem(track.get("artist", ""))
            duration = QTableWidgetItem(track.get("duration", "--:--"))
            for itm in (title, artist, duration):
                self.apply_font(itm, "Medium")
            self.playlist_view.setItem(i, 0, title)
            self.playlist_view.setItem(i, 1, artist)
            self.playlist_view.setItem(i, 2, duration)

    def on_item_double_clicked(self, item):
        if self.current_view == "artists":
            artist_name = item.data(Qt.UserRole)
            if artist_name:
                self.show_artist_albums_of_artist(artist_name)
        elif self.current_view == "artist_albums":
            album_info = item.data(Qt.UserRole)
            self.user_playlist = sorted(
                [
                    t
                    for t in self.music_data
                    if t.get("artist") == album_info["artist"]
                    and t.get("album") == album_info["album"]
                ],
                key=lambda x: int(x.get("track", "0")),
            )
            self.show_playlist_view()
            self.save_playlist()
            self.current_row = 0
            self.play_music()

    def play_all_tracks(self):
        self.user_playlist = sorted(
            self.music_data,
            key=lambda x: (
                locale.strxfrm(x.get("artist", "")),
                x.get("date", "0000"),
                locale.strxfrm(x.get("album", "")),
            ),
        )
        self.show_playlist_view()
        self.save_playlist()
        self.current_row = 0
        self.play_music()

    def show_artist_albums_of_artist(self, artist):
        self.current_artist, self.current_view = artist, "artist_albums"
        self.back_button.setEnabled(True)
        self.artists_list_view.clear()
        albums = {
            (t.get("album"), t.get("date")): t
            for t in self.music_data
            if t.get("artist") == artist
        }
        sorted_albums = sorted(
            albums.values(), key=lambda x: x.get("date", "0000"), reverse=True
        )
        for album_info in sorted_albums:
            item = QListWidgetItem()
            item.setSizeHint(QSize(220, 270))
            widget = AlbumIconWidget(
                album_info.get("path"),
                album_info.get("album"),
                artist,
                album_info.get("date"),
                self,
            )
            item.setData(Qt.UserRole, album_info)
            self.artists_list_view.addItem(item)
            self.artists_list_view.setItemWidget(item, widget)

    def go_back(self):
        if self.current_view == "artist_albums":
            self.switch_view("artists")


if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        player = MetadataMusicPlayer()
        player.show()
        # Auto-play support for testing via environment variable
        autoplay = os.environ.get("NVPLAYER_AUTO_PLAY")
        if autoplay and os.path.exists(autoplay):
            try:
                audio_info = File(autoplay)
                raw_duration = (
                    audio_info.info.length
                    if hasattr(audio_info, "info")
                    and hasattr(audio_info.info, "length")
                    else 0
                )
            except Exception:
                raw_duration = 0
            track = {
                "path": autoplay,
                "title": os.path.basename(autoplay),
                "artist": "",
                "raw_duration": raw_duration,
                "duration": ScanWorker._format_duration(raw_duration),
            }
            player.user_playlist = [track]
            player.current_row = 0
            QTimer.singleShot(500, lambda: player.play_music())
        sys.exit(app.exec_())
    except Exception as e:
        traceback.print_exc()
