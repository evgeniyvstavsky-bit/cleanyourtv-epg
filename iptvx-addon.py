#!/usr/bin/env python3
"""Add IPTVX schedules without changing existing XMLTV programmes."""
import argparse
import csv
import copy
import gzip
import os
import re
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "https://iptvx.one/epg/epg_lite.xml.gz"
EXISTING = {
    2: "711", 8: "2141", 67: "799", 142: "2295",
    162: "2051", 164: "2054", 165: "2050", 169: "2005"
}

def read_xml(path):
    with open(path, "rb") as f:
        zipped = f.read(2) == b"\x1f\x8b"
    with (gzip.open(path, "rb") if zipped else open(path, "rb")) as f:
        result = ET.parse(f).getroot()
    if result.tag != "tv":
        raise RuntimeError("Not XMLTV")
    return result

def parse_time(value):
    m = re.match(
        r"^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?",
        value or ""
    )
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    minutes = 0
    if m.group(2):
        minutes = int(m.group(3)) * 60 + int(m.group(4))
        if m.group(2) == "-":
            minutes = -minutes
    return dt.replace(
        tzinfo=timezone(timedelta(minutes=minutes))
    ).astimezone(timezone.utc)

def download(path):
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "HomeTV-EPG/1.0",
            "Accept-Encoding": "identity"
        }
    )
    total = 0
    with urllib.request.urlopen(req, timeout=60) as response:
        with path.open("wb") as output:
            while True:
                block = response.read(65536)
                if not block:
                    break
                total += len(block)
                if total > 45 * 1024 * 1024:
                    raise RuntimeError("IPTVX download exceeds 45 MB")
                output.write(block)
    with path.open("rb") as f:
        if f.read(2) != b"\x1f\x8b":
            raise RuntimeError("IPTVX response is not gzip")
    return total

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=str(ROOT / "site/epg.xml.gz"))
    parser.add_argument("--map", default=str(ROOT / "iptvx-map.csv"))
    parser.add_argument("--staged", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    base = Path(args.base)
    target = Path(args.output) if args.output else base

    if not base.is_file():
        raise RuntimeError("Base XMLTV not found: " + str(base))

    with open(args.map, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if len(rows) != 62:
        raise RuntimeError("Expected exactly 62 mappings")

    numbers = [int(r["number"]) for r in rows]
    sources = [r["source_id"] for r in rows]

    if len(set(numbers)) != 62 or len(set(sources)) != 62:
        raise RuntimeError("Duplicate number or source ID")

    root = read_xml(base)

    old_channels = root.findall("channel")
    old_programmes = root.findall("programme")
    old_ids = {
        item.get("id", ""): item
        for item in old_channels
    }

    if len(old_ids) != len(old_channels):
        raise RuntimeError("Duplicate existing XMLTV IDs")

    old_counts = Counter(
        p.get("channel", "") for p in old_programmes
    )

    assignments = {}
    reused_existing = {}
    new_channels = []

    for r in rows:
        number = int(r["number"])
        name = r["channel"].strip()
        source = r["source_id"].strip()

        if not name or not source:
            raise RuntimeError("Empty mapping field")

        if number in EXISTING:
            dest = EXISTING[number]
            if dest not in old_ids:
                raise RuntimeError("Missing legacy ID: " + dest)
            if old_counts[dest]:
                # Preserve upstream programmes; never add overlapping IPTVX copies.
                reused_existing[dest] = old_counts[dest]
        else:
            dest = f"hometv.extra.{number:03d}"
            if dest in old_ids:
                raise RuntimeError("Duplicate new ID: " + dest)
            channel = ET.Element("channel", {"id": dest})
            ET.SubElement(channel, "display-name").text = name
            new_channels.append(channel)

        assignments[source] = dest

    if len(new_channels) != 54:
        raise RuntimeError("Expected 54 new channel declarations")

    # XMLTV requires channel declarations before programme entries.
    insert_at = len(old_channels)
    for item in new_channels:
        root.insert(insert_at, item)
        insert_at += 1

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=48)
    counts = Counter()
    titles = defaultdict(set)
    intervals = defaultdict(list)
    additions = []
    seen = set()

    if args.staged:
        # Offline test uses the previously validated 62-channel file.
        staged_root = read_xml(Path(args.staged))
        programme_iter = staged_root.findall("programme")
        staged_mode = True
    else:
        staged_mode = False
        temporary = tempfile.TemporaryDirectory()
        source_file = Path(temporary.name) / "iptvx.xml.gz"
        print("Downloading IPTVX-LITE...", flush=True)
        print("Downloaded bytes:", download(source_file), flush=True)

        def stream():
            with gzip.open(source_file, "rb") as f:
                context = ET.iterparse(f, events=("start", "end"))
                _, document_root = next(context)
                counter = 0
                for event, elem in context:
                    if event != "end":
                        continue
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag == "programme":
                        yield elem
                        elem.clear()
                        counter += 1
                        if counter % 5000 == 0:
                            document_root.clear()
                    elif tag == "channel":
                        elem.clear()
        programme_iter = stream()

    for element in programme_iter:
        source = element.get("channel", "")

        if staged_mode:
            m = re.fullmatch(r"hometv\.extra\.(\d{3})", source)
            if not m:
                continue
            number = int(m.group(1))
            row = next((r for r in rows if int(r["number"]) == number), None)
            if row is None:
                continue
            source = row["source_id"]

        if source not in assignments or assignments[source] in reused_existing:
            continue

        start = parse_time(element.get("start"))
        stop = parse_time(element.get("stop"))

        if not start or not stop or not (
            start < stop and stop > now and start < horizon
        ):
            continue

        title = next(
            (
                (child.text or "").strip()
                for child in element
                if child.tag.rsplit("}", 1)[-1] == "title"
                and (child.text or "").strip()
            ),
            ""
        )
        if not title:
            continue

        dest = assignments[source]
        signature = (dest, element.get("start"), element.get("stop"))
        if signature in seen:
            continue
        seen.add(signature)

        node = copy.deepcopy(element)
        node.set("channel", dest)
        additions.append(node)
        counts[dest] += 1
        titles[dest].add(title)
        intervals[dest].append((max(now, start), min(horizon, stop)))

    if not staged_mode:
        temporary.cleanup()

    for r in rows:
        dest = assignments[r["source_id"]]
        if dest in reused_existing:
            # Already populated and validated by generate.py; do not overwrite.
            continue
        blocks = sorted(intervals[dest])
        merged = []

        for start, stop in blocks:
            if not merged or start > merged[-1][1]:
                merged.append([start, stop])
            elif stop > merged[-1][1]:
                merged[-1][1] = stop

        covered = sum(
            (stop - start).total_seconds() for start, stop in merged
        )
        coverage = 100 * covered / (48 * 3600)

        # RTG_PARTIAL_EXCEPTION_V1: keep only real RTG-TV programmes.
        # Do NOT substitute the different RTG HD / International feeds.
        # All other channels retain the original >=50% / >=3-title policy.
        is_rtg_tv = (
            int(r["number"]) == 141
            and r["source_id"].strip() == "rtg-tv"
            and dest == "hometv.extra.141"
        )
        if is_rtg_tv and 10 <= coverage < 50 and counts[dest] >= 10 and len(titles[dest]) >= 10:
            print(
                f"WARNING: RTG TV PARTIAL: {counts[dest]} authentic programmes, "
                f"{coverage:.1f}% of next 48h. Other programmes untouched.",
                flush=True,
            )
            # This is a documented partial-guide exception, NOT a PASS at >=50%.
            continue
        if counts[dest] == 0 or coverage < 50 or len(titles[dest]) < 3:
            raise RuntimeError(
                f"Insufficient schedule for {r['channel']}: "
                f"{counts[dest]} programmes, {coverage:.1f}% coverage"
            )

    for item in additions:
        root.append(item)

    expected_channels = len(old_channels) + 54
    expected_programmes = len(old_programmes) + len(additions)

    destination_ids = [
        c.get("id") for c in root.findall("channel")
    ]

    if (
        len(destination_ids) != expected_channels
        or len(set(destination_ids)) != expected_channels
        or len(root.findall("programme")) != expected_programmes
    ):
        raise RuntimeError("Merged XMLTV validation failed")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".building")

    try:
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            ET.ElementTree(root).write(
                f, encoding="utf-8", xml_declaration=True
            )

        checked = read_xml(tmp)

        if (
            len(checked.findall("channel")) != expected_channels
            or len(checked.findall("programme")) != expected_programmes
        ):
            raise RuntimeError("Written XMLTV failed validation")

        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

    print("\n========== IPTVX ADDON RESULT ==========")
    print("Existing channels:", len(old_channels))
    print("Added channels:", len(new_channels))
    print("Existing IDs reusing their original programmes:", reused_existing)
    print("Total channels:", expected_channels)
    print("Existing programmes:", len(old_programmes))
    print("Added programmes:", len(additions))
    print("Total programmes:", expected_programmes)
    print("Output:", target)

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit("STOP: " + str(error))
