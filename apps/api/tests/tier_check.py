from drhiro_api.db import SessionLocal
from drhiro_api.food_search import resolve_food, _tier
db = SessionLocal()
U = "0bfad360-9938-4216-8abd-b44d69e2003f"
for q in ["lager, abv, %", "lager", "oyster mushrooms", "white rice",
          "Peppers, bell, green, raw", "Pepper, banana, raw", "pepper", "beer"]:
    r = resolve_food(db, q, limit=3, user_id=U)
    best = r.best.display_name if r.best else None
    tier = r.matches[0].tier if r.matches else -1
    print(f"{q!r:28} -> {best!r:45} tier={tier} amb={r.ambiguous} note={r.note}")
print("tier checks:",
      _tier("Peppers, bell, green, raw", "Peppers, bell, green, raw"),
      _tier("Pepper, banana, raw", "Pepper, banana, raw"),
      _tier("pepper", "Peppers, bell, green, raw"),
      _tier("pepper", "Pepper, banana, raw"))
db.close()