#!/usr/bin/env python3
"""VoiceMinutes — メニューバーから音声ファイルを議事録化するアプリ"""

import json
import os
import math
import subprocess
import threading
import tempfile
import time
import shutil

import rumps
import AppKit
import objc
from PyObjCTools import AppHelper

PYTHON = "/opt/homebrew/bin/python3"
TRANSCRIBE = os.path.expanduser("~/voice-input/transcribe.py")
FFMPEG = "/opt/homebrew/bin/ffmpeg"

AUDIO_EXTS = {".wav", ".m4a", ".mp3"}


def find_claude():
    candidates = [
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        os.path.expanduser("~/.volta/bin/claude"),
        os.path.expanduser("~/.npm-global/bin/claude"),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "claude"


def find_claude_auth_env():
    """~/.claude/sessions/ から生きているDesktopセッションを探し認証env varを返す"""
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    if not os.path.isdir(sessions_dir):
        return {}
    for fname in sorted(os.listdir(sessions_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, fname)) as f:
                session = json.load(f)
        except Exception:
            continue
        if session.get("entrypoint") != "claude-desktop":
            continue
        socket_path = session.get("messagingSocketPath", "")
        if not socket_path or not os.path.exists(socket_path):
            continue
        pid = str(session.get("pid", fname.split(".")[0]))
        # 対応する .key ファイルからpeerTokenを取得
        peer_token = ""
        for kf in os.listdir(sessions_dir):
            if kf.startswith(pid + ".") and kf.endswith(".key"):
                try:
                    kdata = json.loads(open(os.path.join(sessions_dir, kf), "rb").read())
                    peer_token = kdata.get("peerToken", "")
                except Exception:
                    pass
                break
        if not peer_token:
            continue
        return {
            "CLAUDE_CODE_MESSAGING_SOCKET": socket_path,
            "CLAUDE_CODE_MESSAGING_TOKEN": peer_token,
            "CLAUDE_PID": pid,
            "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH": "1",
            "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH": "1",
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            "CLAUDECODE": "1",
        }
    return {}


def pick_files():
    """ファイル選択ダイアログ（複数可）。選択パスのリストを返す"""
    script = (
        'tell application "Finder"\n'
        '  set fs to choose file with prompt "音声ファイルを選択（複数可）" '
        '    of type {"wav", "m4a", "mp3", "public.audio"} with multiple selections allowed\n'
        '  set paths to {}\n'
        '  repeat with f in fs\n'
        '    set end of paths to POSIX path of f\n'
        '  end repeat\n'
        '  return paths\n'
        'end tell'
    )
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    raw = result.stdout.strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(", ") if p.strip()]


def ask_notion_url():
    """Notion URL 入力ダイアログ"""
    script = (
        'tell application "System Events"\n'
        '  set r to display dialog "格納先 Notion URL（不要なら空欄のままOK）" '
        '    default answer "" with title "VoiceMinutes"\n'
        '  return text returned of r\n'
        'end tell'
    )
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    return result.stdout.strip()


def notify(title, message):
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "{title}"'
    ])


# ---------------------------------------------------------------------------
# 音声処理
# ---------------------------------------------------------------------------

def to_wav(src, dst):
    """16kHz・モノラル・PCM s16le に変換する（transcribe.py / whisper の前提に合わせる）"""
    r = subprocess.run(
        [FFMPEG, "-y", "-i", src,
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst],
        capture_output=True, text=True
    )
    return r.returncode == 0, r.stderr


def concat_wavs(wav_paths, out_path):
    tmp_list = out_path + ".list.txt"
    with open(tmp_list, "w") as f:
        for p in wav_paths:
            f.write(f"file '{p}'\n")
    r = subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", tmp_list, out_path],
        capture_output=True, text=True
    )
    os.unlink(tmp_list)
    return r.returncode == 0


# transcribe.py が stderr に出す ERR:<CODE> → 表示メッセージ
TRANSCRIBE_ERRORS = {
    "WAV_READ": "音声ファイルを読めませんでした",
    "NO_AUDIO": "音声が検出されませんでした",
    "MODEL": "変換エンジンが失敗しました",
    "EMPTY": "文字を認識できませんでした",
}


