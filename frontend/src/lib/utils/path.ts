// Builds a save-path joining a directory and filename. Picks `\` on Windows
// paths (which contain `\`) and `/` otherwise. Trailing separators on the
// directory are stripped so we don't emit `/foo//bar.pdf`.
export function buildDefaultSavePath(dir: string | undefined, filename: string): string {
  const trimmed = dir?.trim()
  if (!trimmed) return filename
  const sep = trimmed.includes('\\') ? '\\' : '/'
  const stripped = trimmed.replace(/[\\/]+$/, '')
  return `${stripped}${sep}${filename}`
}
