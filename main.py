import sys
import os
import json
import base64
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QListWidgetItem, QSlider,
    QFileDialog, QMessageBox, QInputDialog, QStyle, QSplitter, QMenu,
    QAbstractItemView, QStackedWidget, QDialog, QDialogButtonBox,
    QLineEdit, QPlainTextEdit, QFormLayout
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtCore import (
    Qt, QUrl, QTime, QSize, QBuffer, QIODevice, pyqtSignal, QTimer
)
from PyQt6.QtGui import (
    QKeySequence, QShortcut, QPixmap, QPainter, QPainterPath
)

from mutagen import File as MutagenFile
from mutagen.flac import Picture as FlacPicture, FLAC
from mutagen.id3 import ID3, APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.asf import ASF


SETTINGS_FILE    = Path.home() / ".mp3_player.json"
SETTINGS_VERSION = 3
COVER_SIZE       = 48
PL_COVER_SIZE    = 46
PL_COVER_SAVE    = 128
HEADER_COVER     = 96    # was 72 — playlist page top is now bigger
EDIT_COVER       = 110
TRACK_EDIT_COVER = 130
NUM_WIDTH        = 30
DUR_WIDTH        = 55

_EXT_COVER_NAMES = ("cover", "folder", "front", "album",
                    "albumart", "artwork", "thumb")
_EXT_COVER_EXTS  = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
_AUDIO_EXTS      = (".mp3", ".wav", ".ogg", ".oga", ".opus",
                    ".flac", ".m4a", ".aac", ".wma")

_DEBUG = bool(os.environ.get("MP3PLAYER_DEBUG"))


def _dbg(*a):
    if _DEBUG:
        print("[metadata]", *a)


def format_duration_seconds(secs) -> str:
    try:
        s = int(round(float(secs)))
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ==================================================================
#  Rounded pixmap helpers
# ==================================================================
def rounded_cover(pix: QPixmap | None, size: int, radius: int) -> QPixmap | None:
    """Scale `pix` to *fill* a `size`×`size` square, center-crop,
    and clip to a rounded rectangle. Returns None if input is None/empty."""
    if pix is None or pix.isNull():
        return None

    scaled = pix.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    # center-crop to a square
    x = max(0, (scaled.width() - size) // 2)
    y = max(0, (scaled.height() - size) // 2)
    cropped = scaled.copy(x, y, size, size)

    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)

    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(size), float(size),
                        float(radius), float(radius))
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, cropped)
    painter.end()
    return result


def apply_rounded_cover(label: QLabel, pix: QPixmap | None,
                        size: int, radius: int,
                        placeholder_pt: int = 20) -> None:
    """Set a rounded cover on `label`; fall back to a rounded ♪ placeholder."""
    if pix is not None and not pix.isNull():
        rounded = rounded_cover(pix, size, radius)
        if rounded is not None:
            label.setPixmap(rounded)
            label.setText("")
            label.setStyleSheet("background: transparent; border: none;")
            return
    # placeholder
    label.clear()
    label.setText("♪")
    f = label.font()
    f.setPointSize(placeholder_pt)
    label.setFont(f)
    label.setStyleSheet(
        f"background: palette(mid); border-radius: {radius}px;"
        "color: palette(placeholder-text);"
    )


# ==================================================================
#  Metadata + cover extraction
# ==================================================================
def extract_cover(audio) -> bytes | None:
    tags = getattr(audio, "tags", None)

    if tags is not None and hasattr(tags, "getall"):
        for frame_id in ("APIC", "PIC"):
            try:
                frames = tags.getall(frame_id)
            except Exception:
                continue
            if not frames:
                continue
            for f in frames:
                if getattr(f, "type", None) == 3:
                    return bytes(f.data)
            return bytes(frames[0].data)

    if tags is not None:
        try:
            covr = tags.get("covr") if hasattr(tags, "get") else None
            if covr:
                return bytes(covr[0])
        except Exception:
            pass
        try:
            mbp = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
            if mbp:
                raw = mbp[0]
                if isinstance(raw, str):
                    raw = raw.encode("ascii")
                pic = FlacPicture(base64.b64decode(raw))
                return bytes(pic.data)
        except Exception:
            pass
        try:
            wmp = tags.get("WM/Picture") if hasattr(tags, "get") else None
            if wmp:
                raw = getattr(wmp[0], "value", None)
                if raw:
                    data = _strip_wma_picture_header(bytes(raw))
                    if data:
                        return data
        except Exception:
            pass

    pics = getattr(audio, "pictures", None)
    if pics:
        try:
            return bytes(pics[0].data)
        except Exception:
            pass

    return None


def _strip_wma_picture_header(raw: bytes) -> bytes | None:
    if len(raw) < 5:
        return None
    size = int.from_bytes(raw[1:5], "little")
    i = 5
    while i + 1 < len(raw) and raw[i:i+2] != b"\x00\x00":
        i += 2
    i += 2
    while i + 1 < len(raw) and raw[i:i+2] != b"\x00\x00":
        i += 2
    i += 2
    img = raw[i:i + size] if size else raw[i:]
    return img or None


def find_external_cover(audio_path: str) -> bytes | None:
    folder = Path(audio_path).parent
    stem = Path(audio_path).stem
    candidates: list[Path] = []
    for ext in _EXT_COVER_EXTS:
        candidates.append(folder / f"{stem}{ext}")
    for name in _EXT_COVER_NAMES:
        for ext in _EXT_COVER_EXTS:
            candidates.append(folder / f"{name}{ext}")
    for c in candidates:
        try:
            if c.is_file():
                return c.read_bytes()
        except Exception:
            continue
    return None


def read_metadata(path: str) -> dict:
    result = {
        "title": Path(path).stem,
        "artist": "",
        "album": "",
        "cover": None,
        "duration": 0.0,
        "missing": not os.path.exists(path),
    }
    if result["missing"]:
        return result

    try:
        easy = MutagenFile(path, easy=True)
        if easy is not None and easy.tags:
            def first(key: str) -> str:
                v = easy.tags.get(key)
                return str(v[0]) if v else ""
            t = first("title")
            if t:  result["title"] = t
            a = first("artist")
            if a:  result["artist"] = a
            al = first("album")
            if al: result["album"] = al
    except Exception as e:
        _dbg(f"easy tags failed for {path}: {e}")

    try:
        audio = MutagenFile(path)
        if audio is not None:
            result["cover"] = extract_cover(audio)
            info = getattr(audio, "info", None)
            if info is not None:
                length = getattr(info, "length", None)
                if length:
                    result["duration"] = float(length)
    except Exception as e:
        _dbg(f"cover/duration extraction failed for {path}: {e}")

    if not result["cover"]:
        result["cover"] = find_external_cover(path)

    return result


