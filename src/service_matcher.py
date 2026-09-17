#!/usr/bin/env python3
"""Match a free-text line-item description to a contracted service.

Billing descriptions abbreviate and scramble contract wording:

    'Procedure Routine Urologic Biop /NG-3022'  ->  Routine Urologic Biopsy Procedure
    'Occ Supervised Ren Isolation Rm'           ->  Supervised Renal Isolation Room Occupancy

Matching is done by token alignment against the contract's own vocabulary,
not embeddings or an LLM: almost every abbreviation is a subsequence of the
word it shortens ('Rtn'->Routine), so this is closed-vocabulary alignment,
not semantic similarity. The rare exceptions that aren't subsequences
('Ent'->Otolaryngologic) are listed explicitly in SEMANTIC_ABBREVIATIONS
rather than guessed — see the decision log for why.

When no service scores above MIN_SCORE, or the top two are too close to
call, the line is left unmatched and its candidates (if any) are preserved
on the result rather than guessed. A line with no candidates at all is a
genuine `unknown_service`; a line with several tied candidates is not — the
pricing engine treats those differently. See the decision log for the
rationale behind both the alignment scoring and the unit-basis tiebreak.
"""

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
import json


STOPWORDS = {"of", "the", "and", "for", "per"}

# Matching thresholds. These are intentionally conservative: low-scoring or
# closely tied descriptions remain unresolved rather than being guessed.
MIN_SCORE = 0.60
MIN_MARGIN = 0.08

# Billing shorthand that isn't a subsequence of the contract word it means.
# Kept as an explicit list rather than inferred — see decision log.
SEMANTIC_ABBREVIATIONS = {
    "ent": "otolaryngologic",  # Ear, Nose & Throat
}


@dataclass
class MatchResult:
    description: str
    matched_service: str | None
    score: float
    runner_up: str | None
    runner_up_score: float
    reason: str  # 'matched' | 'matched_via_unit_basis' | 'ambiguous' | 'below_threshold'

    # Set when unit basis, not text, determined the match. Those lines can't
    # also be checked for wrong_unit_basis.
    used_unit_basis: bool = False
    candidates: tuple = ()


def normalise(description: str) -> list[str]:
    """Strip the /NG-#### code and punctuation, lowercase, tokenise."""
    text = re.sub(r"/[A-Z]{2}-\d+", " ", description)
    text = re.sub(r"[^A-Za-z ]", " ", text)
    return [t for t in text.lower().split() if t and t not in STOPWORDS]


def normalise_unit_basis(basis) -> list[str]:
    """'per_hour' -> ['per', 'hour']. Line items use underscores; the
    contract uses spaces and is sometimes longer ('per day of service')."""
    if basis is None:
        return []
    return [t for t in re.sub(r"[^a-z]", " ", str(basis).lower()).split() if t]


def unit_basis_compatible(billed, contracted) -> bool:
    """True when the billed basis matches the contracted one, allowing the
    contract's longer phrasing ('per_day' matches 'per day of service')."""
    billed_tokens = normalise_unit_basis(billed)
    contracted_tokens = normalise_unit_basis(contracted)
    return bool(billed_tokens) and billed_tokens == contracted_tokens[:len(billed_tokens)]


def is_subsequence(short: str, long: str) -> bool:
    """'rtn' is a subsequence of 'routine'; 'msk' of 'musculoskeletal'."""
    it = iter(long)
    return all(c in it for c in short)


def token_affinity(desc_token: str, service_token: str) -> float:
    """Score how well one description token explains one service token.

    Subsequence matches require the same first letter, so an abbreviation
    can only match the word it actually shortens: 'rtn' is a subsequence of
    both 'routine' and 'intermittent', but only shares a first letter with
    'routine'.
    """
    if desc_token == service_token:
        return 1.0

    if SEMANTIC_ABBREVIATIONS.get(desc_token) == service_token:
        return 1.0

    if service_token.startswith(desc_token):
        return 0.9

    if desc_token[0] == service_token[0] and is_subsequence(desc_token, service_token):
        return 0.75

    return 0.0


def alignment_score(desc_tokens: list[str], service_tokens: list[str]) -> float:
    """Greedy one-to-one token alignment, scored as a Dice coefficient.

    An unaligned description token disqualifies the candidate entirely; an
    unaligned service token does not. The vocabulary is closed, so a
    description token this candidate can't explain means the description
    names something else — whereas a service token nothing points at just
    means the biller omitted a detail.
    """
    if not desc_tokens or not service_tokens:
        return 0.0

    candidate_pairs = []
    for i, desc_token in enumerate(desc_tokens):
        for j, service_token in enumerate(service_tokens):
            affinity = token_affinity(desc_token, service_token)
            if affinity > 0:
                # Longer description tokens are more informative, so they
                # win competition for a service token.
                candidate_pairs.append((affinity, len(desc_token), i, j))
    candidate_pairs.sort(reverse=True)

    used_desc_indices, used_service_indices = set(), set()
    total = 0.0
    for affinity, _, i, j in candidate_pairs:
        if i in used_desc_indices or j in used_service_indices:
            continue
        used_desc_indices.add(i)
        used_service_indices.add(j)
        total += affinity

    if len(used_desc_indices) < len(desc_tokens):
        return 0.0

    return (2 * total) / (len(desc_tokens) + len(service_tokens))


