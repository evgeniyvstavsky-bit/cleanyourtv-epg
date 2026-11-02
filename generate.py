#!/usr/bin/env python3
"""Build a validated, backward-compatible HomeTV XMLTV site feed.

Existing channels.json IDs and EPG.ONE programme IDs are always preserved.
Additional confirmed HomeTV channels are labelled with their exact launcher
names, so TvActivity.parseXmltv can match via display-name without M3U edits.
No guessed timeshift schedules are emitted.
"""
import copy
import csv
import gzip
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / 'site'
MAIN_URL = 'https://cdn.epg.one/ru.xml.gz'
EXTRA_URL = 'https://iptv-epg.org/files/epg-ru.xml.gz'
NOW = datetime.now(timezone.utc)
START = NOW - timedelta(hours=3)
END = NOW + timedelta(hours=72)


def xmltime(value):
    match = re.match(r'^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?', value or '')
    if not match:
        return None
    try:
        date = datetime.strptime(match.group(1), '%Y%m%d%H%M%S')
        if match.group(2):
            offset = int(match.group(3)) * 60 + int(match.group(4))
            if match.group(2) == '-':
                offset = -offset
            date -= timedelta(minutes=offset)
        return date.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def download(url, destination):
    print('Downloading:', url, flush=True)
    subprocess.run([
        'curl', '--http1.1', '-fL', '--connect-timeout', '15',
        '--max-time', '180', '--retry', '3', '--retry-all-errors',
        '--retry-delay', '4', '-o', str(destination), url
    ], check=True)
    size = destination.stat().st_size
    if size < 200000 or size > 45000000:
        raise RuntimeError(f'Unexpected size for {url}: {size}')
    with destination.open('rb') as f:
        if f.read(2) != b'\x1f\x8b':
            raise RuntimeError(f'Not a gzip EPG: {url}')
    print('Downloaded compressed bytes:', size)


def stream_programmes(filename):
    with gzip.open(filename, 'rb') as stream:
        for _, element in ET.iterparse(stream, events=('end',)):
            tag = element.tag.rsplit('}', 1)[-1]
            if tag == 'programme':
                yield element
                element.clear()
            elif tag == 'channel':
                element.clear()


mapping = json.loads((ROOT / 'channels.json').read_text(encoding='utf-8'))
legacy = {str(x['xmltv_id']): str(x['name']) for x in mapping}
if len(legacy) != len(mapping) or len(legacy) < 60:
    raise RuntimeError('Invalid channels.json; refusing to change published EPG')

rows = list(csv.DictReader((ROOT / 'hometv-matching.csv').open(encoding='utf-8-sig', newline='')))
if len(rows) != 230 or len({r['new_tvg_id'] for r in rows}) != 230:
    raise RuntimeError('Unexpected HomeTV matching report; expected 230 unique records')

confirmed = [r for r in rows if r['status'] == 'MATCHED' and r['source'] in ('HomeTV', 'IPTV-EPG-RU')]
if len(confirmed) < 100:
    raise RuntimeError(f'Matching report regressed: {len(confirmed)} confirmed')

# Keep exact channel names. Java matches display-name before XMLTV ID.
# No playlist tvg-id changes required.
extra_by_source = {'HomeTV': defaultdict(list), 'IPTV-EPG-RU': defaultdict(list)}
for r in confirmed:
    ident = r['new_tvg_id'].strip()
    source_id = r['source_id'].strip()
    if not ident.startswith('hometv.') or not source_id or not r['channel'].strip():
        raise RuntimeError('Bad confirmed EPG mapping row')
    if r['source'] == 'HomeTV' and source_id not in legacy:
        raise RuntimeError(f'Confirmed EPG.ONE ID missing from channels.json: {source_id}')
    extra_by_source[r['source']][source_id].append(r)

