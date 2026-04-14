"""Storage adapter. v0 ships an in-memory implementation with a
FerretDB/MongoDB-shaped interface so swapping to the real DB later is
mechanical."""
from purejson import Document, Collection
from extjson import ObjectId


class Store:
    """Abstract interface. Not typed — duck-typed."""

    def collection(self, name):
        raise NotImplementedError


class _Collection:
    """Minimal collection API: insert_one, insert_many, find, find_one, count."""

    def __init__(self):
        self._docs = []

    def insert_one(self, doc):
        doc = Document(dict(doc))
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self._docs.append(doc)
        return doc["_id"]

    def insert_many(self, docs):
        return [self.insert_one(d) for d in docs]

    def find_one(self, filter_=None):
        for d in self._docs:
            if _matches(d, filter_ or {}):
                return d
        return None

    def find(self, filter_=None, sort=None, limit=None):
        hits = [d for d in self._docs if _matches(d, filter_ or {})]
        if sort:
            for key, direction in reversed(list(sort.items())):
                hits.sort(key=lambda d: _sort_key(d, key), reverse=(direction == -1))
        if limit is not None:
            hits = hits[:limit]
        return Collection(hits)

    def count(self, filter_=None):
        return sum(1 for d in self._docs if _matches(d, filter_ or {}))


def _sort_key(doc, path):
    try:
        return doc[path]
    except (KeyError, Exception):
        return None


def _matches(doc, filter_):
    for k, v in filter_.items():
        try:
            actual = doc[k]
        except (KeyError, Exception):
            return False
        if isinstance(v, dict):
            for op, operand in v.items():
                if op == "$gte" and not (actual >= operand):
                    return False
                elif op == "$gt" and not (actual > operand):
                    return False
                elif op == "$lte" and not (actual <= operand):
                    return False
                elif op == "$lt" and not (actual < operand):
                    return False
                elif op == "$eq" and not (actual == operand):
                    return False
                elif op == "$ne" and not (actual != operand):
                    return False
                elif op == "$in" and actual not in operand:
                    return False
        else:
            if actual != v:
                return False
    return True


class InMemoryStore(Store):
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = _Collection()
        return self._collections[name]
