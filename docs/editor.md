# ローカル編集実験

この試作は、選んだ写真・動画を設定済みのローカルフォルダーと照合し、約60秒までの初案を作る。場面ごとに採用・除外、素材の差し替え、動画の前後調整を行い、同じMP4をプレビューと保存に使う。初案は選択順から最大12点を均等に取り、素材の内容は判定しない。

Python 3.11、`experiments/editor/requirements.txt` のPyAV、H.264とAACを書き出せるFFmpegを用意する。FFmpegは別途導入し、リポジトリには含めない。

```sh
python3 -m venv .venv
.venv/bin/pip install -r experiments/editor/requirements.txt
.venv/bin/python experiments/editor/server.py --media-root /path/to/originals --work-dir /path/to/private-work --ffmpeg /path/to/ffmpeg
```

表示された `http://127.0.0.1:8765/` を同じPCのブラウザで開く。普通のファイル選択画面で、設定したフォルダー内の原本を選ぶ。ブラウザは各ファイルの名前・大きさと先頭・末尾64KiBのハッシュだけを送る。照合できない素材は選び直せる。原本全体のブラウザからのアップロードは行わない。出力MP4と非公開のJSON manifestは作業フォルダーに残るため、個人素材と同じように扱う。

素材選択後に初案の動画を自動生成する。編集後は「プレビューを更新」で再生成する。全場面を除外すると書き出せない。BGMは未実装で、曲とライセンスは未決定。書き出しの1280×720、30fps、H.264/AACは試作上の選択であり、製品仕様や色の忠実度の保証ではない。HDR素材は変換方針が決まるまで拒否する。

単一ローカル利用者用の一時実験で、作業状態はサーバー再起動後に復元されない。認証、公開運用、クラウド保存、決済、7日後の削除は実装していない。別のフォルダーや別の形式を試すときは、サーバーを明示的に起動し直す。

テストにはPythonの標準 `unittest`、Pillow、NumPyを使う。`EDITOR_FFMPEG` でテスト用FFmpegのパスを指定する。

```sh
.venv/bin/pip install -r experiments/editor/requirements-test.txt
EDITOR_FFMPEG=/path/to/ffmpeg .venv/bin/python -m unittest discover -s tests -p 'test_editor.py' -v
```
