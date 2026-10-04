"""Session-bounded exact backend handles, independent of observation scope.

Records contain primitive metadata only, never browser/element/remote objects.
The serialized byte budget includes metadata and its immutable state signature.
"""

import copy
import json
import secrets
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass
class NodeRecord:
    root: str
    owner: str
    document: str
    backend: int
    signature: str
    metadata: dict
    size: int


class NodeRegistry:
    def __init__(self, max_bytes=2 * 1024 * 1024, max_entries=1024):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.bytes = 0
        self.records = OrderedDict()
        self.index = {}
        self.evicted = deque(maxlen=256)
        self.pinned = set()

    @staticmethod
    def _identity(record):
        return record.root, record.owner, record.document, record.backend, record.signature

    def _remove(self, node_id, *, evicted=False):
        record = self.records.pop(node_id)
        self.bytes -= record.size
        if self.index.get(self._identity(record)) == node_id:
            self.index.pop(self._identity(record), None)
        if evicted:
            self.evicted.append(node_id)

    def synchronize(self, owners):
        """Remove records whose exact owner/document is no longer alive."""
        for node_id, record in list(self.records.items()):
            if owners.get((record.root, record.owner)) != record.document:
                self._remove(node_id)

    def remember(self, root, owner, document, backend, signature, metadata, *, node_id=None):
        identity = root, owner, document, backend, signature
        node_id = node_id or self.index.get(identity) or "node_" + secrets.token_urlsafe(10)
        frozen = copy.deepcopy(metadata)
        size = len(json.dumps(frozen, ensure_ascii=False).encode()) + len(signature.encode()) + 384
        if node_id in self.records:
            self._remove(node_id)
        record = NodeRecord(root, owner, document, backend, signature, frozen, size)
        self.records[node_id] = record
        self.index[identity] = node_id
        self.bytes += size
        while self.records and (
            self.bytes > self.max_bytes or len(self.records) > self.max_entries
        ):
            candidate = next((nid for nid in self.records if nid not in self.pinned), None)
            self._remove(candidate or next(iter(self.records)), evicted=True)
        return node_id

    def get(self, node_id, root):
        record = self.records.get(node_id)
        if record is None or record.root != root:
            return None
        self.records.move_to_end(node_id)
        return record

    def missing_reason(self, node_id):
        return "registry_evicted" if node_id in self.evicted else "target_unavailable"
