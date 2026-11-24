/**
 * Test helper: pick an option in a `ScopePicker` (the `Select` primitive).
 *
 * Before UI-B1/tokens the picker was a native `<select>` and the panel tests
 * drove it with `fireEvent.change(select, {target: {value}})`. It is now a
 * Radix listbox: the trigger opens on Enter, the options render (into the
 * portal) only while it is open, and an option is picked by a click. The
 * options load asynchronously from the registry, so this opens the picker and
 * waits for the wanted option to land, then picks it and waits for the
 * trigger's `data-value` to reflect it — the same "the controlled value took"
 * assertion the old helpers made against `select.value`.
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'

export async function pickScopeOption(testId: string, value: string): Promise<void> {
  const trigger = await screen.findByTestId(testId)
  const optionId = `${testId}-option-${value}`
  await waitFor(() => {
    if (!screen.queryByTestId(optionId) && trigger.getAttribute('data-state') !== 'open') {
      fireEvent.keyDown(trigger, { key: 'Enter' })
    }
    expect(screen.getByTestId(optionId)).toBeInTheDocument()
  })
  fireEvent.click(screen.getByTestId(optionId))
  await waitFor(() => expect(trigger).toHaveAttribute('data-value', value))
}
