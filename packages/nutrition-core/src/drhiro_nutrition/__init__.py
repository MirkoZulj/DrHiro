"""drHiro nutrition core: food catalog abstraction, Open Food Facts and
USDA adapters, and nutrient normalization.

Design notes from the blueprint:
- Open Food Facts API v3 for barcode lookup (EU/international packaged).
- USDA FoodData Central for generic foods and nutrients.
- A private drHiro catalog for user-created foods and corrected matches.
- Every nutrient calculation stores source and source version.
"""