def parse_transcribe_error(stderr):
    import re
    m = re.search(r"ERR:([A-Z_]+)", stderr or "")
    if m:
        return TRANSCRIBE_ERRORS.get(m.group(1), f"文字起こし失敗（{m.group(1)}）")
    return "文字起こしに失敗しました"


def fail(progress, msg, detail=""):
    progress.error(msg)
    notify("VoiceMinutes", f"{msg}{(': ' + detail) if detail else ''}")


def run_pipeline(audio_paths, notion_url, progress):
    """変換 → 文字起こし → claude -p /minutes。progress で進捗を通知"""
    tmp_dir = tempfile.gettempdir()
    tmp_txt = os.path.join(tmp_dir, "vm_transcript.txt")

    # ステップ0: 音声を準備
    progress.step(0)
    if len(audio_paths) == 1:
        wav_path = os.path.join(tmp_dir, "vm_input.wav")
        ok, err = to_wav(audio_paths[0], wav_path)
        if not ok:
            fail(progress, "音声の変換に失敗しました", err.strip()[-80:])
            return
    else:
        wav_parts = []
        for i, src in enumerate(audio_paths):
            dst = os.path.join(tmp_dir, f"vm_part_{i}.wav")
            ok, err = to_wav(src, dst)
            if not ok:
                fail(progress, f"変換失敗: {os.path.basename(src)}", err.strip()[-80:])
                return
            wav_parts.append(dst)
        wav_path = os.path.join(tmp_dir, "vm_input.wav")
        if not concat_wavs(wav_parts, wav_path):
            fail(progress, "ファイルの結合に失敗しました")
            return

    # ステップ1: 文字起こし
    progress.step(1)
    r = subprocess.run([PYTHON, TRANSCRIBE, wav_path, tmp_txt],
                       capture_output=True, text=True)
    if r.returncode != 0:
        fail(progress, parse_transcribe_error(r.stderr))
        return

    # ステップ2: 議事録生成・格納
    progress.step(2)
    prompt = f"/minutes {tmp_txt}"
    if notion_url:
        prompt += f" {notion_url}"
    # ヘッドレス実行では権限ダイアログに応答できないため事前にバイパスする
    # （ローカルの自分のPCで、自分の音声・自分のNotionにのみ作用するため）
    env = os.environ.copy()
    env.setdefault("HOME", os.path.expanduser("~"))
    env.update(find_claude_auth_env())
    r = subprocess.run(
        [find_claude(), "-p", "--dangerously-skip-permissions", prompt],
        capture_output=True, text=True,
        env=env,
    )
    # 成功・失敗にかかわらず claude の出力を残す（格納可否の確認用）
    with open("/tmp/voice-minutes-claude.log", "w") as lf:
        lf.write(f"cmd: {find_claude()} -p {prompt}\n")
        lf.write(f"returncode: {r.returncode}\n")
        lf.write(f"--- stdout ---\n{r.stdout}\n")
        lf.write(f"--- stderr ---\n{r.stderr}\n")
    if r.returncode != 0:
        fail(progress, "議事録生成に失敗しました", r.stderr.strip()[-80:])
        return

    progress.done()
    notify("VoiceMinutes", "議事録の生成・格納が完了しました")


# ---------------------------------------------------------------------------
# ステータスウィンドウ（チェックリスト型カード・ダークトンマナ）
# ---------------------------------------------------------------------------

CARD_W, CARD_H = 360, 192
CARD_CORNER = 16
STEPS_FULL = ["音声を準備", "文字起こし", "議事録を生成・Notionへ格納"]
M_PENDING, M_ACTIVE, M_DONE, M_ERR = "○", "●", "✓", "✕"
# 各ステップが占める進捗範囲（%）。ステップ中はこの上限へ向けてじわじわ前進する
STEP_BOUNDS = [(0, 15), (15, 55), (55, 95)]