class ServiceMatcher:
    """Matches a description to a contracted service in two passes: first
    by text alignment, then, if the text is ambiguous, by the line's billed
    unit basis. See the module docstring for why each pass works this way.
    """

    def __init__(self, services: list[str], service_rules: dict | None = None):
        self.services = services
        self.service_tokens = {
            service: [t for t in service.lower().split() if t not in STOPWORDS]
            for service in services
        }
        # service -> contracted unit basis, used only for the tiebreak below
        self.service_unit_basis = {
            service: (service_rules or {}).get(service, {}).get("unit_basis")
            for service in services
        }
        self._cache: dict[tuple, MatchResult] = {}

    def match(self, description: str, unit_basis_as_billed=None) -> MatchResult:
        key = (description, unit_basis_as_billed)
        if key in self._cache:
            return self._cache[key]

        desc_tokens = normalise(description)
        scored = sorted(
            ((alignment_score(desc_tokens, service_tokens), service)
             for service, service_tokens in self.service_tokens.items()),
            reverse=True,
        )

        best_score, best_service = scored[0] if scored else (0.0, None)
        second_score, second_service = scored[1] if len(scored) > 1 else (0.0, None)

        if best_score < MIN_SCORE:
            result = MatchResult(description, None, best_score, best_service,
                                 second_score, "below_threshold")

        elif best_score - second_score >= MIN_MARGIN:
            result = MatchResult(description, best_service, best_score, second_service,
                                 second_score, "matched")

        else:
            # Text is ambiguous between the tied candidates. Break the tie
            # with the billed unit basis — never with the billed price,
            # since that would assume the figure being audited is correct.
            tied = [service for score, service in scored if best_score - score < MIN_MARGIN]
            survivors = [
                service for service in tied
                if unit_basis_compatible(unit_basis_as_billed,
                                         self.service_unit_basis.get(service))
            ]
            if len(survivors) == 1:
                result = MatchResult(description, survivors[0], best_score,
                                     second_service, second_score,
                                     "matched_via_unit_basis",
                                     used_unit_basis=True, candidates=tuple(tied))
            else:
                result = MatchResult(description, None, best_score, second_service,
                                     second_score, "ambiguous", candidates=tuple(tied))

        self._cache[key] = result
        return result

    def match_all(self, descriptions, unit_bases=None) -> dict:
        """Return one cached result per distinct (description, unit basis) pair."""
        descriptions = list(descriptions)
        unit_bases = (
            list(unit_bases)
            if unit_bases is not None
            else [None] * len(descriptions)
        )
        pairs = set(zip(descriptions, unit_bases))
        return {
            pair: self.match(pair[0], pair[1])
            for pair in pairs
        }


def load_rules(rules_path: str) -> dict:
    with open(rules_path) as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", required=True, help="data/contract_rules/rules_hN.json")
    parser.add_argument("--line-items", required=True, help="hospital_N_line_items.csv")
    parser.add_argument("--output", default=None, help="write per-description matches to CSV")
    return parser.parse_args()


def print_match_summary(results: dict, lines) -> None:
    line_reason = [
        results[(desc, basis)].reason
        for desc, basis in zip(lines["description"], lines["unit_basis_as_billed"])
    ]

    by_reason = defaultdict(int)
    for result in results.values():
        by_reason[result.reason] += 1

    resolved = sum(1 for r in line_reason if r in ("matched", "matched_via_unit_basis"))

    print(f"distinct (description, unit basis) pairs: {len(results)}")
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason:24s} {count:5d}")
    print()
    print(f"line items: {len(lines)}")
    print(f"  resolved            {resolved:5d} ({resolved / len(lines):.1%})")
    print(f"    via text          {sum(1 for r in line_reason if r == 'matched'):5d}")
    print(f"    via unit basis    {sum(1 for r in line_reason if r == 'matched_via_unit_basis'):5d}")
    print(f"  unresolved          {len(lines) - resolved:5d} ({1 - resolved / len(lines):.1%})")

    unresolved = [r for r in results.values() if r.matched_service is None]
    if unresolved:
        print("\nUNRESOLVED:")
        for result in sorted(unresolved, key=lambda r: r.description):
            print(f"  {result.description!r}  ({result.reason}, best={result.score:.3f})")
            for candidate in result.candidates:
                print(f"       candidate: {candidate}")


def write_match_report(results: dict, output_path: str) -> None:
    with open(output_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["description", "unit_basis_as_billed", "matched_service",
                         "score", "reason", "used_unit_basis"])
        for (description, basis), result in sorted(results.items(), key=lambda item: str(item[0])):
            writer.writerow([description, basis, result.matched_service,
                             f"{result.score:.4f}", result.reason, result.used_unit_basis])
    print(f"\nWritten to {output_path}")


def main() -> None:
    args = parse_args()

    import pandas as pd

    rules = load_rules(args.rules)
    lines = pd.read_csv(args.line_items)

    matcher = ServiceMatcher(list(rules["services"]), rules["services"])
    results = matcher.match_all(lines["description"], lines["unit_basis_as_billed"])

    print_match_summary(results, lines)

    if args.output:
        write_match_report(results, args.output)


if __name__ == "__main__":
    main()