SITE.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
    folder = Path(folder)
    source1 = folder / 'epgone.gz'
    source2 = folder / 'iptv-epg.gz'
    result = folder / 'epg.xml.gz'
    download(MAIN_URL, source1)
    download(EXTRA_URL, source2)

    count_by_id = Counter()
    now_by_id = set()
    future_by_id = set()
    unique = set()
    total = 0

    with gzip.open(result, 'wb', compresslevel=6) as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(b'<tv generator-info-name="HomeTV combined EPG">\n')

        for cid, name in legacy.items():
            node = ET.Element('channel', {'id': cid})
            ET.SubElement(node, 'display-name').text = name
            out.write(ET.tostring(node, encoding='utf-8') + b'\n')

        for r in confirmed:
            node = ET.Element('channel', {'id': r['new_tvg_id']})
            ET.SubElement(node, 'display-name').text = r['channel']
            # Original ID is useful for provenance but display-name carries matching.
            out.write(ET.tostring(node, encoding='utf-8') + b'\n')

        def write_programme(element, target_id, start, stop):
            global total
            # Preserve all original programme metadata and times.
            marker = (target_id, element.get('start'), element.get('stop'))
            if marker in unique:
                return
            unique.add(marker)
            node = copy.deepcopy(element)
            node.set('channel', target_id)
            out.write(ET.tostring(node, encoding='utf-8') + b'\n')
            count_by_id[target_id] += 1
            total += 1
            if start <= NOW < stop:
                now_by_id.add(target_id)
            if stop > NOW:
                future_by_id.add(target_id)

        for label, src in [('HomeTV', source1), ('IPTV-EPG-RU', source2)]:
            included = 0
            for programme in stream_programmes(src):
                cid = programme.get('channel', '')
                target_ids = []
                if label == 'HomeTV' and cid in legacy:
                    target_ids.append(cid)
                target_ids.extend(r['new_tvg_id'] for r in extra_by_source[label].get(cid, []))
                if not target_ids:
                    continue
                start = xmltime(programme.get('start'))
                stop = xmltime(programme.get('stop'))
                if not start or not stop or not (start < stop and start < END and stop > START):
                    continue
                for tid in target_ids:
                    write_programme(programme, tid, start, stop)
                    included += 1
            print(f'{label}: included programme copies:', included)
        out.write(b'</tv>\n')

    old_now = sum(cid in now_by_id for cid in legacy)
    additional_now = sum(r['new_tvg_id'] in now_by_id for r in confirmed)
    additional_future = sum(r['new_tvg_id'] in future_by_id for r in confirmed)
    other_now = sum(r['new_tvg_id'] in now_by_id for r in confirmed if r['source'] == 'IPTV-EPG-RU')
    print(f'Original current coverage: {old_now} / {len(legacy)}')
    print(f'HomeTV confirmed now: {additional_now} / {len(confirmed)}')
    print(f'Additional-source now: {other_now} / 17 expected')
    print('Total programme copies:', total)

    # Never regress the existing feed or silently drop the extra source.
    if old_now < 25 or total < 1500 or additional_now < 65 or other_now < 8:
        raise RuntimeError('Insufficient current programme coverage; refusing publication')
    if result.stat().st_size > 7 * 1024 * 1024:
        raise RuntimeError('Result unexpectedly large; refusing publication')
    with gzip.open(result, 'rb') as f:
        root = ET.parse(f).getroot()
    written_channels = sum(ch.tag == 'channel' for ch in root)
    written_programmes = sum(ch.tag == 'programme' for ch in root)
    if written_channels != len(legacy) + len(confirmed) or written_programmes != total:
        raise RuntimeError('Output XMLTV validation mismatch')

    # All checks passed. The CI artifact is deployed only if this exits 0.
    os.replace(result, SITE / 'epg.xml.gz')

meta = {
    'generated_at_utc': NOW.isoformat(),
    'valid_window_end_utc': END.isoformat(),
    'source': 'EPG.ONE + IPTV-EPG.ORG',
    'legacy_channel_ids_preserved': len(legacy),
    'legacy_channels_now': old_now,
    'hometv_matched_ids': len(confirmed),
    'hometv_channels_now': additional_now,
    'hometv_channels_future': additional_future,
    'programmes': total,
    'bytes': (SITE / 'epg.xml.gz').stat().st_size,
}
(SITE / 'status.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
(SITE / 'index.html').write_text(
    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
    "<title>HomeTV EPG</title><h2>HomeTV EPG</h2>"
    "<a href='epg.xml.gz'>EPG XMLTV</a></html>", encoding='utf-8')
(SITE / '.nojekyll').touch()
print('SUCCESS: site/epg.xml.gz')
print(json.dumps(meta, ensure_ascii=False, indent=2))