# ==================================================================
#  Metadata WRITING
# ==================================================================
def _guess_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def write_track_metadata(path: str, title: str, artist: str, album: str,
                         cover_bytes: bytes | None) -> tuple[bool, str]:
    if not os.path.exists(path):
        return False, "File not found."
    if not os.access(path, os.W_OK):
        return False, "File is not writable (check permissions)."

    try:
        easy = MutagenFile(path, easy=True)
        if easy is None:
            return False, "Unsupported file format."
        if easy.tags is None:
            easy.add_tags()
        for key, value in (("title", title), ("artist", artist), ("album", album)):
            value = value.strip()
            if value:
                easy[key] = [value]
            else:
                if key in easy:
                    del easy[key]
        easy.save()
    except Exception as e:
        return False, f"Could not write text tags:\n{e}"

    try:
        audio = MutagenFile(path)
    except Exception as e:
        return False, f"Could not reopen file for cover write:\n{e}"
    if audio is None:
        return False, "Unsupported file format."

    try:
        if isinstance(audio, MP3):
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            if cover_bytes:
                audio.tags.add(APIC(
                    encoding=3,
                    mime=_guess_image_mime(cover_bytes),
                    type=3,
                    desc="Cover",
                    data=cover_bytes,
                ))
            audio.save()
            return True, ""

        if isinstance(audio, FLAC):
            audio.clear_pictures()
            if cover_bytes:
                pic = FlacPicture()
                pic.type = 3
                pic.mime = _guess_image_mime(cover_bytes)
                pic.desc = "Cover"
                pic.data = cover_bytes
                audio.add_picture(pic)
            audio.save()
            return True, ""

        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            if not cover_bytes:
                if "covr" in audio.tags:
                    del audio.tags["covr"]
            else:
                fmt = (MP4Cover.FORMAT_PNG
                       if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n"
                       else MP4Cover.FORMAT_JPEG)
                audio.tags["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
            audio.save()
            return True, ""

        if isinstance(audio, (OggVorbis, OggOpus)):
            if audio.tags is None:
                audio.add_tags()
            if not cover_bytes:
                if "metadata_block_picture" in audio.tags:
                    del audio.tags["metadata_block_picture"]
            else:
                pic = FlacPicture()
                pic.type = 3
                pic.mime = _guess_image_mime(cover_bytes)
                pic.desc = "Cover"
                pic.data = cover_bytes
                audio["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]
            audio.save()
            return True, ""

        if isinstance(audio, ASF):
            if cover_bytes:
                return True, ("Text tags saved.\n"
                              "Cover writing is not supported for WMA files.")
            return True, ""

        if cover_bytes:
            return True, ("Text tags saved.\n"
                          "Cover writing is not supported for this format.")
        return True, ""

    except Exception as e:
        return False, f"Could not write cover:\n{e}"


# ==================================================================
#  Playlist cover helpers
# ==================================================================
def encode_pixmap_b64(pix: QPixmap) -> str | None:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pix.save(buf, "PNG"):
        return None
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def decode_b64_pixmap(b64: str | None) -> QPixmap | None:
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
    except Exception:
        return None
    pix = QPixmap()
    return pix if pix.loadFromData(data) else None


def scaled_pixmap_from_file(path: str, size: int) -> QPixmap | None:
    src = QPixmap(path)
    if src.isNull():
        return None
    return src.scaled(size, size,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


# ==================================================================
#  Custom song list
# ==================================================================
class SongListWidget(QListWidget):
    files_dropped  = pyqtSignal(list)
    rows_reordered = pyqtSignal(list, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._drag_rows: list[int] = []

    @staticmethod
    def _audio_paths(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            p = url.toLocalFile()
            if not p:
                continue
            if os.path.isfile(p) and Path(p).suffix.lower() in _AUDIO_EXTS:
                out.append(os.path.abspath(p))
        return out

    def _drop_target_row(self, event) -> int:
        pos = event.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.count()
        indicator = self.dropIndicatorPosition()
        if indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
            return idx.row() + 1
        return idx.row()

    def startDrag(self, supported_actions):
        self._drag_rows = sorted({i.row() for i in self.selectedIndexes()})
        super().startDrag(supported_actions)
        self._drag_rows = []

    def dragEnterEvent(self, event):
        if event.source() is self or self._audio_paths(event.mimeData()):
            event.acceptProposedAction(); return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.source() is self or self._audio_paths(event.mimeData()):
            event.acceptProposedAction(); return
        event.ignore()

    def dropEvent(self, event):
        if event.source() is self:
            rows = sorted({i.row() for i in self.selectedIndexes()})
            if not rows and self._drag_rows:
                rows = list(self._drag_rows)
            if rows:
                drop_row = self._drop_target_row(event)
                QTimer.singleShot(
                    0, lambda r=list(rows), d=drop_row:
                        self.rows_reordered.emit(r, d)
                )
            event.acceptProposedAction()
            return

        paths = self._audio_paths(event.mimeData())
        if paths:
            QTimer.singleShot(
                0, lambda p=list(paths): self.files_dropped.emit(p)
            )
            event.acceptProposedAction()
            return
        event.ignore()


# ==================================================================
#  Row widgets
# ==================================================================
class TrackRow(QWidget):
    def __init__(self, number: int, meta: dict,
                 cover: QPixmap | None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 10, 4)
        lay.setSpacing(10)

        self.num_lbl = QLabel(f"{number:02d}")
        self.num_lbl.setFixedWidth(NUM_WIDTH)
        self.num_lbl.setAlignment(Qt.AlignmentFlag.AlignRight |
                                  Qt.AlignmentFlag.AlignVCenter)
        self.num_lbl.setStyleSheet("color: palette(placeholder-text);")
        f = self.num_lbl.font(); f.setBold(True)
        self.num_lbl.setFont(f)
        lay.addWidget(self.num_lbl)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(COVER_SIZE, COVER_SIZE)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_rounded_cover(self.cover_lbl, cover,
                            COVER_SIZE, radius=6, placeholder_pt=20)
        lay.addWidget(self.cover_lbl)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)

        title = meta["title"] or "Unknown title"
        if meta.get("missing"):
            title += "   ⚠ file missing"
        self.title_lbl = QLabel(title)
        f = self.title_lbl.font(); f.setBold(True)
        self.title_lbl.setFont(f)

        parts = []
        if meta.get("artist"): parts.append(meta["artist"])
        if meta.get("album"):  parts.append(meta["album"])
        self.sub_lbl = QLabel("  •  ".join(parts) if parts else "Unknown artist")
        self.sub_lbl.setStyleSheet("color: palette(placeholder-text);")
        f2 = self.sub_lbl.font(); f2.setPointSize(max(8, f2.pointSize() - 1))
        self.sub_lbl.setFont(f2)

        col.addWidget(self.title_lbl)
        col.addWidget(self.sub_lbl)
        lay.addLayout(col, stretch=1)

        dur_text = format_duration_seconds(meta.get("duration"))
        self.dur_lbl = QLabel(dur_text)
        self.dur_lbl.setFixedWidth(DUR_WIDTH)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight |
                                  Qt.AlignmentFlag.AlignVCenter)
        self.dur_lbl.setStyleSheet("color: palette(placeholder-text);")
        f3 = self.dur_lbl.font()
        try:
            f3.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "monospace"])
        except AttributeError:
            pass
        self.dur_lbl.setFont(f3)
        lay.addWidget(self.dur_lbl)

        self.setMinimumHeight(COVER_SIZE + 12)


class PlaylistRow(QWidget):
    def __init__(self, name: str, cover: QPixmap | None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(8)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(PL_COVER_SIZE, PL_COVER_SIZE)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_rounded_cover(self.cover_lbl, cover,
                            PL_COVER_SIZE, radius=10, placeholder_pt=18)
        lay.addWidget(self.cover_lbl)

        self.name_lbl = QLabel(name)
        f = self.name_lbl.font()
        f.setPointSize(max(10, f.pointSize()))
        self.name_lbl.setFont(f)
        lay.addWidget(self.name_lbl, 1)

        self.setMinimumHeight(PL_COVER_SIZE + 8)


class PlaylistHeader(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 12, 6, 12)
        lay.setSpacing(14)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(HEADER_COVER, HEADER_COVER)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.cover_lbl)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        self.name_lbl = QLabel("No playlist selected")
        f = self.name_lbl.font(); f.setBold(True); f.setPointSize(18)
        self.name_lbl.setFont(f)
        self.name_lbl.setWordWrap(True)

        self.sub_lbl = QLabel("")
        self.sub_lbl.setStyleSheet("color: palette(placeholder-text);")

        self.desc_lbl = QLabel("")
        self.desc_lbl.setStyleSheet("color: palette(placeholder-text);")
        self.desc_lbl.setWordWrap(True)
        fd = self.desc_lbl.font(); fd.setPointSize(max(8, fd.pointSize() - 1))
        self.desc_lbl.setFont(fd)
        self.desc_lbl.setVisible(False)

        col.addStretch(1)
        col.addWidget(self.name_lbl)
        col.addWidget(self.sub_lbl)
        col.addWidget(self.desc_lbl)
        col.addStretch(1)

        lay.addLayout(col, stretch=1)

        self.setMinimumHeight(HEADER_COVER + 24)
        apply_rounded_cover(self.cover_lbl, None,
                            HEADER_COVER, radius=14, placeholder_pt=34)

    def set_playlist(self, name: str | None, cover_pix: QPixmap | None,
                     track_count: int, description: str = ""):
        if not name:
            self.name_lbl.setText("No playlist selected")
            self.sub_lbl.setText("")
            self.desc_lbl.setVisible(False)
            apply_rounded_cover(self.cover_lbl, None,
                                HEADER_COVER, radius=14, placeholder_pt=34)
            return

        self.name_lbl.setText(name)
        if track_count == 0:
            self.sub_lbl.setText("Drop audio files here to add")
        else:
            self.sub_lbl.setText(
                f"{track_count} track{'s' if track_count != 1 else ''}")

        if description:
            self.desc_lbl.setText(description)
            self.desc_lbl.setVisible(True)
        else:
            self.desc_lbl.setVisible(False)

        apply_rounded_cover(self.cover_lbl, cover_pix,
                            HEADER_COVER, radius=14, placeholder_pt=34)


# ==================================================================
#  New playlist dialog
# ==================================================================
class NewPlaylistDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New playlist")
        self.setMinimumWidth(380)
        self._cover_b64: str | None = None

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Playlist name")
        form.addRow("Name:", self.name_edit)
        lay.addLayout(form)

        cover_row = QHBoxLayout()
        cover_row.setSpacing(10)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(90, 90)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        apply_rounded_cover(self.cover_lbl, None,
                            90, radius=12, placeholder_pt=28)
        cover_row.addWidget(self.cover_lbl)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        self.btn_choose = QPushButton("Choose image…")
        self.btn_choose.clicked.connect(self._choose_cover)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear_cover)
        btn_col.addWidget(self.btn_choose)
        btn_col.addWidget(self.btn_clear)
        btn_col.addStretch(1)
        cover_row.addLayout(btn_col)
        cover_row.addStretch(1)

        lay.addLayout(cover_row)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._try_accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.name_edit.setFocus()

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)")
        if not path:
            return
        pix = scaled_pixmap_from_file(path, PL_COVER_SAVE)
        if pix is None:
            QMessageBox.warning(self, "Could not load image",
                                "Qt could not decode this image file.")
            return
        b64 = encode_pixmap_b64(pix)
        if not b64:
            QMessageBox.warning(self, "Encode failed",
                                "Could not encode cover image.")
            return
        self._cover_b64 = b64
        apply_rounded_cover(self.cover_lbl, pix,
                            90, radius=12, placeholder_pt=28)

    def _clear_cover(self):
        self._cover_b64 = None
        apply_rounded_cover(self.cover_lbl, None,
                            90, radius=12, placeholder_pt=28)

    def _try_accept(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "Name required",
                                "Please enter a playlist name.")
            return
        self.accept()

    def name(self) -> str:
        return self.name_edit.text().strip()

    def cover_b64(self) -> str | None:
        return self._cover_b64


