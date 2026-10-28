import { describe, expect, it } from 'vitest'
import { withVariantName } from '@/lib/variant'

describe('withVariantName', () => {
  it('appends the variant name after an en dash', () => {
    expect(withVariantName('Casio F-91W', 'Black')).toBe('Casio F-91W – Black')
  })

  it('returns the base name when there is no variant', () => {
    expect(withVariantName('Casio F-91W', null)).toBe('Casio F-91W')
    expect(withVariantName('Casio F-91W', undefined)).toBe('Casio F-91W')
    expect(withVariantName('Casio F-91W', '  ')).toBe('Casio F-91W')
  })
})