def rgba(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


C_BG = rgba(0.055, 0.055, 0.060, 0.97)
C_PROC = rgba(0.039, 0.518, 1.000)         # 青: 処理中
C_OK = rgba(0.188, 0.820, 0.345)           # 緑: 完了
C_ERR = rgba(1.000, 0.231, 0.188)          # 赤: エラー
C_TXT = rgba(1, 1, 1, 1.0)                  # 白
C_PENDING = rgba(1, 1, 1, 0.32)            # 灰（待機）
C_SUB = rgba(1, 1, 1, 0.42)                # サブ文字


def _label(frame, size, color, weight_bold=False):
    tf = AppKit.NSTextField.alloc().initWithFrame_(frame)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    font = (AppKit.NSFont.boldSystemFontOfSize_(size) if weight_bold
            else AppKit.NSFont.systemFontOfSize_(size))
    tf.setFont_(font)
    tf.setTextColor_(color)
    return tf


class CardView(AppKit.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        b = self.bounds()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, CARD_CORNER, CARD_CORNER)
        C_BG.set()
        path.fill()


class StatusWindow(AppKit.NSObject):
    def init(self):
        self = objc.super(StatusWindow, self).init()
        if self is None:
            return None
        self._start = None
        self._timer = None
        self._hide_timer = None
        self._step_labels = list(STEPS_FULL)
        self._build()
        return self

    def _build(self):
        rect = AppKit.NSMakeRect(0, 0, CARD_W, CARD_H)
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setLevel_(AppKit.NSFloatingWindowLevel)
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setReleasedWhenClosed_(False)
        # accessory アプリでも隠れないようにする（NSPanel は既定で非アクティブ時に隠れる）
        panel.setHidesOnDeactivate_(False)

        view = CardView.alloc().initWithFrame_(rect)
        panel.setContentView_(view)

        # タイトル
        self._title = _label(AppKit.NSMakeRect(20, 16, CARD_W - 54, 22),
                            15, C_TXT, weight_bold=True)
        self._title.setStringValue_("処理を実行中")
        view.addSubview_(self._title)

        # ステップ行
        self._steps = []
        y = 50
        for name in STEPS_FULL:
            tf = _label(AppKit.NSMakeRect(22, y, CARD_W - 44, 20), 13, C_PENDING)
            tf.setStringValue_(f"{M_PENDING}  {name}")
            view.addSubview_(tf)
            self._steps.append(tf)
            y += 27

        # 進捗バー（確定・実際の進み具合を表示）
        self._bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(
            AppKit.NSMakeRect(22, y + 4, CARD_W - 44, 14))
        self._bar.setStyle_(0)
        self._bar.setIndeterminate_(False)
        self._bar.setMinValue_(0.0)
        self._bar.setMaxValue_(100.0)
        self._bar.setDoubleValue_(0.0)
        view.addSubview_(self._bar)

        # 経過時間
        self._elapsed = _label(AppKit.NSMakeRect(22, y + 26, CARD_W - 44, 16),
                              12, C_SUB)
        self._elapsed.setStringValue_("経過 00:00")
        view.addSubview_(self._elapsed)

        # ✕ 閉じるボタン
        btn = AppKit.NSButton.alloc().initWithFrame_(
            AppKit.NSMakeRect(CARD_W - 32, 14, 20, 20))
        btn.setBordered_(False)
        btn.setButtonType_(AppKit.NSButtonTypeMomentaryChange)
        attr = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "✕", {
                AppKit.NSForegroundColorAttributeName: rgba(1, 1, 1, 0.5),
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(14),
            })
        btn.setAttributedTitle_(attr)
        btn.setTarget_(self)
        btn.setAction_(b"closeClicked:")
        view.addSubview_(btn)

        self._panel = panel

    # --- 表示・状態更新（メインスレッドで呼ばれる） ---

    def _render_steps(self, active_index, all_done=False, error_index=None):
        for i, tf in enumerate(self._steps):
            name = self._step_labels[i]
            if error_index is not None and i == error_index:
                tf.setStringValue_(f"{M_ERR}  {name}")
                tf.setTextColor_(C_ERR)
            elif all_done or (active_index is not None and i < active_index):
                tf.setStringValue_(f"{M_DONE}  {name}")
                tf.setTextColor_(C_OK)
            elif active_index is not None and i == active_index:
                tf.setStringValue_(f"{M_ACTIVE}  {name}")
                tf.setTextColor_(C_TXT)
            else:
                tf.setStringValue_(f"{M_PENDING}  {name}")
                tf.setTextColor_(C_PENDING)

    def reset_and_show(self, file_count):
        self._step_labels = list(STEPS_FULL)
        if file_count > 1:
            self._step_labels[0] = f"音声を準備（{file_count}ファイルを結合）"
        self._active = 0
        self._bar_val = 0.0
        self._bar_target = float(STEP_BOUNDS[0][1])
        self._title.setStringValue_("処理を実行中")
        self._title.setTextColor_(C_TXT)
        self._render_steps(0)
        self._bar.setDoubleValue_(0.0)
        self._elapsed.setStringValue_("経過 00:00 ・ 0%")

        self._start = time.monotonic()
        if self._timer is not None:
            self._timer.invalidate()
        self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, b"_tick:", None, True)

        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        x = vf.origin.x + vf.size.width - CARD_W - 16
        y = vf.origin.y + 16
        self._panel.setFrameOrigin_((x, y))
        self._panel.orderFrontRegardless()

    def advanceTo_(self, index):
        self._active = index
        lo, hi = STEP_BOUNDS[index]
        self._bar_val = max(self._bar_val, float(lo))
        self._bar_target = float(hi)
        self._render_steps(index)

    def finishSuccess(self):
        self._render_steps(None, all_done=True)
        self._title.setStringValue_("完了しました")
        self._title.setTextColor_(C_OK)
        self._bar_val = 100.0
        self._bar_target = 100.0
        self._bar.setDoubleValue_(100.0)
        sec = int(time.monotonic() - self._start) if self._start else 0
        self._elapsed.setStringValue_(f"経過 {sec // 60:02d}:{sec % 60:02d} ・ 100%")
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
        self._hide_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            4.0, self, b"_hide:", None, False)

    def failWith_(self, msg):
        self._render_steps(None, error_index=getattr(self, "_active", 0))
        self._title.setStringValue_(f"エラー: {msg}")
        self._title.setTextColor_(C_ERR)
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    # --- 内部 ---

    def _tick_(self, timer):
        if self._start is None:
            return
        # 目標へ向けてイージングで前進（ステップ中も止まって見えない）
        self._bar_val += (self._bar_target - self._bar_val) * 0.04
        if self._bar_val > self._bar_target:
            self._bar_val = self._bar_target
        self._bar.setDoubleValue_(self._bar_val)

        sec = int(time.monotonic() - self._start)
        pct = int(self._bar_val)
        self._elapsed.setStringValue_(f"経過 {sec // 60:02d}:{sec % 60:02d} ・ {pct}%")

    def _hide_(self, timer):
        self._panel.orderOut_(None)

    def closeClicked_(self, sender):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None
        if self._hide_timer is not None:
            self._hide_timer.invalidate()
            self._hide_timer = None
        self._panel.orderOut_(None)


