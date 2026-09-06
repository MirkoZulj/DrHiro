import { titleCase } from '../lib/format'
import { Meal } from '../lib/types'

interface MealCardProps {
  meal: Meal
  expanded: boolean
  onToggle: () => void
}

export default function MealCard({ meal, expanded, onToggle }: MealCardProps) {
  const typeLabel = titleCase(meal.meal_type || 'Meal')
  const totals = meal.totals_json
  const kcal = totals?.kcal != null ? Math.round(totals.kcal) : null

  const items = meal.items ?? []

  return (
    <div className={`meal-card${expanded ? ' expanded' : ''}`}>
      <button className="meal-card-header" onClick={onToggle} aria-expanded={expanded}>
        <div className="meal-card-left">
          <span className="meal-card-icon">{mealIcon(meal.meal_type)}</span>
          <span className="meal-card-title">{typeLabel}</span>
          <span className="meal-card-count">{items.length} item{items.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="meal-card-right">
          {kcal != null && <span className="meal-card-kcal">{kcal} kcal</span>}
          <span className={`meal-card-chevron${expanded ? ' open' : ''}`}>▾</span>
        </div>
      </button>

      {expanded && (
        <div className="meal-card-body">
          {items.length === 0 ? (
            <div className="empty">No items.</div>
          ) : (
            <ul className="meal-item-list">
              {items.map((item, idx) => {
                const n = item.nutrients_json ?? {}
                const itemKcal =
                  typeof n.kcal === 'number'
                    ? Math.round(n.kcal)
                    : typeof n.calories === 'number'
                      ? Math.round(n.calories)
                      : null
                const protein = typeof n.protein_g === 'number' ? Math.round(n.protein_g) : null
                const carbs = typeof n.carbs_g === 'number' ? Math.round(n.carbs_g) : null
                const fat = typeof n.fat_g === 'number' ? Math.round(n.fat_g) : null
                return (
                  <li key={item.id ?? idx} className="meal-item">
                    <div className="meal-item-main">
                      <span className="meal-item-name">
                        {item.display_name || item.food_name || 'Unknown'}
                      </span>
                      <span className="meal-item-grams">{item.grams}g</span>
                    </div>
                    <div className="meal-item-nutrients">
                      {itemKcal != null && <span className="nutrient kcal">{itemKcal} kcal</span>}
                      {protein != null && <span className="nutrient protein">P: {protein}g</span>}
                      {carbs != null && <span className="nutrient carbs">C: {carbs}g</span>}
                      {fat != null && <span className="nutrient fat">F: {fat}g</span>}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
          {totals && (
            <div className="meal-card-totals">
              <span className="totals-label">Totals:</span>
              {totals.kcal != null && <span className="nutrient kcal">{Math.round(totals.kcal)} kcal</span>}
              {totals.protein_g != null && <span className="nutrient protein">P: {Math.round(totals.protein_g)}g</span>}
              {totals.carbs_g != null && <span className="nutrient carbs">C: {Math.round(totals.carbs_g)}g</span>}
              {totals.fat_g != null && <span className="nutrient fat">F: {Math.round(totals.fat_g)}g</span>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function mealIcon(type: string | null): string {
  switch (type) {
    case 'breakfast':
      return '🍳'
    case 'lunch':
      return '🥗'
    case 'dinner':
      return '🍽️'
    case 'snack':
      return '🍎'
    default:
      return '🍴'
  }
}