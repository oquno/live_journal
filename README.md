# Live Journal

ライブを見た記録を残して検索できる、軽量な Web アプリです。

## 起動

```bash
python3 -m venv .venv
source .venv/bin/activate
python app.py
```

起動後、`http://127.0.0.1:8000` を開きます。

更新操作には管理者パスワードが必要です。起動前に `LIVE_JOURNAL_PASSWORD` を設定してください。

本番では以下のように設定してください。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LIVE_JOURNAL_PASSWORD='<strong random password>'
gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 2
```

`LIVE_JOURNAL_SESSION_SECRET` を省略した場合は、初回起動時にランダム値を自動生成して `data/session_secret` に保存します。明示的に管理したい場合だけ環境変数で上書きしてください。

開発時は標準ライブラリだけで `python app.py` を使えます。本番では `gunicorn` を使う想定です。

主な環境変数:

- `LIVE_JOURNAL_DB`: SQLite ファイルのパス
- `LIVE_JOURNAL_HOST`: バインド先ホスト
- `LIVE_JOURNAL_PORT`: ポート
- `LIVE_JOURNAL_MODE`: `private` または `public`
- `LIVE_JOURNAL_USER`: 管理者ユーザー名
- `LIVE_JOURNAL_PASSWORD`: 管理者パスワード
- `LIVE_JOURNAL_SESSION_SECRET`: セッション署名キー
- `LIVE_JOURNAL_SESSION_SECRET_FILE`: 自動生成したセッション署名キーの保存先

## デプロイ

最小構成は以下です。

```bash
git clone <repo> /opt/live_journal
cd /opt/live_journal
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LIVE_JOURNAL_PASSWORD='<strong random password>'
.venv/bin/gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 2
```

リバースプロキシは nginx か Caddy を前段に置く前提です。

systemd を使う場合は [deploy/live-journal.service](deploy/live-journal.service) を `/etc/systemd/system/live-journal.service` に置いて、環境変数や `WorkingDirectory` を実際のパスに合わせて修正してください。
`LIVE_JOURNAL_PASSWORD` などの秘密情報は `/etc/live-journal.env` に置く想定です。

```bash
sudo install -m 600 /dev/null /etc/live-journal.env
sudoedit /etc/live-journal.env
```

`/etc/live-journal.env` の例:

```sh
LIVE_JOURNAL_PASSWORD=<strong random password>
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now live-journal
```
