# Dad Tickets — דייג'סט יומי של הופעות

צינור פייתון שסורק 17 אתרי כרטיסים ישראליים, מסנן לפי טעם של אבא (תיאטרון/מוזיקה/סטנדאפ ולא קלאסי/אופרה/בלט), ובונה דף HTML יומי עם:

- כרטיסיות בסגנון אפליקציית סטרימינג, מסגרת בצבע ז'אנר
- מיון לפי "התאמה לטעם" (מבוסס preferences.yaml + פעולות נעיצה/הסתרה ב-localStorage)
- תצוגת לוח שנה אלטרנטיבית
- מחיר נסיעה מהבית (Nominatim, מקומי)
- העשרה אוטומטית מ-Wikipedia + מסך קישורים לחיפושים בספוטיפיי/יוטיוב/ביקורות
- שאלון טעם היררכי + עורך העדפות במודל

## הרצה מקומית

```bash
uv sync
uv run python -m src.main
open output/digest.html
```

האופציה `--no-web-enrich` מדלגת על שליפת תיאורים/תמונות מויקיפדיה (חוסך כדקה בריצה מקומית).

## ארכיטקטורה

```
src/
  main.py              orchestrator
  scrapers/            17 site-specific scrapers
  models.py            Show dataclass
  store.py             SQLite (first_seen, perf-count history)
  distance.py          OSM Nominatim geocoder + Haversine
  image_cache.py       local image cache (Referer hotlink fix)
  web_enrich.py        Wikipedia/DDG description+poster fallback
  enrich.py            search-URL builders (Spotify, YouTube, …)
  preferences.py       match_score + dislike filter
  render.py            Jinja2 → HTML

templates/
  digest.html          single-page app, vanilla JS, RTL Hebrew

data/
  curated_culture.yaml   curated lists of Israeli artists/actors/directors
  venues_candidates.yaml geocoded venue list (45-min radius from home)

config.yaml              sources, freshness windows, output path
preferences.yaml         seed prefs (overridden by browser localStorage)
```

## הוספת מקור (אתר חדש)

1. כתוב `src/scrapers/X.py` שמממש את `Scraper` מ-`base.py`
2. רשום ב-`src/scrapers/__init__.py` תחת `REGISTRY`
3. הוסף ל-`config.yaml` תחת `sources:` (כולל `geocode_query`)

## פריסה אוטומטית (Claude Routine)

ראה את הפרומפט של ה-routine ב-`.claude/routine_prompt.md`.
