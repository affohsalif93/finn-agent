"""Extraction eval: an extracted profile against the hand-written gold profile (ARCHITECTURE §12).

Semantic, not textual: covenants are matched by `metric_type`; recipes are compared after
**flattening** numerator and denominator into weighted building blocks with their conditions
(by plan fact), so definition ids, source ids and the nesting of definitions may differ as long as
the computation is the same. On top, each gold definition is compared component by component with
the extracted definition of the same term, and the clauses around the formula (operator,
threshold, unit, rounding, plan check, breach, cure), dismissed-mention categories, conditions,
pricing grid and examples.

`answer_key_checks` runs the calc engine with the extracted profiles on the sample plan: the
results must reproduce the answer key (SCOPE.md §3) and the V3 contract example (1.54).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from finn.models.contract import Component, ContractProfile, Covenant, Definition

# Answer key (SCOPE.md §3) per (contract, metric type); V1 has no covenants.
ANSWER_KEY: dict[tuple[str, str], Decimal] = {
    ("V2", "economic_equity_ratio"): Decimal("34.32"),
    ("V3", "dscr"): Decimal("1.56"),
    ("V4", "equity_ratio"): Decimal("30.7"),
    ("V4", "net_debt_to_ebitda"): Decimal("3.37"),
}
EXAMPLE_KEY: dict[tuple[str, str], Decimal] = {("V3", "dscr"): Decimal("1.54")}
NO_COVENANTS = {"V1"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    expected: str = ""
    actual: str = ""

    def line(self) -> str:
        mark = "[ok]" if self.ok else "[!!]"
        if self.ok:
            return f"{mark} {self.name}"
        return f"{mark} {self.name}: erwartet {self.expected} | extrahiert {self.actual}"


@dataclass
class ContractEval:
    contract_id: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(c.ok for c in self.checks)

    @property
    def score(self) -> float:
        return self.passed / len(self.checks) if self.checks else 1.0

    def lines(self, verbose: bool = False) -> list[str]:
        head = f"{self.contract_id}: {self.passed}/{len(self.checks)} Prüfpunkte ({self.score:.0%})"
        body = [f"  {c.line()}" for c in self.checks if verbose or not c.ok]
        return [head, *body]


# ── Flattening ───────────────────────────────────────────────────────────────

LeafKey = tuple[str, str]  # (building block, condition as "exclude_if:<fact>" / "only_if:<fact>")


@dataclass
class Leaf:
    weight: Decimal = Decimal(0)
    if_missing: set[str] = field(default_factory=set)
    loan_filters: set[str] = field(default_factory=set)


def flatten(
    profile: ContractProfile,
    components: Sequence[Component],
    factor: Decimal = Decimal(1),
    cond: str = "",
    _seen: frozenset[str] = frozenset(),
) -> dict[LeafKey, Leaf]:
    """Weighted building blocks of a recipe; refs expanded, conditions mapped to plan facts."""
    out: dict[LeafKey, Leaf] = {}
    for c in components:
        own = _condition(profile, c)
        condition = own or cond
        if c.ref is not None:
            definition = profile.definitions.get(c.ref)
            if definition is None or c.ref in _seen:
                key = (f"ref:{c.ref}", condition)
                out.setdefault(key, Leaf()).weight += factor * c.weight
                continue
            sub = flatten(
                profile, definition.components, factor * c.weight, condition, _seen | {c.ref}
            )
            for key, leaf in sub.items():
                _merge(out.setdefault(key, Leaf()), leaf)
            continue
        assert c.item is not None
        leaf = out.setdefault((c.item.value, condition), Leaf())
        leaf.weight += factor * c.weight
        leaf.if_missing.add(c.if_missing)
        leaf.loan_filters |= set(c.loan_filters)
    return {k: v for k, v in out.items() if v.weight != 0}


def _merge(target: Leaf, leaf: Leaf) -> None:
    target.weight += leaf.weight
    target.if_missing |= leaf.if_missing
    target.loan_filters |= leaf.loan_filters


def _condition(profile: ContractProfile, c: Component) -> str:
    for kind, cond_id in (("exclude_if", c.exclude_if), ("only_if", c.only_if)):
        if cond_id is not None:
            condition = profile.conditions.get(cond_id)
            return f"{kind}:{condition.plan_fact.value if condition else cond_id}"
    return ""


def _fmt_leaves(leaves: dict[LeafKey, Leaf]) -> str:
    parts = []
    for (item, cond), leaf in sorted(leaves.items()):
        w = leaf.weight.normalize()
        parts.append(f"{w}×{item}" + (f"[{cond}]" if cond else ""))
    return " + ".join(parts) or "–"


# ── Comparison ───────────────────────────────────────────────────────────────


def compare(gold: ContractProfile, extracted: ContractProfile | None) -> ContractEval:
    ev = ContractEval(gold.contract_id)
    add = ev.checks.append
    if extracted is None:
        add(Check("Profil vorhanden", False, "Profil", "fehlt"))
        return ev
    add(
        _eq(
            "has_financial_covenants",
            gold.has_financial_covenants,
            extracted.has_financial_covenants,
        )
    )
    gold_types = sorted(c.metric_type for c in gold.covenants)
    ext_types = sorted(c.metric_type for c in extracted.covenants)
    add(_eq("Covenants (metric_type)", gold_types, ext_types))
    for g in gold.covenants:
        e = next((c for c in extracted.covenants if c.metric_type == g.metric_type), None)
        if e is None:
            add(Check(f"{g.covenant_id} gefunden", False, g.metric_type, "fehlt"))
            continue
        ev.checks += _compare_covenant(gold, g, extracted, e)
    add(_eq("Bedingungen (plan_fact)", _conditions(gold), _conditions(extracted)))
    add(_eq("Margenraster", _grid(gold), _grid(extracted)))
    add(_eq("Beispielrechnungen", _examples(gold), _examples(extracted)))
    add(_eq("Darlehensnummer", _loan_numbers(gold), _loan_numbers(extracted)))
    gold_cats = sorted({m.category for m in gold.dismissed_mentions})
    ext_cats = {m.category for m in extracted.dismissed_mentions}
    missing = [c for c in gold_cats if c not in ext_cats]
    add(
        Check(
            "Verworfene Erwähnungen (Kategorien)",
            not missing,
            ", ".join(gold_cats),
            f"fehlt: {', '.join(missing)}" if missing else ", ".join(sorted(ext_cats)),
        )
    )
    return ev


def _compare_covenant(
    gp: ContractProfile, g: Covenant, ep: ContractProfile, e: Covenant
) -> list[Check]:
    p = f"{g.covenant_id}"
    checks = [
        _eq(f"{p} operator", g.operator, e.operator),
        _eq(f"{p} Grenzwert", [t.value for t in g.thresholds], [t.value for t in e.thresholds]),
        _eq(f"{p} unit", g.unit, e.unit),
        _eq(f"{p} Rundung (Nachkommastellen)", g.rounding.decimals, e.rounding.decimals),
        _eq(f"{p} Planprüfung Folge", g.plan_check.consequence, e.plan_check.consequence),
        _eq(
            f"{p} Planstatus erforderlich",
            g.plan_check.plan_status_required,
            e.plan_check.plan_status_required,
        ),
        _eq(f"{p} Planbasis", sorted(g.plan_check.plan_basis), sorted(e.plan_check.plan_basis)),
        _eq(f"{p} Verletzung Folge", g.breach_consequence.type, e.breach_consequence.type),
        _eq(f"{p} Heilung erlaubt", _cure(g), _cure(e)),
    ]
    for side in ("numerator", "denominator"):
        gl = flatten(gp, getattr(g, side))
        el = flatten(ep, getattr(e, side))
        weights_ok = {k: v.weight for k, v in gl.items()} == {k: v.weight for k, v in el.items()}
        checks.append(
            Check(
                f"{p} {side} (Bausteine × Gewicht [Bedingung])",
                weights_ok,
                _fmt_leaves(gl),
                _fmt_leaves(el),
            )
        )
        common = [k for k in gl if k in el]
        missing_g = {k: sorted(gl[k].if_missing) for k in common}
        missing_e = {k: sorted(el[k].if_missing) for k in common}
        checks.append(_eq(f"{p} {side} if_missing", _fmt_map(missing_g), _fmt_map(missing_e)))
        filters_g = {k: sorted(gl[k].loan_filters) for k in common if gl[k].loan_filters}
        filters_e = {k: sorted(el[k].loan_filters) for k in common if el[k].loan_filters}
        if filters_g or filters_e:
            checks.append(_eq(f"{p} {side} loan_filters", _fmt_map(filters_g), _fmt_map(filters_e)))
    checks += _compare_definitions(gp, g, ep, e)
    return checks


def _compare_definitions(
    gp: ContractProfile, g: Covenant, ep: ContractProfile, e: Covenant
) -> list[Check]:
    """Each definition the gold covenant uses vs. the extracted definition of the same term."""
    from finn.calc.recipe import covenant_definitions

    checks: list[Check] = []
    ext_ids = covenant_definitions(ep, e)
    for def_id in sorted(covenant_definitions(gp, g)):
        gd = gp.definitions[def_id]
        match = _match_definition(gd, def_id, ep, ext_ids)
        name = f"{g.covenant_id} Definition {gd.term or def_id}"
        if match is None:
            checks.append(Check(name, False, gd.term or def_id, "fehlt"))
            continue
        _, ed = match
        g_leaves = flatten(gp, gd.components)
        e_leaves = flatten(ep, ed.components)
        same = {k: v.weight for k, v in g_leaves.items()} == {
            k: v.weight for k, v in e_leaves.items()
        }
        checks.append(
            Check(f"{name} Bestandteile", same, _fmt_leaves(g_leaves), _fmt_leaves(e_leaves))
        )
        if gd.exclusions:
            checks.append(
                Check(
                    f"{name} Ausschlüsse erfasst",
                    bool(ed.exclusions),
                    "; ".join(x.text for x in gd.exclusions),
                    "; ".join(x.text for x in ed.exclusions) or "keine",
                )
            )
        if gd.unmapped_components:
            effects = sorted(u.effect for u in ed.unmapped_components)
            checks.append(
                _eq(
                    f"{name} nicht abbildbare Teile",
                    sorted(u.effect for u in gd.unmapped_components),
                    effects,
                )
            )
    return checks


def _match_definition(
    gd: Definition, def_id: str, ep: ContractProfile, candidates: set[str]
) -> tuple[str, Definition] | None:
    def norm(x: str | None) -> str:
        return (x or "").casefold().replace(" ", "")

    for ed_id in sorted(candidates):
        ed = ep.definitions[ed_id]
        if norm(ed.term) == norm(gd.term) and gd.term:
            return ed_id, ed
    if def_id in ep.definitions:
        return def_id, ep.definitions[def_id]
    return None


def _cure(c: Covenant) -> str:
    if c.cure is None:
        return "nicht geregelt"
    return "ja" if c.cure.allowed else "nein"


def _conditions(p: ContractProfile) -> list[str]:
    return sorted({c.plan_fact.value for c in p.conditions.values()})


def _grid(p: ContractProfile) -> str:
    g = p.pricing_grid
    if g is None:
        return "keins"
    steps = ", ".join(
        f"≤{s.upper}→{s.margin_pct}" if s.upper is not None else f"→{s.margin_pct}" for s in g.steps
    )
    return f"Start {g.initial_margin_pct}; {steps}"


def _examples(p: ContractProfile) -> list[str]:
    out = []
    for e in p.examples:
        cov = p.covenant(e.covenant_id)
        out.append(f"{cov.metric_type if cov else e.covenant_id} = {e.expected}")
    return sorted(out)


def _loan_numbers(p: ContractProfile) -> list[str]:
    return sorted(str(loan.match.loan_number) for loan in p.loans)


def _fmt_map(m: dict[LeafKey, list[str]]) -> str:
    return (
        "; ".join(
            f"{k[0]}{f'[{k[1]}]' if k[1] else ''}: {','.join(v)}" for k, v in sorted(m.items())
        )
        or "–"
    )


def _eq(name: str, expected: object, actual: object) -> Check:
    return Check(name, expected == actual, _show(expected), _show(actual))


def _show(x: object) -> str:
    if isinstance(x, list):
        return ", ".join(str(v) for v in x) or "–"
    return "–" if x is None else str(x)


# ── Answer key with the extracted profiles ───────────────────────────────────


def answer_key_checks(profiles: Sequence[ContractProfile], plan_path: Path) -> list[Check]:
    """Run the calc engine with `profiles` on the sample plan; compare with the answer key."""
    from finn.calc.engine import calculate
    from finn.contracts.selftest import example_value
    from finn.plan import read_plan

    result = read_plan(plan_path)
    report = calculate(result.plan, list(profiles), result.findings)
    checks: list[Check] = []
    for (cid, metric), expected in ANSWER_KEY.items():
        r = next(
            (x for x in report.results if x.contract_id == cid and x.metric_type == metric), None
        )
        actual = "fehlt" if r is None else f"{r.value_rounded} ({r.status})"
        ok = r is not None and r.value_rounded == expected and r.status == "met"
        checks.append(Check(f"Antwortschlüssel {cid} {metric}", ok, f"{expected} (met)", actual))
    for cid in sorted(NO_COVENANTS):
        p = next((x for x in profiles if x.contract_id == cid), None)
        ok = p is not None and not p.has_financial_covenants and not p.covenants
        actual = "fehlt" if p is None else ", ".join(c.covenant_id for c in p.covenants) or "keine"
        checks.append(Check(f"Antwortschlüssel {cid} keine Covenants", ok, "keine", actual))
    for (cid, metric), expected in EXAMPLE_KEY.items():
        p = next((x for x in profiles if x.contract_id == cid), None)
        values = []
        if p is not None:
            for ex in p.examples:
                cov = p.covenant(ex.covenant_id)
                if cov is not None and cov.metric_type == metric:
                    values.append(example_value(p, ex))
        ok = expected in values
        shown = ", ".join("nicht berechenbar" if v is None else str(v) for v in values) or "fehlt"
        checks.append(Check(f"Vertragsbeispiel {cid} {metric}", ok, str(expected), shown))
    return checks


def evaluate_dirs(
    extracted: Sequence[ContractProfile], gold: Sequence[ContractProfile]
) -> list[ContractEval]:
    by_id = {p.contract_id: p for p in extracted}
    return [compare(g, by_id.get(g.contract_id)) for g in gold]
