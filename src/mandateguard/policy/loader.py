"""Config and policy loading, with the invariants that matter enforced at load time.

Five rules are enforced here rather than trusted:
  * every policy rule cites a clause (an uncited rule is a hallucinated rule)
  * every policy rule **quotes** that clause, and the quote has to appear verbatim in the
    committed circular text -- a citation is a pointer, and a pointer can point at the
    wrong clause without anything noticing (T4.1)
  * every rule expression parses under `agent/expression.py`'s whitelist and reads only
    fields `MandateAuditContext` actually defines
  * the policy file's hash is recoverable, so every ledger entry can pin the exact
    policy version a decision was made under, and `replay` can reproduce it (T5.2)
  * the recovery parameters stay ordered and inside their measured ceiling (T1.2)
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from mandateguard.agent.expression import ExpressionError, referenced_names
from mandateguard.models import Channel, MandateAuditContext, PolicyRule, Rail

ROOT = Path(__file__).resolve().parents[3]
PARAMS_PATH = ROOT / "config" / "params.yaml"
POLICY_PATH = ROOT / "policy" / "mandate_policy.yaml"
POLICY_DIR = POLICY_PATH.parent


class ValueParams(BaseModel):
    """The four terms of the rupee price (T3.2), each traced to a different paper."""

    mu_good_outcome: float = Field(ge=0)
    nu_complaint: float = Field(ge=0)
    alpha_reachability: float = Field(ge=0)
    gamma_fatigue: float = Field(ge=0)
    fatigue_half_life_days: int = Field(gt=0)
    rho_template_reuse: float = Field(ge=0)
    backfire_avoided_per_softer_step: float = Field(ge=0, lt=1)
    """Chrome, USENIX Security 2021. Strictly below 1: at 1 the softest channel would
    carry exactly zero backfire, which would make it free to spam and hand every arm an
    unbounded ask budget through the back door."""


class RecoveryParams(BaseModel):
    """q and r, plus the empirical ceiling the r sweep is not allowed to leave.

    The invariants are enforced here rather than only in `models.Mandate`, because the
    YAML is edited by hand far more often than a `Mandate` is constructed: an
    inconsistent config should fail on load, not three layers downstream.
    """

    after_lapse: float = Field(ge=0, le=1)
    after_revocation: float = Field(ge=0, le=1)
    swept_ceiling_after_revocation: float = Field(ge=0, le=1)
    """Upper bound on r measured in T1.2 (docs/mapping.md 2). The sweep runs over
    (0, ceiling]; nothing above it has any evidence behind it."""

    @model_validator(mode="after")
    def check_ordering(self) -> RecoveryParams:
        if self.after_lapse <= self.after_revocation:
            raise ValueError(
                f"recovery.after_lapse (q={self.after_lapse}) must exceed "
                f"recovery.after_revocation (r={self.after_revocation}): a mandate the "
                "customer deliberately killed cannot be easier to win back than one "
                "that merely expired. See docs/problem.md 6.2."
            )
        if self.after_revocation > self.swept_ceiling_after_revocation:
            raise ValueError(
                f"recovery.after_revocation (r={self.after_revocation}) exceeds its "
                f"measured ceiling ({self.swept_ceiling_after_revocation}). The ceiling "
                "comes from T1.2; raising r above it means claiming revoked mandates "
                "recover more often than cancelled KKBox subscriptions did, with no "
                "evidence for it."
            )
        return self


class InterventionParams(BaseModel):
    """What an ask does to a mandate. Every field here is swept except the last.

    The validator enforces the one thing that is not a matter of taste: backfire has to
    grow with contact count. A configuration where the twelfth ask is *safer* than the
    first would quietly delete the entire reason this project rations asks, and every
    result would then say "contact everyone" -- correctly, for that world.
    """

    uplift_scale: float = Field(ge=0)
    backfire_first_ask: float = Field(ge=0, le=1)
    backfire_twelfth_ask: float = Field(ge=0, le=1)
    natural_revocation_share: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def check_backfire_grows(self) -> InterventionParams:
        if self.backfire_twelfth_ask < self.backfire_first_ask:
            raise ValueError(
                f"intervention.backfire_twelfth_ask ({self.backfire_twelfth_ask}) is "
                f"below backfire_first_ask ({self.backfire_first_ask}). Backfire that "
                "shrinks with repetition removes the reason to ration asks at all, and "
                "every arm in the ladder would then correctly recommend spraying. See "
                "docs/problem.md 5.1."
            )
        return self

    def backfire(self, ask_number: int) -> float:
        """`b(n)` for the nth ask this customer has received, 1-indexed.

        Geometric rather than linear between the two anchors, because the anchors are
        given as a ratio (0.6% to 6% is "ten times worse", not "5.4 points worse") and a
        linear ladder would put the growth in the wrong place.
        """
        if ask_number <= 1 or self.backfire_first_ask == 0:
            return self.backfire_first_ask
        ratio = self.backfire_twelfth_ask / self.backfire_first_ask
        return min(1.0, self.backfire_first_ask * ratio ** ((ask_number - 1) / 11))


class HorizonParams(BaseModel):
    weeks: int = Field(gt=0)
    budget_inr_per_week: float = Field(ge=0)


class IndiaParams(BaseModel):
    """The KKBox -> Indian-mandate bridge (T1.3). Every field here is a decision.

    The rail mix is the one that most needs guarding: it is a synthetic overlay, and a
    mix that does not sum to 1 would silently drop or double-count part of the book.
    """

    snapshot_date: date
    ntd_to_inr: float = Field(gt=0)
    rail_mix: dict[Rail, float]
    upi_autopay_afa_threshold_inr: float = Field(gt=0)
    mandate_validity_days: int = Field(gt=0)
    reachability_fraction_of_ltv: float = Field(ge=0)
    plausible_age_years: tuple[int, int]
    default_debit_frequency_days: int = Field(gt=0)

    @model_validator(mode="after")
    def check_mix(self) -> IndiaParams:
        if any(share < 0 for share in self.rail_mix.values()):
            raise ValueError("india.rail_mix shares must be non-negative")
        total = sum(self.rail_mix.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"india.rail_mix must sum to 1, got {total}. A mix that does not sum to "
                "1 silently drops or double-counts part of the mandate book."
            )
        low, high = self.plausible_age_years
        if not 0 < low < high:
            raise ValueError(f"india.plausible_age_years must be 0 < low < high, got {low}, {high}")
        return self


def _check_channels(channels: list[Channel]) -> list[Channel]:
    """Three things a channel table has to be before an optimiser can use it.

    **Names unique.** A `Decision` names its channel as a string and the harness looks it
    up; two channels sharing a name means one of them can never be selected and the other
    silently absorbs its decisions.

    **At least one intrusive channel.** Non-intrusive channels cost nothing
    (`docs/problem.md` 5.3), so a table of only those gives every arm an unbounded budget
    and the ladder compares nothing.

    **No dominated channel.** If a channel is at least as cheap *and* at least as
    effective as another, nothing would ever choose the other one -- and the
    multiple-choice knapsack (T3.3) is then quietly smaller than the config claims.
    That is not an error in the data, it is an error in the experiment: the whole reason
    channels have distinct costs is to make the allocation a genuine choice rather than
    a sort (`problem.md` 5.2).
    """
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(
            f"channel names must be unique; {duplicates} appear more than once. A "
            "Decision names its channel as a string, so a duplicate makes one of them "
            "unreachable and the other silently absorb its decisions."
        )
    if not any(c.intrusive for c in channels):
        raise ValueError(
            "at least one channel must be intrusive, or no arm can spend its budget and "
            "the evaluation ladder compares nothing. See docs/problem.md 5.3."
        )
    for cheap in channels:
        for dear in channels:
            if cheap.name == dear.name:
                continue
            cheaper = cheap.cost_inr <= dear.cost_inr
            better = cheap.efficacy_prior >= dear.efficacy_prior
            if (
                cheaper
                and better
                and (cheap.cost_inr, -cheap.efficacy_prior)
                != (
                    dear.cost_inr,
                    -dear.efficacy_prior,
                )
            ):
                raise ValueError(
                    f"channel {dear.name!r} (cost {dear.cost_inr}, efficacy "
                    f"{dear.efficacy_prior}) is dominated by {cheap.name!r} (cost "
                    f"{cheap.cost_inr}, efficacy {cheap.efficacy_prior}): no optimiser "
                    "would ever choose it, so the multiple-choice knapsack is smaller "
                    "than this config claims. Channels exist to make the allocation a "
                    "choice rather than a sort -- see docs/problem.md 5.2."
                )
    return channels


class LLMParams(BaseModel):
    """The Phase 4 model job's configuration, including what a call is allowed to cost.

    Prices are USD per million tokens and stay USD. The temptation is to print a rupee
    figure next to every other rupee figure in this project, but no verified USD/INR rate
    exists here, and `india.ntd_to_inr: 1.0` is a decision about a subscription price
    ladder rather than an exchange rate -- reusing it would smuggle a fabricated FX rate
    into a cost headline (CLAUDE.md 3, "no number exists without an origin").
    """

    model: str
    max_tokens: int = Field(gt=0)
    effort: str
    price_input_usd_per_mtok: float = Field(ge=0)
    price_output_usd_per_mtok: float = Field(ge=0)
    price_cache_read_usd_per_mtok: float = Field(ge=0)
    price_cache_write_usd_per_mtok: float = Field(ge=0)
    spend_cap_usd_per_run: float = Field(gt=0)

    @model_validator(mode="after")
    def _cache_prices_are_the_cheap_and_dear_side(self) -> LLMParams:
        """A cache read must be cheaper than a fresh read, and a write dearer.

        Inverted, prompt caching would look like a cost *increase* in every report the
        eval suite produces, and the obvious response -- turning caching off -- would make
        the real bill go up. The check is here because the numbers are multiples of the
        input price (0.1x and 1.25x) written out by hand, and a hand-written multiple is
        exactly the kind of thing that gets transposed.
        """
        if self.price_cache_read_usd_per_mtok >= self.price_input_usd_per_mtok:
            raise ValueError(
                f"llm.price_cache_read_usd_per_mtok ({self.price_cache_read_usd_per_mtok}) "
                f"is not below the input price ({self.price_input_usd_per_mtok}); a cache "
                "read that costs more than a fresh read makes caching look like a loss."
            )
        if self.price_cache_write_usd_per_mtok < self.price_input_usd_per_mtok:
            raise ValueError(
                f"llm.price_cache_write_usd_per_mtok ({self.price_cache_write_usd_per_mtok}) "
                f"is below the input price ({self.price_input_usd_per_mtok}); writing to "
                "the cache carries a premium, not a discount."
            )
        return self


class SafetyParams(BaseModel):
    """Operational limits (T5.3). None of these are swept.

    A spend cap a sweep could raise is not a cap, and a mode a sweep could flip is not a
    default. `eval/sweep.py` varies modelling constants; these are not that.
    """

    mode: str
    kill_switch_file: str
    max_sends_per_window: int = Field(gt=0)
    window_seconds: int = Field(gt=0)
    max_spend_inr_per_run: float = Field(ge=0)
    max_model_age_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _mode_is_one_of_two(self) -> SafetyParams:
        """A typo must not silently mean "live".

        `mode: shaddow` under a truthy check reads as not-shadow, and the system starts
        contacting customers because someone misspelled the word that was supposed to stop
        it. So the field is checked against a closed set rather than compared to a string.
        """
        if self.mode not in {"shadow", "live"}:
            raise ValueError(
                f"safety.mode is {self.mode!r}; it must be 'shadow' or 'live'. A value that "
                "is neither would be read as not-shadow by any truthiness check, which "
                "turns a typo into a live system."
            )
        return self


class Params(BaseModel):
    channels: list[Channel]
    value: ValueParams
    recovery: RecoveryParams
    intervention: InterventionParams
    horizon: HorizonParams
    india: IndiaParams
    llm: LLMParams
    safety: SafetyParams
    seed: int

    @model_validator(mode="after")
    def check_channels(self) -> Params:
        _check_channels(self.channels)
        return self


class PolicySource(BaseModel):
    """Where the rules came from, precisely enough to go and check.

    `text_file` and `sha256` together are the mechanism, not the paperwork. The circular
    text is committed next to the policy, its hash is pinned here, and `load_policy`
    refuses to load if the two disagree. So the source text cannot be edited to make a rule's
    quote match: doing that breaks the hash, and the fix is to re-read the circular and
    re-review the diff, which is exactly the human-in-the-loop step T4.1 is built around.
    """

    name: str
    circular_no: str
    dated: date
    url: str
    retrieved_on: date
    text_file: str = Field(description="path to the committed circular text, relative to policy/")
    sha256: str = Field(min_length=64, max_length=64)
    read: bool = Field(description="False until the circular text itself has been read")


class Policy(BaseModel):
    """The compiled rulebook.

    Structural checks live here; the two that need the circular text on disk (quote
    verbatimness and the source hash) live in `load_policy`, because a Pydantic model does
    not know which file it was loaded from and should not have to guess.
    """

    version: int
    source: PolicySource
    rules: list[PolicyRule]

    @model_validator(mode="after")
    def _rules_are_unique_and_evaluable(self) -> Policy:
        ids = [rule.rule_id for rule in self.rules]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"duplicate rule_id(s) {duplicates}. Rule ids are how a ledger entry names "
                "the rule that produced a finding (T5.1); two rules sharing one make an "
                "audit trail that cannot be followed back."
            )

        known = set(MandateAuditContext.model_fields)
        for rule in self.rules:
            checks = (("expression", rule.expression), ("applies_when", rule.applies_when))
            for field, text in checks:
                try:
                    used = referenced_names(text)
                except ExpressionError as exc:
                    raise ValueError(
                        f"rule {rule.rule_id!r} has an illegal {field}: {exc}"
                    ) from exc
                unknown = sorted(used - known)
                if unknown:
                    raise ValueError(
                        f"rule {rule.rule_id!r} reads {unknown} in its {field}, which "
                        "MandateAuditContext does not define. A rule naming a field that "
                        "does not exist never fires, and a rule that never fires passes "
                        "every test in the suite."
                    )
        return self


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_params(path: Path = PARAMS_PATH) -> Params:
    return Params.model_validate(_read_yaml(path))


_CLAUSE_HEADING = re.compile(r"^## (\d+)\.", re.MULTILINE)
_CLAUSE_NUMBER = re.compile(r"^(\d+)")


def _normalise(text: str) -> str:
    """Collapse whitespace so a YAML-wrapped quote can be compared to a long source line.

    Whitespace only. Punctuation, spelling and casing are left alone on purpose -- clause
    6(c) of this circular contains "shall provider a customer", and a normaliser that
    tidied that would let a rule quote a sentence the regulator never wrote.
    """
    return " ".join(text.split())


def content_hash(path: Path) -> str:
    """SHA-256 of a text file, with line endings normalised out of the answer first.

    A hash over raw bytes answers "is this the same file", and what these hashes are asked
    is "is this the same *text*". The two came apart on 2026-09-04: CI on `windows-latest`
    checked the repository out with `core.autocrlf=true`, every LF in the circular became
    CRLF, and `load_policy()` refused to start -- reporting that the regulator's text had
    been edited, when what had changed was git's checkout setting. The same failure would
    meet any contributor cloning on Windows with git's defaults.

    Normalising also protects the value this is used for: `policy_hash()` goes into every
    ledger entry, so without it "this decision replays under the policy that produced it"
    would quietly become "...if you cloned the way I did".

    The committed files are LF, so this changes no existing hash -- verified before the
    change, because a normalisation that silently moved `policy_hash` would invalidate
    every ledger already written.
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def source_text(policy: Policy, policy_dir: Path = POLICY_DIR) -> str:
    """The committed circular text a policy's rules cite, hash-checked on the way out."""
    path = policy_dir / policy.source.text_file
    if not path.is_file():
        raise FileNotFoundError(
            f"policy source text {path} is missing. Every rule cites this file; without it "
            "no citation in mandate_policy.yaml can be checked against anything."
        )
    raw = path.read_bytes()
    digest = content_hash(path)
    if digest != policy.source.sha256:
        raise ValueError(
            f"{path.name} hashes to {digest}, but mandate_policy.yaml pins "
            f"{policy.source.sha256}. Either the circular text was edited after the rules "
            "were compiled, or the rules were compiled against a different text. Re-read "
            "the circular, re-run scripts/compile_policy.py, and review the diff -- do not "
            "update the hash on its own."
        )
    return raw.decode("utf-8")


