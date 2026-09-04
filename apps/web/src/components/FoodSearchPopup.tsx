import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { FoodSearchResult } from '../lib/types'

interface FoodSearchPopupProps {
  onClose: () => void
  onAdd: (food: FoodSearchResult, grams: number) => void
}

export default function FoodSearchPopup({ onClose, onAdd }: FoodSearchPopupProps) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<FoodSearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // grams keyed by food id
  const [gramsMap, setGramsMap] = useState<Record<string, string>>({})

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function doSearch() {
    const q = query.trim()
    if (!q) return
    setSearching(true)
    setError(null)
    setSearched(true)
    try {
      const res = await authClient.api(`/meals/foods/search?q=${encodeURIComponent(q)}`)
      setResults(Array.isArray(res) ? res : [])
    } catch (e) {
      setError(String(e))
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  function getGrams(f: FoodSearchResult): number {
    const g = gramsMap[f.id]
    if (g && !isNaN(Number(g)) && Number(g) > 0) return Number(g)
    return typeof f.serving_grams === 'number' ? f.serving_grams : 100
  }

  function kcalOf(f: FoodSearchResult): number | null {
    if (typeof f.calories === 'number') return f.calories
    if (typeof f.kcal_per_100g === 'number' && typeof f.serving_grams === 'number') {
      return f.kcal_per_100g * (f.serving_grams / 100)
    }
    return null
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-popup food-search-popup"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Search foods"
      >
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon">🔍</span>
            <h2>Search foods</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="modal-content">
          <div className="field">
            <div className="search-row">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && doSearch()}
                placeholder="e.g. chicken breast, banana, oat milk"
                autoFocus
              />
              <button onClick={doSearch} disabled={searching || !query.trim()}>
                {searching ? <span className="spinner" /> : null}Search
              </button>
            </div>
          </div>

          {error && <div className="error">{error}</div>}

          {searched && results.length === 0 && !searching && (
            <div className="empty">No foods found for &ldquo;{query}&rdquo;. Try a simpler name.</div>
          )}

          {results.length > 0 && (
            <ul className="food-results">
              {results.map((f) => {
                const kcal = kcalOf(f)
                const defaultGrams = typeof f.serving_grams === 'number' ? f.serving_grams : 100
                return (
                  <li key={f.id} className="list-item food-result-item">
                    <div className="main">
                      <div className="title">{f.name || f.display_name || f.id}</div>
                      <div className="desc">
                        {kcal != null ? `${Math.round(kcal)} kcal` : 'calories n/a'} ·{' '}
                        {defaultGrams}g per serving
                        {f.source ? ` · ${f.source}` : ''}
                      </div>
                      <div className="food-grams-row">
                        <label className="grams-label">Grams:</label>
                        <input
                          className="grams-input"
                          type="number"
                          min="0"
                          value={gramsMap[f.id] ?? ''}
                          placeholder={String(defaultGrams)}
                          onChange={(e) =>
                            setGramsMap((prev) => ({ ...prev, [f.id]: e.target.value }))
                          }
                        />
                      </div>
                    </div>
                    <button
                      className="small"
                      onClick={() => onAdd(f, getGrams(f))}
                    >
                      Add
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}