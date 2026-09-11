import { describe, expect, it } from 'vitest'

import type { OaReimbursementTravelProfile } from '@/types/reimbursements'
import {
  mappedTravelTypeOption,
  subsidyTripTypeForProfile,
  subsidyTripTypeForTravelLabel,
} from './travelTypes'

describe('travel type mapping', () => {
  it.each([
    ['境内商务出差', 'business'],
    ['境内市外项目出差（长期）', 'project'],
    ['境内市外项目出差（短期）', 'project'],
    ['境内市内项目出差（长/短期）', 'same_city_project'],
    ['公司内部出差（长期）', 'internal'],
    ['公司内部出差（短期）', 'internal'],
    ['境外商务出差（长期）', 'overseas'],
    ['境外商务出差（短期）', 'overseas'],
  ] as const)('keeps the current reimbursement label %s in policy family %s', (label, policy) => {
    expect(subsidyTripTypeForTravelLabel(label)).toBe(policy)
  })

  it('resolves a source value through the configured mapping', () => {
    const profile: OaReimbursementTravelProfile = {
      profileKey: 'domestic',
      displayName: '境内出差申请',
      processCode: 'PROC-DOMESTIC',
      schemaFingerprint: 'a'.repeat(64),
      travelTypeOption: { value: 'fixed', label: '固定类别', key: null },
      subsidyTripType: null,
      travelTypeMappings: {
        '市外项目出差（短期）': {
          value: 'domestic-short-project',
          label: '境内市外项目出差（短期）',
          key: 'domestic-short-project',
        },
      },
      subsidyTripTypeMappings: { '市外项目出差（短期）': 'project' },
    }

    expect(mappedTravelTypeOption(profile, '市外项目出差（短期）')?.label)
      .toBe('境内市外项目出差（短期）')
    expect(subsidyTripTypeForProfile(profile, '市外项目出差（短期）')).toBe('project')
    expect(mappedTravelTypeOption(profile, undefined)).toBeNull()
  })
})
