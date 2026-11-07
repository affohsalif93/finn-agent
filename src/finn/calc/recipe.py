"""Whitelist and structure checks for recipes, and their dependencies (ARCHITECTURE §3.3, D23).

Pure functions over a `ContractProfile`. The store runs `recipe_issues` at load time; a covenant
with an issue is reported as `manual_review` (PROFILE_INVALID_RECIPE), never guessed.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from finn.models.contract import Component, ContractProfile, Covenant
from finn.models.plan import FactKey, ItemKey


@dataclass(frozen=True)
class RecipeIssue:
    covenant_id: str | None  # None: profile-level (e.g. an unused but broken definition)
    message: str


def recipe_issues(profile: ContractProfile) -> list[RecipeIssue]:
    """Refs resolve within the contract, no cycles, conditions exist, example inputs known."""
    issues: list[RecipeIssue] = []
    used_defs: set[str] = set()
    for cov in profile.covenants:
        # Recurses into the definitions it uses, so a broken definition flags each covenant.
        problems = _component_problems(profile, [*cov.numerator, *cov.denominator], path=())
        issues.extend(RecipeIssue(cov.covenant_id, p) for p in problems)
        used_defs |= covenant_definitions(profile, cov)
    for def_id, definition in profile.definitions.items():
        if def_id in used_defs:
            continue
        problems = _component_problems(profile, definition.components, path=(def_id,))
        issues.extend(RecipeIssue(None, f"Definition {def_id}: {p}") for p in problems)
    known_inputs = {i.value for i in ItemKey} | set(profile.definitions)
    for example in profile.examples:
        unknown = sorted(set(example.inputs) - known_inputs)
        if unknown:
            issues.append(
                RecipeIssue(example.covenant_id, f"Beispiel: unbekannte Eingaben {unknown}")
            )
    return issues


def _component_problems(
    profile: ContractProfile, components: list[Component], path: tuple[str, ...]
) -> list[str]:
    problems: list[str] = []
    for c in components:
        for cond in (c.exclude_if, c.only_if):
            if cond is not None and cond not in profile.conditions:
                problems.append(f"Bedingung {cond} nicht definiert")
        if c.ref is None:
            continue
        if c.ref not in profile.definitions:
            problems.append(f"Definition {c.ref} nicht vorhanden")
        elif c.ref in path:
            problems.append(f"Zirkelbezug {' → '.join((*path, c.ref))}")
        else:
            problems += _component_problems(
                profile, profile.definitions[c.ref].components, (*path, c.ref)
            )
    return list(dict.fromkeys(problems))


def iter_components(
    profile: ContractProfile, components: list[Component], _seen: frozenset[str] = frozenset()
) -> Iterator[Component]:
    """All components, refs expanded recursively (stops at missing refs and cycles)."""
    for c in components:
        yield c
        if c.ref and c.ref in profile.definitions and c.ref not in _seen:
            yield from iter_components(
                profile, profile.definitions[c.ref].components, _seen | {c.ref}
            )


def covenant_definitions(profile: ContractProfile, cov: Covenant) -> set[str]:
    return {
        c.ref
        for c in iter_components(profile, [*cov.numerator, *cov.denominator])
        if c.ref is not None
    }


@dataclass(frozen=True)
class Dependencies:
    items: frozenset[ItemKey]
    facts: frozenset[FactKey]
    definitions: frozenset[str]
    uses_loans: bool
    keys: frozenset[str]  # items + facts as strings (to match `Finding.items`)


def dependencies(profile: ContractProfile, cov: Covenant) -> Dependencies:
    """Building blocks and plan facts a covenant needs (to map plan findings to covenants)."""
    comps = list(iter_components(profile, [*cov.numerator, *cov.denominator]))
    items = {c.item for c in comps if c.item is not None}
    facts = {
        profile.conditions[cond].plan_fact
        for c in comps
        for cond in (c.exclude_if, c.only_if)
        if cond is not None and cond in profile.conditions
    }
    uses_loans = any(c.loan_filters for c in comps)
    if uses_loans:
        items.add(ItemKey.SPECIAL_REPAYMENTS_TOTAL)
    keys = frozenset({i.value for i in items} | {f.value for f in facts})
    return Dependencies(
        items=frozenset(items),
        facts=frozenset(facts),
        definitions=frozenset(covenant_definitions(profile, cov)),
        uses_loans=uses_loans,
        keys=keys,
    )
