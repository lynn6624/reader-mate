"""Read-only validation for MCP saved-position summaries; no reader/server imports."""
import math
import re
from datetime import datetime, timedelta, timezone

TIME_ZONE = 'Asia/Shanghai'
LOCAL_TIME = timezone(timedelta(hours=8))


def saved_time(value):
    """This deployment stores naive datetime.now() in verified Asia/Shanghai time."""
    if not isinstance(value, str) or 'T' not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIME)
        return parsed.astimezone(LOCAL_TIME)
    except ValueError:
        return None


def time_key(value):
    return saved_time(value) or datetime.min.replace(tzinfo=LOCAL_TIME)


def _integer(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
        return number if not isinstance(value, float) or value == number else None
    except (ValueError, TypeError, OverflowError):
        return None


def verified_position(record, text, revision, locate, view):
    """Return a verified *saved* position, or None; revision zero is never a veto.

    Never default a missing offset/percent to zero or relocate an anchor by searching.
    Legacy anchors normalize both sides (old saves kept newlines); a mismatching
    anchor, content hash, length or coordinate is pending review, not a new position.
    """
    if not isinstance(record, dict):
        return None
    offset = _integer(record.get('offset'))
    if offset is None:
        return None
    position = locate(text, offset)
    if position is None or position[2] <= 0:
        return None
    if record.get('book_revision') and record['book_revision'] != revision:
        return None
    for field, expected in (('paragraph_idx', position[0]), ('char_offset', position[1])):
        if field in record and _integer(record[field]) != expected:
            return None
    if 'chars' in record and _integer(record['chars']) != position[2]:
        return None
    anchor = record.get('anchor')
    if anchor is not None and not isinstance(anchor, str):
        return None
    normalized = lambda value: re.sub(r'\s+', ' ', value).strip()
    anchored = bool(anchor) and normalized(anchor) == normalized(text[position[3]:position[3]+40])
    if anchor and not anchored:
        return None
    updated = saved_time(record.get('updated'))
    try:
        percentage = float(record['percent'])
        has_percentage = not isinstance(record['percent'], bool) and math.isfinite(percentage) and 0 <= percentage <= 1
    except (KeyError, TypeError, ValueError, OverflowError):
        has_percentage = False
    # Timestamp is evidence of an actual old save; hash+revision or a complete
    # anchor/size/percentage legacy tuple can also establish a save without it.
    complete_legacy = anchored and has_percentage and _integer(record.get('chars')) == position[2]
    versioned_save = record.get('book_revision') == revision and (_integer(record.get('revision')) or 0) > 0
    if not (updated or complete_legacy or versioned_save):
        return None
    if not record.get('book_revision') and not anchored:
        if not (has_percentage and _integer(record.get('chars')) == position[2]):
            return None
    mapped = view(record, text, revision)
    status = 'ok' if record.get('book_revision') else ('legacy_anchor_verified' if anchored else 'legacy_position_verified')
    return {'offset':offset, 'percent':offset/position[2], 'total_units':position[2],
            'chars':position[2], 'chapter':mapped.get('chapter') or '',
            'revision':max(0, _integer(record.get('revision')) or 0),
            'last_read_at':updated.isoformat(timespec='seconds') if updated else None,
            'book_revision':revision, 'location_status':status,
            'paragraph_idx':position[0], 'char_offset':position[1]}