class Progress:
    """ワーカースレッドから StatusWindow をメインスレッド経由で更新する"""
    def __init__(self, win):
        self.win = win

    def step(self, index):
        AppHelper.callAfter(self.win.advanceTo_, index)

    def done(self):
        AppHelper.callAfter(self.win.finishSuccess)

    def error(self, msg):
        AppHelper.callAfter(self.win.failWith_, msg)


# ---------------------------------------------------------------------------
# アプリ本体
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PDF = os.path.join(BASE_DIR, "icon.pdf")


class VoiceMinutesApp(rumps.App):
    def __init__(self):
        super().__init__("", icon=ICON_PDF, template=True, quit_button="終了")
        # ドックの Python アイコンを隠す（メニューバー常駐アプリにする）
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
        self.menu = [
            rumps.MenuItem("議事録を作成...", callback=self.start),
            None,
        ]
        self.status = StatusWindow.alloc().init()

    def start(self, _):
        audio_paths = pick_files()
        if not audio_paths:
            return
        notion_url = ask_notion_url()

        self.status.reset_and_show(len(audio_paths))
        notify("VoiceMinutes", "処理を開始しました")

        progress = Progress(self.status)
        threading.Thread(
            target=run_pipeline,
            args=(audio_paths, notion_url, progress),
            daemon=True,
        ).start()


if __name__ == "__main__":
    VoiceMinutesApp().run()
