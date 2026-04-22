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
export LIVE_JOURNAL_PASSWORD='change-me'
export LIVE_JOURNAL_SESSION_SECRET='change-me-too'
python app.py
```

このアプリは Python 標準ライブラリのみで動くので、追加の `pip install` は不要です。

主な環境変数:

- `LIVE_JOURNAL_DB`: SQLite ファイルのパス
- `LIVE_JOURNAL_HOST`: バインド先ホスト
- `LIVE_JOURNAL_PORT`: ポート
- `LIVE_JOURNAL_MODE`: `private` または `public`
- `LIVE_JOURNAL_USER`: 管理者ユーザー名
- `LIVE_JOURNAL_PASSWORD`: 管理者パスワード
- `LIVE_JOURNAL_SESSION_SECRET`: セッション署名キー