# ==================================================================
#  Track metadata edit dialog
# ==================================================================
class TrackEditDialog(QDialog):
    def __init__(self, path: str, meta: dict, cover_pix: QPixmap | None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit track metadata")
        self.setMinimumWidth(460)

        self._path = path
        self._cover_bytes: bytes | None = meta.get("cover")

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        name_lbl = QLabel(f"File: {Path(path).name}")
        nf = name_lbl.font(); nf.setItalic(True)
        name_lbl.setFont(nf)
        name_lbl.setStyleSheet("color: palette(placeholder-text);")
        name_lbl.setWordWrap(True)
        lay.addWidget(name_lbl)

        cover_row = QHBoxLayout()
        cover_row.setSpacing(12)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(TRACK_EDIT_COVER, TRACK_EDIT_COVER)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_row.addWidget(self.cover_lbl)

        cbtn_col = QVBoxLayout()
        cbtn_col.setSpacing(6)
        self.btn_choose_cover = QPushButton("Choose image…")
        self.btn_choose_cover.clicked.connect(self._choose_cover)
        self.btn_clear_cover = QPushButton("Clear cover")
        self.btn_clear_cover.clicked.connect(self._clear_cover)
        cbtn_col.addWidget(self.btn_choose_cover)
        cbtn_col.addWidget(self.btn_clear_cover)
        cbtn_col.addStretch(1)
        cover_row.addLayout(cbtn_col)
        cover_row.addStretch(1)
        lay.addLayout(cover_row)

        form = QFormLayout()
        form.setSpacing(8)
        self.title_edit = QLineEdit(meta.get("title", ""))
        self.title_edit.setPlaceholderText("Song title")
        self.artist_edit = QLineEdit(meta.get("artist", ""))
        self.artist_edit.setPlaceholderText("Artist name")
        self.album_edit = QLineEdit(meta.get("album", ""))
        self.album_edit.setPlaceholderText("Album name")
        form.addRow("Title:",  self.title_edit)
        form.addRow("Artist:", self.artist_edit)
        form.addRow("Album:",  self.album_edit)
        lay.addLayout(form)

        hint = QLabel("Changes are written to the audio file itself "
                      "and will be visible in other players.")
        hf = hint.font(); hf.setPointSize(max(8, hf.pointSize() - 1))
        hint.setFont(hf)
        hint.setStyleSheet("color: palette(placeholder-text);")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Save")
        ok_btn.setStyleSheet("""
            QPushButton {
                background: palette(highlight);
                color: palette(highlighted-text);
                border: 1px solid palette(highlight);
                border-radius: 14px;
                padding: 6px 22px;
                font-weight: bold;
            }
            QPushButton:hover { background: palette(highlight); }
            QPushButton:pressed { background: palette(dark); }
        """)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        apply_rounded_cover(self.cover_lbl, cover_pix,
                            TRACK_EDIT_COVER, radius=16, placeholder_pt=40)

        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover image", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except Exception as e:
            QMessageBox.warning(self, "Read failed", str(e))
            return
        test = QPixmap()
        if not test.loadFromData(data):
            QMessageBox.warning(self, "Could not load image",
                                "Qt could not decode this image file.")
            return
        self._cover_bytes = data
        apply_rounded_cover(self.cover_lbl, test,
                            TRACK_EDIT_COVER, radius=16, placeholder_pt=40)

    def _clear_cover(self):
        self._cover_bytes = None
        apply_rounded_cover(self.cover_lbl, None,
                            TRACK_EDIT_COVER, radius=16, placeholder_pt=40)

    def _on_save(self):
        title  = self.title_edit.text().strip()
        artist = self.artist_edit.text().strip()
        album  = self.album_edit.text().strip()

        if not title:
            QMessageBox.warning(self, "Title required",
                                "Please enter a song title.")
            return

        ok, err = write_track_metadata(
            self._path, title, artist, album, self._cover_bytes,
        )
        if not ok:
            QMessageBox.critical(self, "Could not save metadata", err)
            return
        if err:
            QMessageBox.information(self, "Saved with note", err)
        self.accept()


# ==================================================================
#  In-window playlist edit page
# ==================================================================
class PlaylistEditPage(QWidget):
    saved     = pyqtSignal(str, str, str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cover_b64: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        top = QHBoxLayout()
        self.btn_back = QPushButton("←  Back")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(self.cancelled.emit)
        top.addWidget(self.btn_back)
        top.addStretch(1)

        title = QLabel("Edit playlist")
        tf = title.font(); tf.setBold(True); tf.setPointSize(14)
        title.setFont(tf)
        top.addWidget(title)
        top.addStretch(1)
        top.addWidget(QLabel(" " * 6))
        root.addLayout(top)

        cover_row = QHBoxLayout()
        cover_row.setSpacing(14)

        self.cover_lbl = QLabel()
        self.cover_lbl.setFixedSize(EDIT_COVER, EDIT_COVER)
        self.cover_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cover_row.addWidget(self.cover_lbl)

        cbtn_col = QVBoxLayout()
        cbtn_col.setSpacing(6)
        self.btn_choose_cover = QPushButton("Choose image…")
        self.btn_choose_cover.clicked.connect(self._choose_cover)
        self.btn_clear_cover = QPushButton("Clear cover")
        self.btn_clear_cover.clicked.connect(self._clear_cover)
        cbtn_col.addWidget(self.btn_choose_cover)
        cbtn_col.addWidget(self.btn_clear_cover)
        cbtn_col.addStretch(1)
        cover_row.addLayout(cbtn_col)
        cover_row.addStretch(1)
        root.addLayout(cover_row)

        root.addWidget(QLabel("Name"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Playlist name")
        root.addWidget(self.name_edit)

        root.addWidget(QLabel("Description"))
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("Optional description…")
        self.desc_edit.setFixedHeight(100)
        root.addWidget(self.desc_edit)

        root.addStretch(1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancelled.emit)
        self.btn_save = QPushButton("Save")
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save.setStyleSheet("""
            QPushButton {
                background: palette(highlight);
                color: palette(highlighted-text);
                border: 1px solid palette(highlight);
                border-radius: 14px;
                padding: 6px 22px;
                font-weight: bold;
            }
            QPushButton:hover { background: palette(highlight); }
            QPushButton:pressed { background: palette(dark); }
        """)
        self.btn_cancel.setMinimumHeight(32)
        self.btn_save.setMinimumHeight(32)
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_save)
        root.addLayout(actions)

        apply_rounded_cover(self.cover_lbl, None,
                            EDIT_COVER, radius=14, placeholder_pt=34)

    def load(self, name: str, description: str, cover_b64: str | None):
        self.name_edit.setText(name)
        self.desc_edit.setPlainText(description or "")
        self._cover_b64 = cover_b64 or None
        pix = decode_b64_pixmap(self._cover_b64)
        apply_rounded_cover(self.cover_lbl, pix,
                            EDIT_COVER, radius=14, placeholder_pt=34)

    def _choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose cover", "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)")
        if not path:
            return
        pix = scaled_pixmap_from_file(path, PL_COVER_SAVE)
        if pix is None:
            QMessageBox.warning(self, "Could not load image",
                                "Qt could not decode this image file.")
            return
        b64 = encode_pixmap_b64(pix)
        if not b64:
            QMessageBox.warning(self, "Encode failed",
                                "Could not encode cover image.")
            return
        self._cover_b64 = b64
        apply_rounded_cover(self.cover_lbl, pix,
                            EDIT_COVER, radius=14, placeholder_pt=34)

    def _clear_cover(self):
        self._cover_b64 = None
        apply_rounded_cover(self.cover_lbl, None,
                            EDIT_COVER, radius=14, placeholder_pt=34)

    def _on_save(self):
        self.saved.emit(
            self.name_edit.text().strip(),
            self.desc_edit.toPlainText().strip(),
            self._cover_b64 or "",
        )


# ==================================================================
#  Main window
# ==================================================================
class MP3Player(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MP3 Player")
        self.resize(1040, 700)

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.7)

        self.playlists: dict[str, dict] = {}
        self.current_playlist: str | None = None
        self.current_index = -1
        self.is_seeking = False

        self._editing_playlist: str | None = None
        self._row_cache: dict[str, tuple[dict, QPixmap | None]] = {}

        self._load_settings()
        if not self.playlists:
            self.playlists["Library"] = self._blank_playlist()

        self._build_ui()
        self._connect_signals()
        self._setup_shortcuts()

        self._refresh_playlist_view()
        self._select_initial_playlist()

    @staticmethod
    def _blank_playlist() -> dict:
        return {"tracks": [], "cover": None, "description": ""}

    def _tracks(self, name: str | None = None) -> list[str]:
        name = name or self.current_playlist
        if name is None:
            return []
        return self.playlists.get(name, {}).get("tracks", [])

    def _pl_cover(self, name: str | None = None) -> str | None:
        name = name or self.current_playlist
        if name is None:
            return None
        return self.playlists.get(name, {}).get("cover")

    def _pl_desc(self, name: str | None = None) -> str:
        name = name or self.current_playlist
        if name is None:
            return ""
        return self.playlists.get(name, {}).get("description", "")

    # ==============================================================
    #  UI
    # ==============================================================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        # ---------- LEFT ----------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(QLabel("Playlists"))
        header.addStretch(1)

        self.btn_add_pl = QPushButton("+")
        self.btn_add_pl.setFixedSize(26, 26)
        self.btn_add_pl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_pl.setToolTip("New playlist")
        self.btn_add_pl.setStyleSheet("""
            QPushButton {
                border: 1px solid palette(mid);
                border-radius: 13px;
                font-size: 16px; font-weight: bold;
                padding: 0px;
                background: palette(button);
            }
            QPushButton:hover {
                background: palette(highlight);
                color: palette(highlighted-text);
                border: 1px solid palette(highlight);
            }
            QPushButton:pressed { background: palette(dark); }
        """)
        header.addWidget(self.btn_add_pl)
        ll.addLayout(header)

        self.playlist_list = QListWidget()
        self.playlist_list.setAlternatingRowColors(True)
        self.playlist_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        ll.addWidget(self.playlist_list, stretch=1)

        left.setMinimumWidth(240)
        left.setMaximumWidth(320)
        splitter.addWidget(left)

        # ---------- RIGHT ----------
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.right_stack = QStackedWidget()
        rl.addWidget(self.right_stack)

        # page 0: view
        page_view = QWidget()
        pv = QVBoxLayout(page_view)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(8)

        self.playlist_header = PlaylistHeader()
        pv.addWidget(self.playlist_header)

        self.list_widget = SongListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.setToolTip(
            "Drop audio files here to add them to the current playlist.\n"
            "Drag tracks to reorder them.\n"
            "Right-click a track for more options.")
        pv.addWidget(self.list_widget, stretch=1)

        self.title_label = QLabel("No track loaded")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        pv.addWidget(self.title_label)

        progress_row = QHBoxLayout()
        self.time_label = QLabel("00:00")
        self.duration_label = QLabel("00:00")
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setRange(0, 0)
        self.progress.setStyleSheet(self._slider_qss())
        progress_row.addWidget(self.time_label)
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.duration_label)
        pv.addLayout(progress_row)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addStretch(1)

        style = self.style()
        self.btn_prev = QPushButton()
        self.btn_prev.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.btn_play = QPushButton()
        self.btn_play.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_stop = QPushButton()
        self.btn_stop.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.btn_next = QPushButton()
        self.btn_next.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward))

        for b in (self.btn_prev, self.btn_stop, self.btn_next):
            self._make_circular(b, 46, 20)
        self._make_circular(self.btn_play, 58, 26, primary=True)

        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)
        controls.addWidget(self.btn_next)
        controls.addStretch(1)

        self.vol_icon = QLabel("🔊")
        self.vol_icon.setStyleSheet("font-size: 20px;")
        controls.addWidget(self.vol_icon)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(70)
        self.volume.setFixedWidth(120)
        self.volume.setStyleSheet(self._slider_qss())
        controls.addWidget(self.volume)

        pv.addLayout(controls)
        self.right_stack.addWidget(page_view)

        # page 1: edit
        self.edit_page = PlaylistEditPage()
        self.edit_page.saved.connect(self._on_playlist_edit_saved)
        self.edit_page.cancelled.connect(self._close_edit_page)
        self.right_stack.addWidget(self.edit_page)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    @staticmethod
    def _make_circular(btn: QPushButton, diameter: int, icon_px: int,
                       primary: bool = False) -> None:
        radius = diameter // 2
        btn.setFixedSize(diameter, diameter)
        btn.setIconSize(QSize(icon_px, icon_px))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bg       = "palette(highlight)" if primary else "palette(button)"
        bg_hover = "palette(highlight)" if primary else "palette(midlight)"
        bg_dn    = "palette(dark)"
        # widget-level stylesheet — overrides the app-wide QPushButton rule
        btn.setStyleSheet(f"""
            QPushButton {{
                border-radius: {radius}px;
                background-color: {bg};
                border: 1px solid palette(mid);
                padding: 0px;
                min-height: 0px;
                min-width: 0px;
            }}
            QPushButton:hover {{
                background-color: {bg_hover};
                border: 1px solid palette(highlight);
            }}
            QPushButton:pressed {{ background-color: {bg_dn}; }}
        """)

    @staticmethod
    def _slider_qss() -> str:
        return """
            QSlider::groove:horizontal {
                height: 6px; background: palette(mid); border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: palette(highlight); border-radius: 3px;
            }
            QSlider::add-page:horizontal {
                background: palette(mid); border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: palette(highlight);
                width: 16px; height: 16px;
                margin: -6px 0;
                border-radius: 8px;
                border: 2px solid palette(base);
            }
        """

    # ==============================================================
    #  Signals
    # ==============================================================
    def _connect_signals(self):
        self.playlist_list.currentItemChanged.connect(self._on_playlist_current_changed)
        self.playlist_list.customContextMenuRequested.connect(
            self._on_playlist_context_menu)
        self.btn_add_pl.clicked.connect(self.new_playlist)

        self.list_widget.files_dropped.connect(self._on_files_dropped)
        self.list_widget.rows_reordered.connect(self._on_rows_reordered)
        self.list_widget.itemDoubleClicked.connect(self.play_index)
        self.list_widget.customContextMenuRequested.connect(
            self._on_song_context_menu)

        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_stop.clicked.connect(self.stop)
        self.btn_next.clicked.connect(self.next_track)
        self.btn_prev.clicked.connect(self.prev_track)

        self.progress.sliderPressed.connect(self._seek_start)
        self.progress.sliderReleased.connect(self._seek_end)
        self.volume.valueChanged.connect(
            lambda v: self.audio_output.setVolume(v / 100.0))

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle_play)
        QShortcut(QKeySequence("Ctrl+Right"), self, self.next_track)
        QShortcut(QKeySequence("Ctrl+Left"), self, self.prev_track)
        QShortcut(QKeySequence("Ctrl+Up"),    self, lambda: self._move_selected(-1))
        QShortcut(QKeySequence("Ctrl+Down"),  self, lambda: self._move_selected(+1))
        QShortcut(QKeySequence("Delete"),     self, self.remove_selected)
        QShortcut(QKeySequence("Ctrl+E"),     self, self._edit_current_track)

    # ==============================================================
    #  Persistence
    # ==============================================================
    def _load_settings(self):
        if not SETTINGS_FILE.exists():
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[mp3 player] could not read {SETTINGS_FILE}: {e}")
            return

        raw = None
        if isinstance(data, dict) and isinstance(data.get("playlists"), dict):
            raw = data["playlists"]
            last = data.get("last_playlist")
            self.current_playlist = last if last in raw else None
        elif isinstance(data, dict):
            raw = data

        if not isinstance(raw, dict):
            return

        for name, value in raw.items():
            name = str(name)
            if isinstance(value, list):
                tracks = [str(p) for p in value if isinstance(p, str)]
                self.playlists[name] = {
                    "tracks": tracks, "cover": None, "description": ""
                }
            elif isinstance(value, dict):
                tracks = value.get("tracks", [])
                cover = value.get("cover")
                desc = value.get("description", "")
                self.playlists[name] = {
                    "tracks": [str(p) for p in tracks if isinstance(p, str)]
                              if isinstance(tracks, list) else [],
                    "cover": cover if isinstance(cover, str) else None,
                    "description": desc if isinstance(desc, str) else "",
                }

    def _save_settings(self):
        payload = {
            "version": SETTINGS_VERSION,
            "playlists": self.playlists,
            "last_playlist": self.current_playlist,
        }
        try:
            tmp = SETTINGS_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as e:
            print(f"[mp3 player] could not write {SETTINGS_FILE}: {e}")

    def closeEvent(self, event):
        self.player.stop()
        self._save_settings()
        super().closeEvent(event)

    # ==============================================================
    #  Metadata cache
    # ==============================================================
    def _get_row_data(self, path: str) -> tuple[dict, QPixmap | None]:
        cached = self._row_cache.get(path)
        if cached is not None:
            return cached
        meta = read_metadata(path)
        pix: QPixmap | None = None
        if meta.get("cover"):
            p = QPixmap()
            if p.loadFromData(meta["cover"]):
                pix = p
        result = (meta, pix)
        self._row_cache[path] = result
        return result

    def _invalidate_cache(self, path: str):
        self._row_cache.pop(path, None)

    # ==============================================================
    #  Views
    # ==============================================================
    def _refresh_playlist_view(self):
        self.playlist_list.blockSignals(True)
        self.playlist_list.clear()
        for name in self.playlists:
            pix = decode_b64_pixmap(self._pl_cover(name))
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, name)
            row = PlaylistRow(name, pix)
            item.setSizeHint(QSize(0, row.minimumHeight()))
            self.playlist_list.addItem(item)
            self.playlist_list.setItemWidget(item, row)
            if name == self.current_playlist:
                self.playlist_list.setCurrentItem(item)
        self.playlist_list.blockSignals(False)

    def _refresh_songs_view(self):
        vbar = self.list_widget.verticalScrollBar()
        prev_scroll = vbar.value()
        was_at_bottom = prev_scroll >= vbar.maximum()

        self.list_widget.clear()

        pix = decode_b64_pixmap(self._pl_cover())
        pl = self._tracks() if self.current_playlist else []
        desc = self._pl_desc() if self.current_playlist else ""

        self.playlist_header.set_playlist(
            self.current_playlist, pix, len(pl), desc)

        if self.current_playlist is None or self.current_playlist not in self.playlists:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for i, path in enumerate(pl):
                meta, cover = self._get_row_data(path)
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, path)
                row = TrackRow(i + 1, meta, cover)
                item.setSizeHint(QSize(0, row.minimumHeight()))
                self.list_widget.addItem(item)
                self.list_widget.setItemWidget(item, row)
        finally:
            QApplication.restoreOverrideCursor()

        self.list_widget.doItemsLayout()
        self.list_widget.viewport().update()

        if was_at_bottom:
            self.list_widget.scrollToBottom()
        else:
            vbar.setValue(min(prev_scroll, vbar.maximum()))

    def _select_initial_playlist(self):
        if not self.playlists:
            return
        target = self.current_playlist if self.current_playlist in self.playlists \
                 else next(iter(self.playlists))
        self.current_playlist = target
        for i in range(self.playlist_list.count()):
            it = self.playlist_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == target:
                self.playlist_list.setCurrentItem(it)
                break
        self._refresh_songs_view()

    # ==============================================================
    #  Playlist ops
    # ==============================================================
    def _on_playlist_current_changed(self, current, _previous):
        if current is None:
            return
        if self.right_stack.currentIndex() == 1:
            self._close_edit_page()

        name = current.data(Qt.ItemDataRole.UserRole)
        if name == self.current_playlist:
            return
        self.current_playlist = name
        self.player.stop()
        self.current_index = -1
        self.title_label.setText("No track loaded")
        self._refresh_songs_view()
        self._save_settings()

    def _on_playlist_context_menu(self, pos):
        item = self.playlist_list.itemAt(pos)
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Edit…",     lambda n=name: self.open_edit_page(n))
        menu.addSeparator()
        menu.addAction("Rename…",   lambda n=name: self.rename_playlist(n))
        menu.addAction("Delete",    lambda n=name: self.delete_playlist(n))
        menu.addSeparator()
        menu.addAction("Set cover…", lambda n=name: self.set_playlist_cover(n))
        if self._pl_cover(name):
            menu.addAction("Clear cover", lambda n=name: self.clear_playlist_cover(n))
        menu.exec(self.playlist_list.mapToGlobal(pos))

    def new_playlist(self):
        dlg = NewPlaylistDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.name()
        if name in self.playlists:
            QMessageBox.warning(self, "Already exists",
                                f'Playlist "{name}" already exists.')
            return
        self.playlists[name] = self._blank_playlist()
        self.playlists[name]["cover"] = dlg.cover_b64()
        self.current_playlist = name
        self._save_settings()
        self.player.stop()
        self.current_index = -1
        self.title_label.setText("No track loaded")
        self._refresh_playlist_view()
        self._refresh_songs_view()

    def rename_playlist(self, name: str | None = None):
        old = name or self.current_playlist
        if not old or old not in self.playlists:
            return
        new, ok = QInputDialog.getText(self, "Rename playlist", "New name:", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if new in self.playlists:
            QMessageBox.warning(self, "Already exists",
                                f'Playlist "{new}" already exists.')
            return
        self.playlists = {new if k == old else k: v for k, v in self.playlists.items()}
        if self.current_playlist == old:
            self.current_playlist = new
        self._save_settings()
        self._refresh_playlist_view()
        self._refresh_songs_view()

    def delete_playlist(self, name: str | None = None):
        target = name or self.current_playlist
        if not target or target not in self.playlists:
            return
        reply = QMessageBox.question(
            self, "Delete playlist",
            f'Delete playlist "{target}"?\n(Files on disk are NOT deleted.)')
        if reply != QMessageBox.StandardButton.Yes:
            return
        if target == self.current_playlist:
            self.player.stop()
            self.current_index = -1
            self.title_label.setText("No track loaded")
        del self.playlists[target]
        if self.current_playlist == target:
            self.current_playlist = next(iter(self.playlists), None)
        self._save_settings()
        self._refresh_playlist_view()
        self._refresh_songs_view()

    def set_playlist_cover(self, name: str | None = None):
        target = name or self.current_playlist
        if not target or target not in self.playlists:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f'Choose cover for "{target}"', "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)")
        if not path:
            return
        pix = scaled_pixmap_from_file(path, PL_COVER_SAVE)
        if pix is None:
            QMessageBox.warning(self, "Could not load image",
                                "Qt could not decode this image file.")
            return
        b64 = encode_pixmap_b64(pix)
        if not b64:
            QMessageBox.warning(self, "Encode failed",
                                "Could not encode cover image.")
            return
        self.playlists[target]["cover"] = b64
        self._save_settings()
        self._refresh_playlist_view()
        self._refresh_songs_view()

    def clear_playlist_cover(self, name: str | None = None):
        target = name or self.current_playlist
        if not target or target not in self.playlists:
            return
        self.playlists[target]["cover"] = None
        self._save_settings()
        self._refresh_playlist_view()
        self._refresh_songs_view()

    # ==============================================================
    #  Edit page
    # ==============================================================
    def open_edit_page(self, name: str | None = None):
        target = name or self.current_playlist
        if not target or target not in self.playlists:
            return
        self._editing_playlist = target
        self.edit_page.load(
            target,
            self.playlists[target].get("description", ""),
            self.playlists[target].get("cover"),
        )
        self.right_stack.setCurrentIndex(1)

    def _close_edit_page(self):
        self._editing_playlist = None
        self.right_stack.setCurrentIndex(0)

    def _on_playlist_edit_saved(self, new_name: str, new_desc: str,
                                new_cover_b64: str):
        old_name = self._editing_playlist
        if old_name is None or old_name not in self.playlists:
            self._close_edit_page()
            return

        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Name required",
                                "Playlist name cannot be empty.")
            return
        if new_name != old_name and new_name in self.playlists:
            QMessageBox.warning(self, "Name taken",
                                f'Playlist "{new_name}" already exists.')
            return

        data = self.playlists[old_name]
        data["description"] = new_desc
        data["cover"] = new_cover_b64 if new_cover_b64 else None

        if new_name != old_name:
            self.playlists = {new_name if k == old_name else k: v
                              for k, v in self.playlists.items()}
            if self.current_playlist == old_name:
                self.current_playlist = new_name

        self._editing_playlist = None
        self._save_settings()
        self._refresh_playlist_view()
        self._refresh_songs_view()
        self.right_stack.setCurrentIndex(0)

    # ==============================================================
    #  Track metadata edit
    # ==============================================================
    def _edit_current_track(self):
        if not self.current_playlist or self.current_index < 0:
            return
        item = self.list_widget.item(self.current_index)
        if item is not None:
            self.edit_track_metadata(item)

    def edit_track_metadata(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "File missing",
                                "Cannot edit metadata — file not found.")
            return

        meta, cover_pix = self._get_row_data(path)
        dlg = TrackEditDialog(path, meta, cover_pix, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self._invalidate_cache(path)
        self._refresh_songs_view()

        if (self.current_playlist
                and 0 <= self.current_index < len(self._tracks())
                and self._tracks()[self.current_index] == path):
            new_meta, _ = self._get_row_data(path)
            if new_meta.get("artist"):
                self.title_label.setText(
                    f'{new_meta["artist"]} — {new_meta["title"]}')
            else:
                self.title_label.setText(new_meta["title"])

        if self.list_widget.currentItem() is not None:
            self.list_widget.currentItem().setSelected(True)

    # ==============================================================
    #  Drag-and-drop / song ops
    # ==============================================================
    def _on_files_dropped(self, paths: list[str]):
        if not self.current_playlist:
            QMessageBox.information(self, "No playlist",
                                    "Create or select a playlist first.")
            return
        pl = self.playlists[self.current_playlist]["tracks"]
        added = 0
        for p in paths:
            if p not in pl:
                pl.append(p)
                added += 1
        if added:
            self._save_settings()
            self._refresh_songs_view()

    def _on_rows_reordered(self, rows: list[int], drop_row: int):
        if not self.current_playlist:
            return
        pl = self.playlists[self.current_playlist]["tracks"]
        n = len(pl)
        if n == 0 or not rows:
            return

        rows = sorted({r for r in rows if 0 <= r < n})
        if not rows:
            return
        drop_row = max(0, min(n, drop_row))

        if rows[0] <= drop_row <= rows[-1] + 1:
            return

        current_path = None
        if 0 <= self.current_index < n:
            current_path = pl[self.current_index]

        moving = [pl[r] for r in rows]
        removed_before = sum(1 for r in rows if r < drop_row)
        dest = drop_row - removed_before

        for r in reversed(rows):
            del pl[r]
        for i, item in enumerate(moving):
            pl.insert(dest + i, item)

        if current_path is not None:
            try:
                self.current_index = pl.index(current_path)
            except ValueError:
                self.current_index = -1

        self._save_settings()
        self._refresh_songs_view()

        for i in range(len(moving)):
            it = self.list_widget.item(dest + i)
            if it:
                it.setSelected(True)
        last_idx = dest + len(moving) - 1
        it_last = self.list_widget.item(last_idx)
        if it_last is not None:
            self.list_widget.scrollToItem(
                it_last,
                QAbstractItemView.ScrollHint.EnsureVisible)
        self.list_widget.viewport().update()

    def _on_song_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return

        row = self.list_widget.row(item)
        selection = sorted({i.row() for i in self.list_widget.selectedIndexes()})
        if row not in selection:
            selection = [row]

        pl = self._tracks()
        single = len(selection) == 1
        r = selection[0]

        menu = QMenu(self)

        act_edit = menu.addAction(
            "Edit metadata…",
            lambda it=item: self.edit_track_metadata(it),
        )
        act_edit.setEnabled(single)
        menu.addSeparator()

        label = "Remove from playlist" if single else \
                f"Remove {len(selection)} tracks from playlist"
        menu.addAction(label, lambda rows=selection: self.remove_rows(rows))
        menu.addSeparator()

        act_up   = menu.addAction("Move up",        lambda: self._move_selected(-1))
        act_down = menu.addAction("Move down",      lambda: self._move_selected(+1))
        act_top  = menu.addAction("Move to top",    lambda: self._move_to(0))
        act_bot  = menu.addAction("Move to bottom", lambda: self._move_to(len(pl) - 1))

        if r == 0:
            act_up.setEnabled(False); act_top.setEnabled(False)
        if r == len(pl) - 1:
            act_down.setEnabled(False); act_bot.setEnabled(False)
        if not single:
            for a in (act_up, act_down, act_top, act_bot):
                a.setEnabled(False)

        menu.exec(self.list_widget.mapToGlobal(pos))

    def remove_rows(self, rows: list[int]):
        if not self.current_playlist:
            return
        pl = self.playlists[self.current_playlist]["tracks"]
        rows = sorted({r for r in rows if 0 <= r < len(pl)}, reverse=True)
        if not rows:
            return

        if self.current_index in rows:
            self.player.stop()
            self.current_index = -1
            self.title_label.setText("No track loaded")

        for r in rows:
            del pl[r]

        if self.current_index >= 0:
            removed_before = sum(1 for r in rows if r < self.current_index)
            self.current_index -= removed_before
            if self.current_index >= len(pl):
                self.current_index = -1

        self._save_settings()
        self._refresh_songs_view()

    def remove_selected(self):
        rows = sorted({i.row() for i in self.list_widget.selectedIndexes()})
        if rows:
            self.remove_rows(rows)

    def _move_selected(self, delta: int):
        rows = sorted({i.row() for i in self.list_widget.selectedIndexes()})
        if len(rows) != 1:
            return
        self._move_row(rows[0], delta)

    def _move_row(self, row: int, delta: int):
        pl = self._tracks()
        new = row + delta
        if not (0 <= row < len(pl)) or not (0 <= new < len(pl)):
            return
        pl[row], pl[new] = pl[new], pl[row]
        if self.current_index == row:
            self.current_index = new
        elif self.current_index == new:
            self.current_index = row
        self._save_settings()
        self._refresh_songs_view()
        self.list_widget.setCurrentRow(new)

    def _move_to(self, target: int):
        rows = sorted({i.row() for i in self.list_widget.selectedIndexes()})
        if len(rows) != 1:
            return
        row = rows[0]
        pl = self._tracks()
        if not (0 <= row < len(pl)) or not (0 <= target < len(pl)) or row == target:
            return
        path = pl.pop(row)
        pl.insert(target, path)
        if self.current_index == row:
            self.current_index = target
        elif row < self.current_index <= target:
            self.current_index -= 1
        elif target <= self.current_index < row:
            self.current_index += 1
        self._save_settings()
        self._refresh_songs_view()
        self.list_widget.setCurrentRow(target)

    # ==============================================================
    #  Playback
    # ==============================================================
    def play_index(self, index):
        if self.current_playlist is None:
            return
        if isinstance(index, QListWidgetItem):
            index = self.list_widget.row(index)
        pl = self._tracks()
        if not (0 <= index < len(pl)):
            return
        path = pl[index]
        if not os.path.exists(path):
            QMessageBox.warning(self, "File missing", f"File not found:\n{path}")
            return
        self.current_index = index
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.list_widget.setCurrentRow(index)
        meta, _ = self._get_row_data(path)
        if meta.get("artist"):
            self.title_label.setText(f'{meta["artist"]} — {meta["title"]}')
        else:
            self.title_label.setText(meta["title"])

    def toggle_play(self):
        if not self.current_playlist or not self._tracks():
            return
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.player.play()
        else:
            if self.current_index == -1:
                self.play_index(0)
            else:
                self.player.play()

    def stop(self):
        self.player.stop()

    def next_track(self):
        if not self.current_playlist:
            return
        n = len(self._tracks())
        if n == 0:
            return
        self.play_index((self.current_index + 1) % n)

    def prev_track(self):
        if not self.current_playlist:
            return
        n = len(self._tracks())
        if n == 0:
            return
        if self.player.position() > 3000 and self.current_index >= 0:
            self.player.setPosition(0)
            return
        self.play_index((self.current_index - 1) % n)

    # ==============================================================
    #  Slider + player events
    # ==============================================================
    def _seek_start(self):
        self.is_seeking = True

    def _seek_end(self):
        self.is_seeking = False
        self.player.setPosition(self.progress.value())

    def _on_position(self, pos):
        if not self.is_seeking:
            self.progress.setValue(pos)
        self.time_label.setText(self._fmt(pos))

    def _on_duration(self, dur):
        self.progress.setRange(0, dur)
        self.duration_label.setText(self._fmt(dur))

    def _on_state(self, state):
        style = self.style()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPause))
        else:
            self.btn_play.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next_track()

    def _on_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            QMessageBox.warning(self, "Playback error", error_string or str(error))

    @staticmethod
    def _fmt(ms):
        if ms <= 0:
            return "00:00"
        t = QTime(0, 0).addMSecs(ms)
        return t.toString("H:mm:ss") if t.hour() > 0 else t.toString("mm:ss")


# ==================================================================
#  Entry point
# ==================================================================
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MP3 Player")

    # Global rounded-button style. Widget-level stylesheets (used by the
    # circular transport buttons and accent Save buttons) take precedence,
    # so they keep their own border-radius.
    app.setStyleSheet("""
        QPushButton {
            border-radius: 12px;
            padding: 5px 14px;
            background-color: palette(button);
            border: 1px solid palette(mid);
            min-height: 22px;
        }
        QPushButton:hover {
            background-color: palette(midlight);
            border: 1px solid palette(mid);
        }
        QPushButton:pressed {
            background-color: palette(dark);
        }
        QPushButton:disabled {
            color: palette(placeholder-text);
            background-color: palette(window);
        }
    """)

    win = MP3Player()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()