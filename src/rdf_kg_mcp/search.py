"""Free-text search over labels, aliases and descriptions, indexed once when the bundle loads.

Scoring is BM25F-like: each query word is matched against the words of each field, after light
suffix stripping, either exactly or as a prefix of a longer word. Field matches are weighted
(label 3, alias 2, description and declared text 1) and normalised by field length, so a short
name that matches beats a long sentence that mentions the word. The text score is multiplied by
a class-rarity prior: entities of rare classes (a handful of use cases, models or tasks) rank
above the many records of common classes (results, evaluations) that repeat the same word.
Results are then diversified so that one class fills at most a few of the leading places.

Nothing here is specific to one dataset. Only statements outside the ontology graph are indexed.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pyoxigraph as ox

ALT_LABEL = "http://www.w3.org/2004/02/skos/core#altLabel"
FIELD_WEIGHTS = {"label": 3.0, "alias": 2.0, "description": 1.0, "text": 1.0}
PREFIX_WEIGHT = 0.6
MIN_PREFIX = 4
RARITY_EXPONENT = 2.0
PER_CLASS_LEAD = 3
K1 = 1.2
B = 0.75
_WORD = re.compile(r"[^\W_]+")
_SUFFIXES = ("ing", "ed", "es", "e", "s")
UNTYPED = ""


def stem(word: str) -> str:
    """Strip one common English suffix, keeping at least four characters."""
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def words(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text.casefold())]


@dataclass
class _Doc:
    lengths: dict[str, int] = field(default_factory=dict)
    rarity: float = 1.0
    group: str = UNTYPED


class SearchIndex:
    """An inverted index from word stems to entities and per-field term counts."""

    def __init__(self) -> None:
        self.postings: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)
        self.docs: dict[str, _Doc] = {}
        self.average: dict[str, float] = {}
        self._terms: list[str] = []

    @classmethod
    def build(
        cls,
        quads_by_field: dict[str, Iterable[Any]],
        types: dict[str, set[str]],
        searchable: set[str] | None = None,
    ) -> SearchIndex:
        """quads_by_field maps a field name to quads whose literal objects hold its text.
        searchable, if given, limits results to these subjects (others are not indexed)."""
        index = cls()
        for name, quads in quads_by_field.items():
            for quad in quads:
                subject, obj = quad.subject, quad.object
                if not isinstance(subject, ox.NamedNode) or not isinstance(obj, ox.Literal):
                    continue
                iri = subject.value
                if searchable is not None and iri not in searchable:
                    continue
                tokens = words(obj.value)
                if not tokens:
                    continue
                doc = index.docs.setdefault(iri, _Doc())
                doc.lengths[name] = doc.lengths.get(name, 0) + len(tokens)
                for token in tokens:
                    counts = index.postings[token].setdefault(iri, {})
                    counts[name] = counts.get(name, 0) + 1
        index._finish(types)
        return index

    def _finish(self, types: dict[str, set[str]]) -> None:
        totals: Counter[str] = Counter()
        counts: Counter[str] = Counter()
        for doc in self.docs.values():
            for name, length in doc.lengths.items():
                totals[name] += length
                counts[name] += 1
        self.average = {name: totals[name] / counts[name] for name in totals}
        class_sizes: Counter[str] = Counter()
        for iri in self.docs:
            for cls in types.get(iri) or {UNTYPED}:
                class_sizes[cls] += 1
        n = max(1, len(self.docs))
        for iri, doc in self.docs.items():
            classes = types.get(iri) or {UNTYPED}
            # The entity's rarest class sets its prior and its diversification group.
            doc.group = min(classes, key=lambda c: (class_sizes[c], c))
            doc.rarity = math.log(1 + n / class_sizes[doc.group])
        self._terms = sorted(self.postings)

    def __len__(self) -> int:
        return len(self.docs)

    def _expand(self, stem_: str) -> list[tuple[str, float]]:
        matches = [(stem_, 1.0)] if stem_ in self.postings else []
        if len(stem_) < MIN_PREFIX:
            return matches
        i = bisect.bisect_right(self._terms, stem_)
        while i < len(self._terms) and self._terms[i].startswith(stem_):
            matches.append((self._terms[i], PREFIX_WEIGHT))
            i += 1
        return matches

    def score(self, text: str) -> tuple[dict[str, float], dict[str, set[str]]]:
        """Scores and matched fields for every entity matching any query word."""
        scores: dict[str, float] = defaultdict(float)
        matched: dict[str, set[str]] = defaultdict(set)
        n = max(1, len(self.docs))
        for query_stem in dict.fromkeys(words(text)):
            for term, weight in self._expand(query_stem):
                posting = self.postings[term]
                idf = math.log(1 + (n - len(posting) + 0.5) / (len(posting) + 0.5))
                for iri, fields in posting.items():
                    doc = self.docs[iri]
                    total = 0.0
                    for name, tf in fields.items():
                        norm = 1 - B + B * doc.lengths[name] / self.average[name]
                        total += FIELD_WEIGHTS.get(name, 1.0) * tf * (K1 + 1) / (tf + K1 * norm)
                        matched[iri].add(name)
                    scores[iri] += weight * idf * total
        for iri in scores:
            scores[iri] *= self.docs[iri].rarity ** RARITY_EXPONENT
        return scores, matched

    def rank(self, scores: dict[str, float]) -> list[str]:
        """Order by score, letting each class take at most PER_CLASS_LEAD of the leading places."""
        ordered = sorted(scores, key=lambda iri: (-scores[iri], iri))
        seen: Counter[str] = Counter()
        lead, rest = [], []
        for iri in ordered:
            group = self.docs[iri].group
            (lead if seen[group] < PER_CLASS_LEAD else rest).append(iri)
            seen[group] += 1
        return lead + rest
