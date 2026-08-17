# VoiceMinutes

macOSメニューバーから音声ファイルを選ぶだけで、文字起こし → 議事録生成 → Notion格納まで自動で行うアプリ。

Claude Code の `/minutes` スキルをヘッドレス実行して議事録を生成するため、Claude Desktop App が起動している環境であれば認証不要で動作する。

## 動作環境

- macOS 14 Sonoma 以上（Apple Silicon 推奨）
- Claude Desktop App（起動中であること）
- Claude CLI（`claude` コマンド）
- Python 3（Homebrew: `/opt/homebrew/bin/python3`）
- ffmpeg

## 依存ライブラリ

```bash
brew install ffmpeg
pip3 install rumps pyobjc
```

## 追加の依存

文字起こしに `~/voice-input/transcribe.py` を使用する。
mlx-whisper ベースの文字起こしスクリプトで、以下のセットアップが必要：

```bash
pip3 install mlx-whisper sounddevice numpy --break-system-packages
```

`transcribe.py` は [voice-input](https://github.com/AkihiroMiki/voice-input) リポジトリを参照（または独自実装でも可）。

## Claude の設定

議事録生成には Claude Code の `/minutes` スキルが必要。
`~/.claude/skills/minutes/SKILL.md` にスキルファイルを置いておくこと。

## 起動方法

```bash
python3 voice_minutes_app.py
```

または `launch.sh` を実行、もしくはログイン項目に登録して常駐させる。

## 使い方

1. メニューバーのアイコンをクリック
2. 「議事録を作成...」を選択
3. 音声ファイル（wav / m4a / mp3）を選択（複数選択で連結）
4. Notion の格納先 URL を入力（省略でデフォルトDBへ）
5. 処理が完了すると通知が届く

## 処理の流れ

```
音声ファイル選択
    ↓
ffmpeg で 16kHz WAV に変換・結合
    ↓
transcribe.py（mlx-whisper）で文字起こし
    ↓
claude -p /minutes {transcript} で議事録生成・Notion格納
```

## ライセンス

MIT
