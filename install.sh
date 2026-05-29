python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LIVE_JOURNAL_PASSWORD="${LIVE_JOURNAL_PASSWORD:?set LIVE_JOURNAL_PASSWORD before running install.sh}"
.venv/bin/gunicorn wsgi:application --bind 127.0.0.1:8000 --workers 2
