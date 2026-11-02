import gzip
import json
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
SOURCE_URL = "https://cdn.epg.one/ru.xml.gz"

NOW = datetime.now(timezone.utc)
START = NOW - timedelta(hours=3)
END = NOW + timedelta(hours=72)

def xmltime(value):
    match = re.match(
        r"^(\d{14})(?:\s*([+-])(\d{2})(\d{2}))?",
        value or ""
    )
    if not match:
        return None

    try:
        date = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")

        if match.group(2):
            offset = int(match.group(3)) * 60 + int(match.group(4))
            if match.group(2) == "-":
                offset = -offset
            date -= timedelta(minutes=offset)

        return date.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

mapping = json.loads(
    (ROOT / "channels.json").read_text(encoding="utf-8")
)

ids = {str(item["xmltv_id"]): item["name"] for item in mapping}

if len(ids) != len(mapping) or len(ids) < 60:
    raise RuntimeError("Invalid channel mapping")

SITE.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory() as directory:
    source = Path(directory) / "source.xml.gz"
    temp_output = Path(directory) / "compact.xml.gz"

    print("Downloading:", SOURCE_URL, flush=True)

    subprocess.run([
        "curl", "--http1.1", "-fL",
        "--connect-timeout", "15",
        "--max-time", "180",
        "--retry", "3",
        "--retry-all-errors",
        "--retry-delay", "5",
        "-o", str(source),
        SOURCE_URL,
    ], check=True)

    if not 1000000 < source.stat().st_size < 40000000:
        raise RuntimeError("Unexpected source size")

    print("Source bytes:", source.stat().st_size)

    counts = Counter()
    current = set()

    with gzip.open(temp_output, "wb", compresslevel=6) as output:
        output.write(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<tv generator-info-name="CleanYourTV Auto EPG">\n'
        )

        for cid, name in ids.items():
            node = ET.Element("channel", {"id": cid})
            ET.SubElement(node, "display-name").text = name
            output.write(ET.tostring(node, encoding="utf-8") + b"\n")

        with gzip.open(source, "rb") as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                tag = element.tag.rsplit("}", 1)[-1]

                if tag == "programme":
                    cid = element.get("channel", "")

                    if cid in ids:
                        start = xmltime(element.get("start"))
                        stop = xmltime(element.get("stop"))

                        if (
                            start is not None
                            and stop is not None
                            and start < stop
                            and start < END
                            and stop > START
                        ):
                            output.write(
                                ET.tostring(element, encoding="utf-8")
                                + b"\n"
                            )
                            counts[cid] += 1

                            if start <= NOW < stop:
                                current.add(cid)

                    element.clear()

                elif tag == "channel":
                    element.clear()

        output.write(b"</tv>\n")

    total = sum(counts.values())

    print("Channels with programmes:", len(counts))
    print("Channels currently airing:", len(current))
    print("Total programmes:", total)

    # Prevent replacing a useful guide with an empty/stale one.
    if len(current) < 25 or total < 1500:
        raise RuntimeError(
            "Source EPG has insufficient current coverage"
        )

    with gzip.open(temp_output, "rb") as stream:
        parsed_channels = 0
        parsed_programmes = 0

        for _, element in ET.iterparse(stream, events=("end",)):
            tag = element.tag.rsplit("}", 1)[-1]

            if tag == "channel":
                parsed_channels += 1
                element.clear()
            elif tag == "programme":
                parsed_programmes += 1
                element.clear()

    if parsed_channels != len(ids) or parsed_programmes != total:
        raise RuntimeError("XMLTV validation failed")

    if temp_output.stat().st_size > 5 * 1024 * 1024:
        raise RuntimeError("Compact EPG unexpectedly large")

    # Publish only after all validations succeed.
    os.replace(temp_output, SITE / "epg.xml.gz")

metadata = {
    "generated_at_utc": NOW.isoformat(),
    "valid_window_end_utc": END.isoformat(),
    "mapped_channels": len(ids),
    "channels_with_programmes": len(counts),
    "channels_now": len(current),
    "programmes": total,
    "bytes": (SITE / "epg.xml.gz").stat().st_size,
    "source": "EPG.ONE",
}

(SITE / "status.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

(SITE / "index.html").write_text(
    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
    "<title>CleanYourTV EPG</title>"
    "<h2>CleanYourTV EPG</h2>"
    "<p>Automated XMLTV feed</p>"
    "<a href='epg.xml.gz'>EPG XMLTV</a></html>",
    encoding="utf-8"
)

(SITE / ".nojekyll").touch()

print("SUCCESS:", SITE / "epg.xml.gz")
print(json.dumps(metadata, ensure_ascii=False, indent=2))
