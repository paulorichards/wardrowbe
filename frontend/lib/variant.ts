/** "Name – Variant" when the item is a variant of another one, otherwise just the name. */
export function withVariantName(
  name: string,
  variantName: string | null | undefined,
): string {
  const variant = variantName?.trim();
  return variant ? `${name} – ${variant}` : name;
}
