"""Fetch public NSE daily archives for the fixed, later PCR validation window."""
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

CACHE = Path('/private/tmp/pcr_validation_cache')
START = date(2026, 5, 20)
END = date(2026, 9, 4)


def main():
    CACHE.mkdir(exist_ok=True)
    manifest = []
    day = START
    while day <= END:
        if day.weekday() < 5:
            filename = f'BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip'
            target = CACHE / filename
            url = 'https://nsearchives.nseindia.com/content/fo/' + filename
            row = {'date': day.isoformat(), 'url': url}
            try:
                if target.exists():
                    raw = target.read_bytes()
                else:
                    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(request, timeout=25) as response:
                        raw = response.read()
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    if archive.testzip() is not None:
                        raise ValueError('ZIP integrity check failed')
                target.write_bytes(raw)
                row.update(status='downloaded', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            except Exception as exc:
                row.update(status='unavailable', error=str(exc))
            manifest.append(row)
            (CACHE / 'download_manifest.json').write_text(json.dumps(manifest, indent=2))
            print(day, row['status'], row.get('error', row.get('bytes')), flush=True)
            time.sleep(0.2)
        day += timedelta(days=1)
    successes = sum(row['status'] == 'downloaded' for row in manifest)
    print(f'Downloaded {successes}/{len(manifest)} weekday files into {CACHE}', flush=True)
    if not successes:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
