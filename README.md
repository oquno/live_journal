# Live Journal

ライブを見た記録を残し、後から演者・会場・日付などを手掛かりに振り返れる、
個人利用向けの軽量な Web アプリです。Python と SQLite で動作し、単一ユーザーでの
セルフホストを想定しています。

## 主な機能

### ライブ記録の管理

- ライブ記録の作成、編集、削除
- 開催日、イベント名、会場、演者、メモの保存
- 1 件の記録に複数の演者を登録
- 購入物を名前、URL、メモ付きで複数登録
- ブログ、写真アルバムなどの関連リンクを、任意のラベル付きで複数登録
- 事前登録したブログ RSS/Atom と Flickr アルバムから、イベント名・会場名・開催日に合う関連リンク候補を表示
- 同名の演者や会場を既存データとして再利用

### 演者ごとの観覧回数

演者を見た回数を、ライブの開催日順に自動計算して「1 回目」「2 回目」のように
表示します。過去の記録を途中から入力する場合などは、各記録で回数を手動指定でき、
その値を基準に以後の回数が続きます。

演者名をクリックすると、その演者を見たライブを古い順に一覧でき、それぞれが
何回目だったかも確認できます。

### 一覧、検索、振り返り

- ライブ記録を開催日の新しい順に表示
- キーワードによる検索
  - イベント名
  - メモ
  - 会場名
  - 演者名
  - 購入物名
  - 関連リンクのラベルと URL
- 演者、会場、開催日の範囲による絞り込み
- 1 ページ 50 件のページ分割
- 会場ごとの開催記録一覧
- ライブ詳細で、演者、購入物、関連リンク、メモをまとめて表示

### 公開範囲と認証

更新操作は管理者としてログインした場合だけ行えます。表示範囲は起動時の
`LIVE_JOURNAL_MODE` で切り替えます。

- `private`: 閲覧にもログインが必要
- `public`: 誰でも閲覧でき、作成・編集・削除にはログインが必要

複数ユーザーの管理や、記録ごとの公開・非公開の切り替えには対応していません。

## 基本的な使い方

1. 管理者としてログインする
2. `Link Sources` で Flickr API key を保存し、ブログの RSS/Atom URL や Flickr アカウントを登録して「今すぐ更新」を実行する
3. `New Entry` で開催日、イベント名、会場、演者などを入力し、関連リンクの「候補を読み込む」から一致候補を選ぶ
4. 保存後、一覧から記録を検索するか、演者名・会場名をクリックして履歴を振り返る

## 起動

```bash
python3 -m venv .venv
source .venv/bin/activate
export LIVE_JOURNAL_PASSWORD='<password>'
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
- `LIVE_JOURNAL_TITLE`: 画面に表示するサイト名
- `LIVE_JOURNAL_MODE`: `private` または `public`
- `LIVE_JOURNAL_USER`: 管理者ユーザー名
- `LIVE_JOURNAL_PASSWORD`: 管理者パスワード
- `LIVE_JOURNAL_SESSION_SECRET`: セッション署名キー
- `LIVE_JOURNAL_SESSION_SECRET_FILE`: 自動生成したセッション署名キーの保存先
Flickr API key は [Flickr App Garden](https://www.flickr.com/services/apps/) でアプリを作成して取得します。
取得後は `Link Sources` の `Flickr API key` 欄に保存してください。API key は SQLite データベースに保存されるため、
データベースのバックアップやファイル権限を適切に管理してください。

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
