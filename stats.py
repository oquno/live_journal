"""Period validation and statistics based on recorded live entries."""

from collections import Counter
from datetime import date


def stats_period(year="", date_from="", date_to=""):
    year, date_from, date_to = (value.strip() for value in (year, date_from, date_to))
    for value in (date_from, date_to):
        if value:
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                raise ValueError("開始日・終了日は有効な日付を指定してください。") from None
            if parsed_date.isoformat() != value:
                raise ValueError("日付は YYYY-MM-DD 形式で指定してください。")
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise ValueError("開始日は終了日以前の日付を指定してください。")
        return {
            "year": "", "from": date_from, "to": date_to,
            "label": f'{date_from or "最初の記録"} 〜 {date_to or "最新の記録"}',
        }
    if year:
        if len(year) != 4 or not year.isascii() or not year.isdigit() or not 1 <= int(year) <= 9999:
            raise ValueError("年は 4 桁の有効な西暦を指定してください。")
        return {"year": year, "from": f"{year}-01-01", "to": f"{year}-12-31", "label": f"{year}年"}
    return {"year": "", "from": "", "to": "", "label": "全期間"}


def load_stats(conn, period):
    clauses, values = [], []
    if period["from"]:
        clauses.append("e.event_date >= ?")
        values.append(period["from"])
    if period["to"]:
        clauses.append("e.event_date <= ?")
        values.append(period["to"])
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    entries = conn.execute(
        f"""SELECT e.id, e.event_date, v.id AS venue_id, v.name AS venue_name
            FROM entries e JOIN venues v ON v.id = e.venue_id
            {where} ORDER BY e.event_date, e.id""", values,
    ).fetchall()
    appearances = conn.execute(
        f"""WITH first_seen AS (
                SELECT ea.artist_id, MIN(e.event_date) AS first_date
                FROM entry_artists ea JOIN entries e ON e.id = ea.entry_id
                GROUP BY ea.artist_id
            )
            SELECT ea.artist_id, a.name, e.event_date, f.first_date
            FROM entry_artists ea JOIN entries e ON e.id = ea.entry_id
            JOIN artists a ON a.id = ea.artist_id
            JOIN first_seen f ON f.artist_id = ea.artist_id
            {where} ORDER BY e.event_date, e.id""", values,
    ).fetchall()
    years = [row[0] for row in conn.execute(
        "SELECT DISTINCT substr(event_date, 1, 4) FROM entries ORDER BY substr(event_date, 1, 4) DESC"
    )]

    artists, venues = {}, {}
    annual_entries, annual_artists = Counter(), {}
    monthly_entries, weekdays = Counter(), Counter()
    for entry in entries:
        event_date = entry["event_date"]
        monthly_entries[event_date[:7]] += 1
        annual_entries[event_date[:4]] += 1
        weekdays[date.fromisoformat(event_date).weekday()] += 1
        venue = venues.setdefault(entry["venue_id"], {
            "id": entry["venue_id"], "name": entry["venue_name"], "count": 0,
        })
        venue["count"] += 1
    for appearance in appearances:
        artist = artists.setdefault(appearance["artist_id"], {
            "id": appearance["artist_id"], "name": appearance["name"], "count": 0,
            "first_date": appearance["first_date"], "months": Counter(), "years": Counter(),
        })
        artist["count"] += 1
        artist["months"][appearance["event_date"][:7]] += 1
        artist["years"][appearance["event_date"][:4]] += 1
        annual_artists.setdefault(appearance["event_date"][:4], Counter())[appearance["artist_id"]] += 1
    ranked_artists = sorted(artists.values(), key=lambda row: (-row["count"], row["name"], row["id"]))
    ranked_venues = sorted(venues.values(), key=lambda row: (-row["count"], row["name"], row["id"]))

    # A short period uses monthly buckets; long archives use years and drill down.
    buckets, grain = [], "months"
    if entries:
        start = date.fromisoformat(period["from"] or entries[0]["event_date"])
        end = date.fromisoformat(period["to"] or entries[-1]["event_date"])
        month_span = (end.year - start.year) * 12 + end.month - start.month + 1
        if month_span <= 24:
            for index in range(month_span):
                offset = start.year * 12 + start.month - 1 + index
                bucket_year, month_index = divmod(offset, 12)
                key = f"{bucket_year:04d}-{month_index + 1:02d}"
                buckets.append({"key": key, "label": key, "count": monthly_entries[key]})
        else:
            grain = "years"
            # Bound the output by the recorded years, even for a very wide filter.
            for bucket_year in range(int(entries[0]["event_date"][:4]), int(entries[-1]["event_date"][:4]) + 1):
                key = f"{bucket_year:04d}"
                buckets.append({"key": key, "label": f"{key}年", "count": annual_entries[key]})

    annual = []
    for year_key, count in sorted(annual_entries.items(), reverse=True):
        counts = annual_artists.get(year_key, Counter())
        top = sorted(counts, key=lambda artist_id: (-counts[artist_id], artists[artist_id]["name"], artist_id))[:3]
        annual.append({"year": year_key, "count": count, "artist_count": len(counts),
                       "top": [{**artists[artist_id], "count": counts[artist_id]} for artist_id in top]})
    new_artists = sum(1 for artist in ranked_artists if
                      (not period["from"] or artist["first_date"] >= period["from"]) and
                      (not period["to"] or artist["first_date"] <= period["to"]))
    seasons = Counter()
    for month_key, count in monthly_entries.items():
        seasons[int(month_key[5:7])] += count
    peak_month = max(sorted(monthly_entries), key=monthly_entries.get) if monthly_entries else ""
    return {
        "years": years, "entries": len(entries), "days": len({row["event_date"] for row in entries}),
        "artists": ranked_artists, "venues": ranked_venues, "appearances": len(appearances),
        "new_artists": new_artists, "known_artists": len(artists) - new_artists,
        "repeat_artists": sum(row["count"] > 1 for row in ranked_artists),
        "first_date": entries[0]["event_date"] if entries else "",
        "last_date": entries[-1]["event_date"] if entries else "",
        "peak_month": {"key": peak_month, "count": monthly_entries[peak_month]},
        "buckets": buckets, "grain": grain, "annual": annual,
        "seasons": [{"label": f"{month}月", "count": seasons[month]} for month in range(1, 13)],
        "weekdays": [{"label": label, "count": weekdays[index]} for index, label in enumerate("月火水木金土日")],
    }
