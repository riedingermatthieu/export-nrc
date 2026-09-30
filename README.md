# NRC → Strava export

Downloads your Nike Run Club runs and converts them to files Strava can import:

- **GPX** for outdoor runs with GPS: route, elevation, heart rate, and the run name as the Strava title
- **TCX** for indoor/treadmill runs without GPS: distance, duration, heart rate, calories

Needs Python 3.7 or newer. No dependencies.

## 1. Get a Nike access token

1. Log in at https://www.nike.com in a desktop browser.
2. Open DevTools (F12) and go to the **Network** tab. Filter on `api.nike.com`.
3. Refresh the page or open your profile. Click any request to `api.nike.com`.
4. Under **Request Headers**, copy the value of `Authorization` (the `Bearer eyJ...` part).

The token expires after about an hour. If it expires partway through, get a new one and run the script again. Runs that were already downloaded are cached in `raw/`.

## 2. Run the export

```powershell
python nrc_export.py              # prompts for the token
# or
$env:NRC_TOKEN = "eyJ..."; python nrc_export.py
```

The files go in `export/`. Other options:

| Option | Meaning |
|---|---|
| `--format auto\|gpx\|tcx` | `auto` (the default) writes GPX when a run has GPS and TCX when it doesn't |
| `--offline` | Skip downloading and only re-convert what's already in `raw/` |
| `--raw-dir`, `--out` | Change the cache and output folders |

## 3. Upload to Strava

Go to https://www.strava.com/upload/select and pick your files. You can select up to 25 at a time.
Strava skips duplicates, so runs that NRC already synced won't be imported twice.
