# Live Journal

ライブを見た記録を残して検索できる、軽量な Web アプリです。

## 起動

```bash
python3 -m venv .venv
source .venv/bin/activate
python app.py
```

起動後、`http://127.0.0.1:8000` を開きます。

初期ログイン情報:

- ID: `admin`
- Password: `admin`

本番では以下を設定してください。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LIVE_JOURNAL_PASSWORD='change-me'
export LIVE_JOURNAL_SESSION_SECRET='change-me-too'
gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 2
```

開発時は標準ライブラリだけで `python app.py` を使えます。本番では `gunicorn` を使う想定です。

主な環境変数:

- `LIVE_JOURNAL_DB`: SQLite ファイルのパス
- `LIVE_JOURNAL_HOST`: バインド先ホスト
- `LIVE_JOURNAL_PORT`: ポート
- `LIVE_JOURNAL_MODE`: `private` または `public`
- `LIVE_JOURNAL_USER`: 管理者ユーザー名
- `LIVE_JOURNAL_PASSWORD`: 管理者パスワード
- `LIVE_JOURNAL_SESSION_SECRET`: セッション署名キー

## デプロイ

最小構成は以下です。

```bash
git clone <repo> /opt/live_journal
cd /opt/live_journal
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LIVE_JOURNAL_PASSWORD='change-me'
export LIVE_JOURNAL_SESSION_SECRET='change-me-too'
.venv/bin/gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 2
```

リバースプロキシは nginx か Caddy を前段に置く前提です。

systemd を使う場合は [deploy/live-journal.service](/home/oquno/live_journal/deploy/live-journal.service:1) を `/etc/systemd/system/live-journal.service` に置いて、環境変数や `WorkingDirectory` を実際のパスに合わせて修正してください。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now live-journal
```
