from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user_optional
from drhiro_api.food_search import nutrient_map, resolve_food
from drhiro_api.models import Nutrient, User

router = APIRouter(prefix="/foods", tags=["foods"])


@router.get("/search")
def search_foods(
    q: str = Query(min_length=1),
    limit: int = 10,
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    """Search local foods, best match first.

    Ranking lives in drhiro_api.food_search so search and meal logging can
    never disagree about which food a name refers to.
    """
    result = resolve_food(db, q, limit=limit, user_id=user.id if user else None)

    # Load the nutrient vocabulary once rather than per nutrient per food.
    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}

    out = []
    for match in result.matches:
        f = match.food
        nmap = nutrient_map(f, code_by_id)
        out.append({
            "external_id": str(f.external_id),
            "display_name": f.display_name,
            "source": "usda",
            "kcal_per_100g": nmap.get("energy"),
            "protein_g_per_100g": nmap.get("protein"),
            "carbs_g_per_100g": nmap.get("carbs"),
            "fat_g_per_100g": nmap.get("fat"),
            "fiber_g_per_100g": nmap.get("fiber"),
            "sodium_mg_per_100g": nmap.get("sodium"),
            "barcode": f.barcode,
            # Added fields (existing consumers ignore unknown keys).
            "match_tier": match.tier,
            "is_generic": bool(f.is_generic),
        })
    return out
