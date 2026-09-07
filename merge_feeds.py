#!/usr/bin/env python3
"""Merge a freshly-scraped PAGASA feed with the previously-published one.

Retain-for-24h safety net + STRICTLY-UNIQUE pubDates.

PAGASA reissues advisories (e.g. two "No. 23" with the same issued time), which
produced DUPLICATE pubDates in the merged feed. Microsoft's RSS connector
requires each item's pubDate to be strictly greater than the previous one --
duplicates cause it to stop detecting new items. After merging we now enforce
strictly-decreasing (hence unique) pubDates by nudging any collision down by a
second, without changing the newest item.

Usage:
    python merge_feeds.py OLD.rss NEW.rss OUTPUT.rss [--hours 24]
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

from lxml import etree as ET

PF_NS = "https://pagasa-feeds.local/ns"
PF = "{%s}" % PF_NS
SEEN_TAG = PF + "seen"


def load_items(path):
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


def get_pub(item):
    pub = item.find("pubDate")
    if pub is not None and pub.text:
        try:
            return parsedate_to_datetime(pub.text)
        except (TypeError, ValueError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def set_pub(item, dt):
    pub = item.find("pubDate")
    if pub is None:
        pub = ET.SubElement(item, "pubDate")
    pub.text = format_datetime(dt.astimezone(timezone.utc))


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

    for item in list(new_channel.findall("item")):
        new_channel.remove(item)

    merged = {}

    # 1) NEW items -> seen = now.
    for key, item in new_items.items():
        set_seen(item, now)
        merged[key] = item

    # 2) OLD items not in NEW -> retain if within window.
    for key, item in old_items.items():
        if key in merged:
            continue
        seen = get_seen(item, default=get_pub(item))
        if seen.timestamp() >= cutoff:
            merged[key] = item

    # Sort newest first by pubDate.
    ordered = sorted(merged.values(), key=get_pub, reverse=True)

    # 3) Enforce STRICTLY-DECREASING (unique) pubDates so the RSS connector can
    #    always distinguish items. Never raises the top item; only nudges
    #    duplicates/inversions downward by 1 second.
    for i in range(1, len(ordered)):
        above = get_pub(ordered[i - 1])
        cur = get_pub(ordered[i])
        if cur >= above:
            set_pub(ordered[i], above - timedelta(seconds=1))

    for item in ordered:
        new_channel.append(item)

    ET.indent(new_root, space="  ")
    ET.ElementTree(new_root).write(
        args.output, encoding="utf-8", xml_declaration=True
    )
    print(
        f"Merged {len(new_items)} new + retained "
        f"{len(merged) - len(new_items)} old = {len(merged)} items "
        f"(pubDates made strictly unique) -> {args.output}"
    )


if __name__ == "__main__":
    main()
