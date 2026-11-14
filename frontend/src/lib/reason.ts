/** Why a song is suggested, in words: the voices first, then the moods, a style and the decade. */
import { useCallback } from 'react'
import { useTranslation } from 'react-i18next'

import { decadeName, moodName } from './music'
import type { Reason } from './suggest'

export function useReasonText() {
  const { t } = useTranslation()
  return useCallback(
    (reason: Reason) =>
      [
        reason.sound ? t('suggest.soundsAlike') : '',
        reason.together ? t('suggest.together') : '',
        reason.moods.map(moodName).join(', '),
        reason.style ?? '',
        reason.decade ? decadeName(reason.decade) : '',
      ]
        .filter(Boolean)
        .join(' · '),
    [t],
  )
}
