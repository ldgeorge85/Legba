/** Opens Consult's "⋯" menu (once), where the settings that are not on the composer live. */
import { fireEvent, screen } from '@testing-library/react'

export function openConsultMenu(): void {
  if (screen.queryByTestId('consult-more-menu')) return
  fireEvent.click(screen.getByTestId('consult-more'))
}
