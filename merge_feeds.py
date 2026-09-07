#!/usr/bin/env python3
"""Merge a freshly-scraped PAGASA feed with the previously-published one.

v3 — SCRAPE-ORDER pubDates (definitive fix for skipped/blocked advisories).

Why: PAGASA's "Issued at" time is unreliable for ordering. We hit three
failures in a row -- AM/PM misparses (an 11:50 AM read as 11:50 PM), same-number
reissues, and duplicate timestamps. Any ordering based on the issued time can be
jumped by a bad timestamp, which sticks the RSS trigger's watermark and hides
genuine new advisories.

Fix: order and de-duplicate by FIRST-SEEN scrape time (the moment WE first
captured the item), not by PAGASA's issued time. Genuinely new advisories are
always scraped later, so they always get the newest pubDate and are detected.
A misparsed/phantom item settles at its own first-seen time and can never block
new items again. The real issued time is still shown in the item description /
card, so nothing user-visible is lost.

Undated items (e.g. the Taal special forecast) stay pinned to a stable past date
so they never float to "newest".

Also keeps:
- 24h retention safety net
- future-dated item drop (belt-and-suspenders for 12h misparses)
- strictly-unique pubDates (RSS connector requirement)

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

# Items whose pubDate is more than this far in the FUTURE are misparses.
_FUTURE_GRACE = timedelta(hours=2)
# Items with a pubDate at/older than this are "undated" (pinned to stable past);
# their ordering must NOT be overridden with scrape time.
_PINNED_BEFORE = datetime(2001, 1, 1, tzinfo=timezone.utc)


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


def get_seen(item):
    el = item.find(SEEN_TAG)
    if el is not None and el.text:
        try:
            return parsedate_to_datetime(el.text)
        except (TypeError, ValueError):
            pass
    return None


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
    future_limit = now + _FUTURE_GRACE

    new_root, new_channel, new_items = load_items(args.new)
    if new_channel is None:
        print("Error: new feed missing/invalid; nothing to write.", file=sys.stderr)
        sys.exit(1)

    _, _, old_items = load_items(args.old)
    old_seen = {k: get_seen(v) for k, v in old_items.items()}

    for item in list(new_channel.findall("item")):
        new_channel.remove(item)

    merged = {}

    # 1) NEW items. Preserve FIRST-seen time if we saw it before; else now.
    #    Drop future-dated (misparsed) items.
    for key, item in new_items.items():
        if get_pub(item) > future_limit:
            continue
        prior = old_seen.get(key)
        set_seen(item, prior if prior else now)
        merged[key] = item

    # 2) OLD items not in NEW -> retain if within window and not future-dated.
    for key, item in old_items.items():
        if key in merged:
            continue
        if get_pub(item) > future_limit:
            continue
        seen = old_seen.get(key) or get_pub(item)
        if seen.timestamp() >= cutoff:
            merged[key] = item

    # 3) Re-base pubDate on FIRST-SEEN scrape time for dated advisories, so
    #    ordering/detection follows when WE captured each item (reliable),
    #    not PAGASA's issued time (unreliable). Leave pinned/undated items
    #    (year <= 2000, e.g. Taal) untouched so they never float to newest.
    for item in merged.values():
        if get_pub(item) < _PINNED_BEFORE:
            continue
        seen = get_seen(item) or now
        set_pub(item, seen)

    # 4) Sort newest-first, then enforce strictly-decreasing (unique) pubDates.
    ordered = sorted(merged.values(), key=get_pub, reverse=True)
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
        f"(scrape-order pubDates) -> {args.output}"
    )


if __name__ == "__main__":
    main()
