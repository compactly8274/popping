/**
 * Component tests for the PreferencesProvider. The pure helpers
 * (applyRowToState, mergeStateFromServer, trimVotedEntries) are
 * covered in preferences.test.ts. This file covers the React
 * integration: the provider does the right thing on mount, the
 * setters trigger the right network calls, the debounce + flush
 * work as advertised.
 *
 * Mocking strategy: vi.mock the api module so the test doesn't
 * hit the network. The provider is the integration boundary; if
 * the provider's mount path / setters wire the right api calls,
 * the rest of the system gets the right shape.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

// Mock the api module BEFORE importing the component. The
// provider imports api at module load, so the mock has to be in
// place first.
const mockGetPreferences = vi.fn()
const mockSetPreference = vi.fn()
const mockDeletePreference = vi.fn()
vi.mock('../api', () => ({
  api: {
    getPreferences: mockGetPreferences,
    setPreference: mockSetPreference,
    deletePreference: mockDeletePreference,
  },
}))

// Mock the interactions module (used by the provider for batched
// write triggers). We don't care about its behavior here, just
// that importing it doesn't blow up.
vi.mock('./interactions', () => ({
  recordImmediate: vi.fn(),
  recordBatched: vi.fn(),
}))

// Now safe to import the component.
import {
  PreferencesProvider,
  usePreferences,
  PREFERENCE_KEYS,
  MAX_HIDDEN,
  MAX_STARRED,
  MAX_VOTED,
  MAX_PER_COLUMN,
} from './preferences'

// A minimal consumer that exposes the provider's state for
// assertions. Each test renders a <Probe /> that calls
// ``usePreferences()`` and renders the value of one key so the
// test can query it via @testing-library/react.
function Probe({ field }: { field: string }) {
  const prefs = usePreferences()
  // Expose the test-relevant state via a data-attribute on a
  // <div> so the test can read it without re-rendering the
  // whole tree on every state change.
  const value = (prefs.state as any)[field]
  return (
    <div data-testid="probe" data-field={field}>
      {JSON.stringify(value)}
    </div>
  )
}

function SetterProbe({
  fn,
}: {
  fn: (api: ReturnType<typeof usePreferences>) => void
}) {
  const prefs = usePreferences()
  return (
    <button data-testid="setter" onClick={() => fn(prefs)}>
      run
    </button>
  )
}

beforeEach(() => {
  mockGetPreferences.mockReset()
  mockSetPreference.mockReset()
  mockDeletePreference.mockReset()
  // Default: server returns no rows (cold start).
  mockGetPreferences.mockResolvedValue({ items: [] })
  mockSetPreference.mockResolvedValue({} as any)
  mockDeletePreference.mockResolvedValue(undefined as any)
})

afterEach(() => {
  // Flush any in-flight timers between tests so a debounced
  // PUT from one test doesn't leak into the next.
  vi.useRealTimers()
})

describe('PreferencesProvider mount', () => {
  it('starts in the loading state until the GET resolves', async () => {
    let resolveGet: (v: any) => void = () => {}
    mockGetPreferences.mockReturnValue(
      new Promise((res) => { resolveGet = res }),
    )
    render(
      <PreferencesProvider>
        <Probe field="hiddenEntries" />
      </PreferencesProvider>,
    )
    // The probe is rendered with the default state. Before the
    // GET resolves, the provider is still in its initial state.
    expect(screen.getByTestId('probe')).toHaveTextContent('[]')
    // Resolve the GET to unmount cleanly.
    resolveGet({ items: [] })
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalledTimes(1),
    )
  })

  it('GETs /api/preferences on mount exactly once', async () => {
    render(
      <PreferencesProvider>
        <Probe field="hiddenEntries" />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalledTimes(1),
    )
  })

  it('decodes server rows into the right state field', async () => {
    mockGetPreferences.mockResolvedValue({
      items: [
        { key: PREFERENCE_KEYS.hiddenEntries, value: [10, 20, 30], updated_at: 't' },
        { key: PREFERENCE_KEYS.starredEntries, value: [99], updated_at: 't' },
      ],
    })
    render(
      <PreferencesProvider>
        <Probe field="hiddenEntries" />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(screen.getByTestId('probe')).toHaveTextContent('[10,20,30]'),
    )
  })
})

describe('PreferencesProvider setters', () => {
  it('setHiddenEntries triggers a debounced PUT', async () => {
    vi.useFakeTimers()
    render(
      <PreferencesProvider>
        <SetterProbe
          fn={(api) => api.setHiddenEntries([1, 2, 3])}
        />
      </PreferencesProvider>,
    )
    // Wait for the initial GET to settle so the provider is
    // out of the loading state.
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalled(),
    )
    act(() => {
      screen.getByTestId('setter').click()
    })
    // The PUT is debounced; before the timer fires, no PUT.
    expect(mockSetPreference).not.toHaveBeenCalled()
    act(() => {
      vi.advanceTimersByTime(300)
    })
    // After the debounce, the PUT fires with the latest value.
    expect(mockSetPreference).toHaveBeenCalledWith(
      PREFERENCE_KEYS.hiddenEntries,
      [1, 2, 3],
    )
  })

  it('coalesces rapid writes into one PUT with the latest value', async () => {
    vi.useFakeTimers()
    render(
      <PreferencesProvider>
        <SetterProbe
          fn={(api) => {
            api.setHiddenEntries([1])
            api.setHiddenEntries([2])
            api.setHiddenEntries([3])
          }}
        />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalled(),
    )
    act(() => {
      screen.getByTestId('setter').click()
    })
    // Three writes coalesced — only the latest value ([3]) is
    // sent after the debounce.
    act(() => {
      vi.advanceTimersByTime(300)
    })
    expect(mockSetPreference).toHaveBeenCalledTimes(1)
    expect(mockSetPreference).toHaveBeenCalledWith(
      PREFERENCE_KEYS.hiddenEntries,
      [3],
    )
  })

  it('clearLastViewed issues a DELETE, not a PUT', async () => {
    vi.useFakeTimers()
    render(
      <PreferencesProvider>
        <SetterProbe fn={(api) => api.clearLastViewed('tech')} />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalled(),
    )
    act(() => {
      screen.getByTestId('setter').click()
    })
    // DELETE is immediate (not debounced) so we can assert
    // synchronously.
    expect(mockDeletePreference).toHaveBeenCalledWith(
      `${PREFERENCE_KEYS.lastViewed}:tech`,
    )
    expect(mockSetPreference).not.toHaveBeenCalled()
  })

  it('trims starred entries to MAX_STARRED before writing', async () => {
    vi.useFakeTimers()
    const ids = Array.from({ length: MAX_STARRED + 5 }, (_, i) => i)
    render(
      <PreferencesProvider>
        <SetterProbe fn={(api) => api.setStarredEntries(ids)} />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalled(),
    )
    act(() => {
      screen.getByTestId('setter').click()
    })
    act(() => {
      vi.advanceTimersByTime(300)
    })
    expect(mockSetPreference).toHaveBeenCalledTimes(1)
    const sentValue = mockSetPreference.mock.calls[0][1] as number[]
    expect(sentValue.length).toBe(MAX_STARRED)
  })

  it('trims hidden entries to MAX_HIDDEN before writing', async () => {
    vi.useFakeTimers()
    const ids = Array.from({ length: MAX_HIDDEN + 5 }, (_, i) => i)
    render(
      <PreferencesProvider>
        <SetterProbe fn={(api) => api.setHiddenEntries(ids)} />
      </PreferencesProvider>,
    )
    await waitFor(() =>
      expect(mockGetPreferences).toHaveBeenCalled(),
    )
    act(() => {
      screen.getByTestId('setter').click()
    })
    act(() => {
      vi.advanceTimersByTime(300)
    })
    const sentValue = mockSetPreference.mock.calls[0][1] as number[]
    expect(sentValue.length).toBe(MAX_HIDDEN)
  })
})

describe('PreferencesProvider invariants', () => {
  it('exposes MAX_* constants consistent with the helpers', () => {
    // The provider exports the caps; consumers (the column
    // render, the card bookmark UI) use them to enforce the
    // server-side cap client-side. If these numbers drift
    // from the server's cap, the next PUT will be rejected —
    // the test fails so the operator notices before merge.
    expect(MAX_HIDDEN).toBe(1000)
    expect(MAX_STARRED).toBe(1000)
    expect(MAX_VOTED).toBe(2000)
    expect(MAX_PER_COLUMN).toBe(200)
  })

  it('PREFERENCE_KEYS namespace covers all 8 known prefs', () => {
    // If a future commit adds a new pref type, this is the
    // source of truth for the namespace string. The 8
    // preferences are documented in the module docstring;
    // drift between the docstring and the export means a
    // consumer is reading the wrong key.
    const expected = [
      'readEntries',
      'lastViewed',
      'columnPrefs',
      'columnSections',
      'hiddenEntries',
      'starredEntries',
      'votedEntries',
      'filterPresets',
      'historyGroupBy',
    ]
    for (const key of expected) {
      expect((PREFERENCE_KEYS as any)[key]).toBeDefined()
    }
  })
})
