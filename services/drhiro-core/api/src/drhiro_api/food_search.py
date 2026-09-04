"""Ranked food-name resolution.

Single source of truth for turning a free-text food name into a ranked list of
`Food` rows. Used by BOTH the search endpoint and the meal nutrient lookup so
ranking can never drift between what a user sees in search and what actually
gets logged.

The previous implementation used `.ilike('%q%').first()`, which returns an
arbitrary row in physical table order. Searching "egg" could return
"Eggs, Grade A, Large, egg white" (55 kcal) when the user meant a whole egg
(143 kcal) -- silently, with no error, because the wrong food resolved
"successfully". A wrong number is indistinguishable from a right one
downstream, which makes this the worst kind of failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import and_, case, func, literal, or_
from sqlalchemy.orm import Session, selectinload

from drhiro_api.models import Food, FoodResolutionRule

# Ranking tiers, lower sorts first.
TIER_EXACT = 0      # display_name == query (case-insensitive)
TIER_PREFIX = 1     # display_name starts with query AT A WORD BOUNDARY
TIER_WORD = 2       # query appears as a whole word anywhere
TIER_SUBSTRING = 3  # query appears anywhere, including mid-word ("egg" in "Eggplant")

# Two candidates in the same tier whose name lengths differ by no more than
# this many characters are treated as genuinely ambiguous: the tie-breakers
# cannot meaningfully separate them, so we refuse to silently pick a winner.
AMBIGUITY_LENGTH_DELTA = 4

# Preparation qualifiers that make a row a less likely answer to a bare
# ingredient name. Someone typing "egg" means a fresh egg, not dried egg
# powder or frozen pasteurised egg product. These are demoted within their
# tier, below unqualified rows, but never excluded -- an explicit search for
# "Egg, white, dried" must still find it (and will, via the exact-match tier).
PREPARATION_QUALIFIERS = ("dried", "frozen", "pasteurized", "canned", "powder", "dehydrated")

# USDA names are comma-inverted: "Vinegar, red wine" is red-wine VINEGAR, not
# wine. When the query matches only a modifier and the head category is a
# different food, demote. Map: query -> head-category terms that make the row
# a different kind of food than what the query means.
_HEAD_CATEGORY_EXCLUSIONS = {
    "wine": {"vinegar", "sauce"},
    "steak": {"potato", "fries", "sauce", "sandwich", "submarine"},
}


@dataclass
class FoodMatch:
    """One ranked candidate."""

    food: Food
    tier: int


@dataclass
class ResolveResult:
    """Ranked candidates plus whether the top hit can be trusted outright."""

    matches: list[FoodMatch] = field(default_factory=list)
    ambiguous: bool = False
    note: str | None = None  # set when a learned user rule decided the result

    @property
    def best(self) -> Food | None:
        return self.matches[0].food if self.matches else None

    @property
    def candidate_names(self) -> list[str]:
        return [m.food.display_name for m in self.matches]

    def __bool__(self) -> bool:
        return bool(self.matches)


def _tier_expression(query: str):
    """SQL CASE that assigns each row its relevance tier.

    Ranking happens in the database, not in Python: we must not pull the whole
    table into memory to sort it.

    Word boundaries matter. USDA names are comma-separated phrases
    ("Egg, whole, raw, fresh"), so we normalise separators to spaces and pad
    the whole string, letting a single LIKE test find whole words anywhere --
    including the first and last. Without this, a prefix test on "egg" ranks
    "Eggplant, raw" above "Egg, whole, raw, fresh", which is exactly the class
    of silent substitution this module exists to prevent.
    """
    lowered = func.lower(Food.display_name)
    q = query.lower()

    # ", " and "," -> " " so commas act as word separators, then pad both ends.
    normalised = func.concat(
        " ", func.replace(func.replace(lowered, ",", " "), "  ", " "), " "
    )

    return case(
        (lowered == q, TIER_EXACT),
        # Prefix, but only when the query ends at a word boundary.
        (normalised.like(f" {q} %"), TIER_PREFIX),
        # Whole word anywhere in the name.
        (normalised.like(f"% {q} %"), TIER_WORD),
        else_=TIER_SUBSTRING,
    )


def _preparation_penalty(query: str):
    """1 when the row carries a preparation qualifier the query did not ask for.

    Applied as an ORDER BY term after tier, so it never promotes a worse tier --
    it only settles which row wins *within* a tier. If the user actually typed
    the qualifier, no penalty applies.
    """
    lowered = func.lower(Food.display_name)
    q = query.lower()

    conditions = [
        lowered.like(f"%{word}%")
        for word in PREPARATION_QUALIFIERS
        if word not in q
    ]
    if not conditions:
        return literal(0)

    return case((or_(*conditions), 1), else_=0)


def _head_category_penalty(query: str):
    """1 when the query matches only a modifier of an unrelated head category.

    "Vinegar, red wine" is vinegar, not wine: the query appears after the
    comma while the head ("Vinegar") names a different food. Applied as an
    ORDER BY term after tier, so it never promotes a worse tier -- it only
    settles which row wins *within* a tier.
    """
    terms = _HEAD_CATEGORY_EXCLUSIONS.get(query.lower())
    if not terms:
        return literal(0)

    head = func.lower(func.split_part(Food.display_name, ",", 1))
    q = query.lower()

    # Query must NOT appear (even partially) in the head -- "wine sauce" head
    # still contains the query concept, so it stays unpenalised.
    query_in_head = head.like(f"%{q}%")
    head_is_other_food = or_(*[head.like(f"%{t}%") for t in terms])

    return case((and_(head_is_other_food, ~query_in_head), 1), else_=0)


def resolve_food(db: Session, query: str, limit: int = 10, user_id=None) -> ResolveResult:
    """Resolve a free-text food name to ranked candidates.

    Ordering:
      1. tier (exact, then word-boundary prefix, then whole-word, then substring)
      2. no unrequested preparation qualifier -- fresh before dried/frozen
      3. generic foods before branded -- someone typing "egg" wants the
         ingredient, not a packaged product
      4. shorter display_name first -- fewer qualifiers means more canonical
      5. name alphabetically, purely so results are deterministic
    """
    query = (query or "").strip()
    if not query:
        return ResolveResult()

    # Learned rules first: a user-corrected resolution ("wine" means the
    # beverage, not vinegar) overrides ranking for this user.
    ruled = match_resolution_rule(db, query, user_id)
    if ruled is not None:
        return ResolveResult(matches=[FoodMatch(food=ruled, tier=TIER_EXACT)],
                             note="user rule")

    tier = _tier_expression(query)
    penalty = _preparation_penalty(query)
    head_penalty = _head_category_penalty(query)

    rows = (
        db.query(Food, tier.label("tier"))
        .options(selectinload(Food.nutrients))
        .filter(Food.display_name.ilike(f"%{query}%"))
        .order_by(
            tier.asc(),
            head_penalty.asc(),
            penalty.asc(),
            # NULL is_generic sorts with branded rather than ahead of generic.
            case((Food.is_generic.is_(True), 0), else_=1).asc(),
            func.length(Food.display_name).asc(),
            Food.display_name.asc(),
        )
        .limit(limit)
        .all()
    )

    matches = [FoodMatch(food=food, tier=int(tier_value)) for food, tier_value in rows]

    return ResolveResult(matches=matches, ambiguous=_is_ambiguous(matches, query))


def match_resolution_rule(db: Session, query: str, user_id=None):
    """Return the Food a learned rule pins this query to, or None.

    Matching is deliberately simple: case-insensitive substring of the rule's
    original_pattern in the query. Only active rules for THIS user (plus any
    global-scope rules) apply; most recently updated wins.
    """
    q = query.lower()
    if not q:
        return None

    conds = [FoodResolutionRule.active.is_(True),
             func.lower(FoodResolutionRule.original_pattern).isnot(None)]
    if user_id is not None:
        conds.append(
            or_(FoodResolutionRule.user_id == user_id,
                FoodResolutionRule.scope == "global")
        )

    rules = (
        db.query(FoodResolutionRule)
        .filter(and_(*conds))
        .order_by(FoodResolutionRule.updated_at.desc())
        .all()
    )
    for r in rules:
        pat = (r.original_pattern or "").lower().strip()
        if pat and pat in q and r.resolved_food_id:
            food = db.get(Food, r.resolved_food_id)
            if food is not None:
                return food
    return None


def _has_unrequested_qualifier(name: str, query: str) -> bool:
    low, q = name.lower(), query.lower()
    return any(w in low and w not in q for w in PREPARATION_QUALIFIERS)


def _has_head_exclusion(name: str, query: str) -> bool:
    """Python mirror of _head_category_penalty for ambiguity detection."""
    terms = _HEAD_CATEGORY_EXCLUSIONS.get(query.lower())
    if not terms:
        return False
    head = name.split(",")[0].strip().lower()
    q = query.lower()
    if q in head:
        return False
    return any(t in head for t in terms)


def _is_ambiguous(matches: list[FoodMatch], query: str) -> bool:
    """True when the top two candidates cannot be meaningfully separated.

    Ambiguous means: same tier, same preparation-penalty class, and name
    lengths close enough that shorter-is-more-canonical is not discriminating.
    An exact match is never ambiguous -- there is nothing better to find.
    """
    if len(matches) < 2:
        return False

    first, second = matches[0], matches[1]

    if first.tier == TIER_EXACT:
        return False
    if first.tier != second.tier:
        return False

    # A penalised runner-up is not a genuine rival to an unpenalised leader.
    if _has_unrequested_qualifier(first.food.display_name, query) != \
       _has_unrequested_qualifier(second.food.display_name, query):
        return False
    if _has_head_exclusion(first.food.display_name, query) != \
       _has_head_exclusion(second.food.display_name, query):
        return False
    if bool(first.food.is_generic) != bool(second.food.is_generic):
        return False

    delta = abs(len(first.food.display_name) - len(second.food.display_name))
    return delta <= AMBIGUITY_LENGTH_DELTA


def nutrient_map(food: Food, code_by_id: dict) -> dict:
    """Flatten a Food's nutrient rows into {nutrient_code: amount_per_100g}.

    `code_by_id` is passed in so callers can load the nutrient vocabulary once
    instead of querying per nutrient per food (the previous N+1).
    """
    out = {}
    for fn in food.nutrients:
        code = code_by_id.get(fn.nutrient_id)
        if code is not None:
            out[code] = fn.amount_per_100g
    return out