def check_rule_citations(rules: list[PolicyRule], text: str, source_name: str) -> None:
    """Every rule cites a clause that exists and quotes words that are actually in it.

    This is the check that makes the LLM compiler in T4.1 trustworthy rather than merely
    plausible. A model asked for rules with citations will produce rules with citations;
    what it will not reliably produce is rules whose quoted words survive a literal
    substring test against the regulation.

    Public because the compiler (`agent/compiler.py`) runs it on a *proposal*, before the
    proposal is ever written to disk. A rule that fails here never reaches the reviewer's
    diff, which keeps the review about judgement rather than about spotting fabrications.
    """
    clauses = {int(n) for n in _CLAUSE_HEADING.findall(text)}
    if not clauses:
        raise ValueError(
            f"no numbered clauses found in {source_name}: the citation check would pass "
            "vacuously, which is worse than not running it."
        )
    haystack = _normalise(text)

    for rule in rules:
        match = _CLAUSE_NUMBER.match(rule.clause.strip())
        if match is None or int(match.group(1)) not in clauses:
            raise ValueError(
                f"rule {rule.rule_id!r} cites clause {rule.clause!r}, which is not one of "
                f"the {len(clauses)} numbered clauses in {source_name} "
                f"({min(clauses)}-{max(clauses)})."
            )
        if _normalise(rule.quote) not in haystack:
            raise ValueError(
                f"rule {rule.rule_id!r} quotes text that does not appear in "
                f"{source_name}:\n  {rule.quote!r}\n"
                "A quote that is not in the source is a rule the regulation does not "
                "support, however reasonable it sounds."
            )


def load_policy(path: Path = POLICY_PATH) -> Policy:
    """Load, validate, and check every rule against the circular text it cites.

    Costs one file read and one SHA-256 of a ~12 KB document per call, which is why the
    auditor takes a `Policy` rather than re-loading per mandate.
    """
    policy = Policy.model_validate(_read_yaml(path))
    if policy.rules and not policy.source.read:
        raise ValueError(
            f"{path.name} carries {len(policy.rules)} rules but `source.read` is false. "
            "Rules compiled from a circular nobody read are rules with no origin -- see "
            "CLAUDE.md 3."
        )
    check_rule_citations(policy.rules, source_text(policy, path.parent), policy.source.text_file)
    return policy


def policy_hash(path: Path = POLICY_PATH) -> str:
    """Content hash of the policy file, recorded on every ledger entry.

    Lets us say, in one line: change the policy and old decisions still replay under the
    policy that produced them. Line endings are normalised out (`content_hash`) so that
    sentence does not depend on how the repository was cloned.
    """
    return content_hash(path)[:16]
