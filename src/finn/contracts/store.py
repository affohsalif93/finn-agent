"""Load reviewed contract profiles (ARCHITECTURE §3, D2) and check them against the PDFs.

`load_profiles(profiles_dir, contracts_dir)` never raises for a bad profile: it returns the
usable profiles plus findings (FAILURES.md §G), so one broken contract does not stop the others.
"""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from finn.calc.recipe import recipe_issues
from finn.errors import ErrorCode, Finding, finding
from finn.models.contract import ContractProfile


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profiles(
    profiles_dir: Path, contracts_dir: Path | None = None
) -> tuple[list[ContractProfile], list[Finding]]:
    """All `*.json` profiles in `profiles_dir` (sorted by contract id) + findings.

    `<id>.review.json` (the onboarding review next to a profile) is not a profile and skipped.
    """
    profiles: list[ContractProfile] = []
    findings: list[Finding] = []
    for path in profile_paths(profiles_dir):
        profile, problems = load_profile(path)
        findings += problems
        if profile is None:
            continue
        if any(p.contract_id == profile.contract_id for p in profiles):
            findings.append(
                finding(ErrorCode.PROFILE_MISSING, file=path.name, detail="doppelte contract_id")
            )
            continue
        profiles.append(profile)
        findings += check_profile(profile, contracts_dir)
    if contracts_dir is not None and contracts_dir.is_dir():
        known = {p.document.file for p in profiles}
        findings += [
            finding(ErrorCode.CONTRACT_NEW_FILE, file=pdf.name)
            for pdf in sorted(contracts_dir.glob("*.pdf"))
            if pdf.name not in known
        ]
    profiles.sort(key=lambda p: p.contract_id)
    return profiles, findings


REVIEW_SUFFIX = ".review.json"


def profile_paths(profiles_dir: Path) -> list[Path]:
    return sorted(p for p in profiles_dir.glob("*.json") if not p.name.endswith(REVIEW_SUFFIX))


def load_profile(path: Path) -> tuple[ContractProfile | None, list[Finding]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [finding(ErrorCode.PROFILE_MISSING, file=path.name, detail=_short(exc))]
    try:
        return ContractProfile.model_validate(data), []
    except ValidationError as exc:
        return None, [_validation_finding(path, exc)]


def _validation_finding(path: Path, exc: ValidationError) -> Finding:
    """Map the first error to PROFILE_INVARIANT / PROFILE_INVALID_RECIPE / PROFILE_MISSING."""
    errors = exc.errors(include_url=False)
    details = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in errors[:3]]
    detail = "; ".join(details) + (f" (+{len(errors) - 3} weitere)" if len(errors) > 3 else "")
    messages = " ".join(str(e["msg"]) for e in errors)
    if "PROFILE_INVARIANT" in messages:
        return finding(ErrorCode.PROFILE_INVARIANT, file=path.name, detail=detail)
    recipe_locs = ("components", "numerator", "denominator")
    if "PROFILE_INVALID_RECIPE" in messages or any(
        any(part in recipe_locs for part in map(str, e["loc"])) for e in errors
    ):
        return finding(
            ErrorCode.PROFILE_INVALID_RECIPE, variant="profile", file=path.name, detail=detail
        )
    return finding(ErrorCode.PROFILE_MISSING, file=path.name, detail=detail)


def check_profile(profile: ContractProfile, contracts_dir: Path | None) -> list[Finding]:
    """Review status, contract file hash, recipe structure (per covenant)."""
    out: list[Finding] = []
    if profile.review.status != "reviewed":
        out.append(finding(ErrorCode.PROFILE_NOT_REVIEWED, contract=profile.contract_id))
    if contracts_dir is not None:
        pdf = contracts_dir / profile.document.file
        if not pdf.is_file():
            out.append(
                finding(
                    ErrorCode.CONTRACT_CHANGED,
                    variant="missing",
                    contract=profile.contract_id,
                    file=profile.document.file,
                )
            )
        elif sha256_file(pdf) != profile.document.sha256:
            out.append(
                finding(
                    ErrorCode.CONTRACT_CHANGED,
                    contract=profile.contract_id,
                    file=profile.document.file,
                )
            )
    covenant_ids = [c.covenant_id for c in profile.covenants]
    for issue in recipe_issues(profile):
        out.append(
            finding(
                ErrorCode.PROFILE_INVALID_RECIPE,
                covenant=issue.covenant_id or "(Profil)",
                contract=profile.contract_id,
                detail=issue.message,
                affects=[issue.covenant_id] if issue.covenant_id in covenant_ids else [],
            )
        )
    return out


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) <= 120 else text[:119] + "…"
