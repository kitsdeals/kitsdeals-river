"""Append-only JSONL log of decisions per evaluated deal.

This is the structured memory layer the SDK provides between watcher runs:
- Dedup signal: future runs read recent entries and skip duplicates
- Self-improvement: user feedback on past notifications informs future ones
- Audit trail: operator can grep history of every decision

Format: one JSON object per line. Append-only by design. Rotation cuts in
when the file grows past ``MAX_BYTES``; older entries archive to a
``.<n>.jsonl`` sibling file. The watcher only reads the current file's
recent tail by default — long-term history lives in archives.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Rotate at 10MB. At ~500 bytes/decision that's ~20K decisions per file —
# more than a year of activity at any realistic rate.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_KEEP_ARCHIVES = 5


class DecisionsLog:
    """JSONL-backed decision log with size-based rotation."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_archives: int = DEFAULT_KEEP_ARCHIVES,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max_bytes
        self.keep_archives = keep_archives
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        deal_id: str,
        decision: str,
        reason: str,
        message_sent: str | None = None,
        canonical_product_id: str | None = None,
        feedback: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Append one decision entry. Idempotent on the file system: if
        rotation needs to happen, it happens before the write so we never
        leave a half-rotated state.

        Required fields are positional-by-keyword to encourage call-site
        clarity; ``extra`` is the escape hatch for skill-specific metadata
        we don't want to bake into the schema yet.
        """
        self._maybe_rotate()
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "deal_id": deal_id,
            "decision": decision,
            "reason": reason,
            "message_sent": message_sent,
            "canonical_product_id": canonical_product_id,
            "feedback": feedback,
        }
        if extra:
            # Don't let extra clobber the canonical fields above
            for k, v in extra.items():
                if k not in entry:
                    entry[k] = v
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """Read the last ``n`` entries, newest-last (chronological order).

        Reads only the current file (not archives) — that's the practical
        memory window for an evaluator. If you need to scan history, call
        :meth:`iter_all` and filter yourself.
        """
        if not self.path.exists():
            return []
        # Cheap approach for small-ish files: read the whole thing, take the
        # last n. JSONL files at our scale are well under 10MB so this is
        # fine. If scale grows, switch to a reverse-line iterator.
        lines: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    # Corrupted line — skip rather than fail the whole tail
                    continue
        return lines[-n:] if n else lines

    def iter_all(self) -> Iterator[dict[str, Any]]:
        """Iterate every entry across the current file and all archives, in
        chronological order. Useful for long-running synthesis tasks."""
        # Iterate archives oldest-first, then current. Filename pattern:
        # <name>.<N>.jsonl. Earliest archive has highest N (it shifted there
        # in the most rotations), so reverse the index sort.
        import re
        archive_re = re.compile(rf"^{re.escape(self.path.name)}\.(\d+)\.jsonl$")
        candidates = []
        for p in self.path.parent.glob(self.path.name + ".*.jsonl"):
            m = archive_re.match(p.name)
            if m:
                candidates.append((int(m.group(1)), p))
        archives = [p for _idx, p in sorted(candidates, key=lambda t: t[0], reverse=True)]
        for archive in archives:
            yield from _iter_jsonl(archive)
        if self.path.exists():
            yield from _iter_jsonl(self.path)

    def has_recent_canonical(
        self, canonical_product_id: str | None, *, within_n: int = 50
    ) -> bool:
        """Return True if a recent entry referenced this canonical product.

        Tail-scope only — for the dedup hot path. ``within_n`` bounds the
        scan so this stays cheap.
        """
        if not canonical_product_id:
            return False
        for entry in self.tail(within_n):
            if entry.get("canonical_product_id") == canonical_product_id:
                return True
        return False

    def _maybe_rotate(self) -> None:
        if not self.path.exists():
            return
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return

        # Roll: path.<keep_archives-1>.jsonl is dropped; each path.<n>.jsonl
        # shifts to path.<n+1>.jsonl; current path becomes path.0.jsonl.
        # Last-write-wins on a crash mid-rotation: at worst we lose one
        # archive boundary, never the live file.
        oldest = self.path.with_name(f"{self.path.name}.{self.keep_archives - 1}.jsonl")
        if oldest.exists():
            oldest.unlink()
        for i in range(self.keep_archives - 2, -1, -1):
            src = self.path.with_name(f"{self.path.name}.{i}.jsonl")
            if src.exists():
                src.rename(self.path.with_name(f"{self.path.name}.{i + 1}.jsonl"))
        self.path.rename(self.path.with_name(f"{self.path.name}.0.jsonl"))


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
