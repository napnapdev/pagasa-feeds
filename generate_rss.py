#!/usr/bin/env python3


import argparse
import os
import sys
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime

from lxml import etree as ET

PF_NS = "https://pagasa-feeds.local/ns"
PF = "{%s}" % PF_NS
SEEN_TAG = PF + "seen"


def load_items(path):
    """Return (root, channel, {guid: item_element}) or (None, None, {})."""
    if not path or not os.path.exists(path):
        return None, None, {}
    try:
        tree = ET.parse(path)
    except Exception as e:  # noqa: BLE001
        print(f"Warning: could not parse {path}: {e}", file=sys.stderr)
        return None, None, {}
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        return root, None, {}
    items = {}
    for item in channel.findall("item"):
        guid_el = item.find("guid")
        key = guid_el.text if guid_el is not None and guid_el.text else None
        if key is None:
            title_el = item.find("title")
            key = title_el.text if title_el is not None else str(id(item))
        items[key] = item
    return root, channel, items


def get_seen(item, default):
    el = item.find(SEEN_TAG)
    if el is not None and el.text:
        try:
            return parsedate_to_datetime(el.text)
        except (TypeError, ValueError):
            pass
    return default


def set_seen(item, dt):
    el = item.find(SEEN_TAG)
    if el is None:
        el = ET.SubElement(item, SEEN_TAG)
    el.text = format_datetime(dt.astimezone(timezone.utc))


def item_sort_key(item):
    pub = item.find("pubDate")
    if pub is not None and pub.text:
        try:
            return parsedate_to_datetime(pub.text)
        except (TypeError, ValueError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("output")
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - args.hours * 3600

    new_root, new_channel, new_items = load_items(args.new)
    if new_channel is None:
        print("Error: new feed missing/invalid; nothing to write.", file=sys.stderr)
        sys.exit(1)

    _, _, old_items = load_items(args.old)

    # Start from the NEW feed's root/channel (fresh channel metadata),
    # then rebuild its item list as a merge.
    for item in list(new_channel.findall("item")):
        new_channel.remove(item)

    merged = {}

    # 1) All NEW items -> seen = now (they are currently active).
    for key, item in new_items.items():
        set_seen(item, now)
        merged[key] = item

    # 2) OLD items not in NEW -> keep if within retention window.
    for key, item in old_items.items():
        if key in merged:
            continue
        seen = get_seen(item, default=item_sort_key(item))
        if seen.timestamp() >= cutoff:
            merged[key] = item  # retain (still fresh)
        # else: too old -> drop

    # Sort newest first by pubDate and re-attach.
    for item in sorted(merged.values(), key=item_sort_key, reverse=True):
        new_channel.append(item)

    ET.indent(new_root, space="  ")
    ET.ElementTree(new_root).write(
        args.output, encoding="utf-8", xml_declaration=True
    )
    print(
        f"Merged {len(new_items)} new + retained "
        f"{len(merged) - len(new_items)} old = {len(merged)} items -> {args.output}"
    )


if __name__ == "__main__":
    main()
