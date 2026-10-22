// The Settings tabs, in strip order. Its own module so App, the Menu
// and Settings share one list: an array exported from Settings.tsx
// would trip react-refresh/only-export-components.

export const SETTINGS_TABS = ['feeds', 'llm', 'notifications', 'history', 'hidden', 'starred', 'voted', 'reset'] as const

export type SettingsTab = (typeof SETTINGS_TABS)[number]

export function isSettingsTab(t: string | null | undefined): t is SettingsTab {
  return !!t && (SETTINGS_TABS as readonly string[]).includes(t)
}
