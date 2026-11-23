import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/client'
import type { MedicationDayResponse, MedicationOccurrenceData } from '../src/api/medicationSchedules'
import HomeTodaySection, { type HomeTodayServices } from '../src/pages/HomeTodaySection'

const NOW = Date.parse('2026-11-05T05:00:00Z') // KST 14:00
const DATE = '2026-11-05'
const occurrence = (id: string, extra: Partial<MedicationOccurrenceData> = {}): MedicationOccurrenceData => ({
  occurrence_id: id, prescription_version_id: 'version-1', prescription_version_medication_id: `med-${id}`,
  scheduled_local_date: DATE, scheduled_at: '2026-11-04T23:00:00Z', confirmation_deadline_at: '2026-11-05T18:00:00Z',
  status: 'PENDING', checkin: null, ...extra,
})
const day = (items: MedicationOccurrenceData[], status: MedicationDayResponse['data']['schedule_status'] = 'READY'): MedicationDayResponse => ({
  data: { schedule_status: status, schedule_items: [], occurrences: items },
})
function services(response = day([occurrence('a'), occurrence('b')])): HomeTodayServices {
  return {
    getMedicationDay: vi.fn().mockResolvedValue(response),
    getOccurrenceMedication: vi.fn(async (id) => ({ data: { occurrence_id: id, prescription_version_id: 'version-1', prescription_version_medication_id: `med-${id}`,
      medication_name: `예시약 ${id.toUpperCase()}`, strength_text: null, dose_value: null, dose_unit: null } })),
  }
}
function Destination() { const location = useLocation(); return <output data-testid="destination">{location.pathname}{location.search}</output> }
function mount(api: HomeTodayServices, now = NOW) {
  return render(<MemoryRouter><Routes><Route path="/" element={<HomeTodaySection now={now} services={api} />} /><Route path="*" element={<Destination />} /></Routes></MemoryRouter>)
}
afterEach(cleanup)
describe('Today home uses existing occurrence data', () => {
  it('shows due time and original names and opens the existing group route', async () => {
    const api = services(); mount(api)
    expect(await screen.findByText('예시약 A')).toBeTruthy()
    expect(screen.getByText('08:00')).toBeTruthy()
    expect(screen.getByText('0개 / 총 2개')).toBeTruthy()
    expect(api.getMedicationDay).toHaveBeenCalledWith(DATE, expect.any(AbortSignal))
    fireEvent.click(screen.getByRole('button', { name: '먹었는지 확인하기' }))
    expect(screen.getByTestId('destination').textContent).toBe('/schedule/checkin/a?date=2026-11-05')
  })
  it('does not offer premature recording for future-only schedules', async () => {
    mount(services(day([occurrence('a', { scheduled_at: '2026-11-05T11:00:00Z' })])))
    await screen.findByText('예시약 A')
    expect(screen.getByText('다음 확인 시간')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '먹었는지 확인하기' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '오늘 일정 보기' }))
    expect(screen.getByTestId('destination').textContent).toBe('/schedule?date=2026-11-05')
  })
  it('counts NOT_TAKEN as recorded, never as taken, and retains records access', async () => {
    const item = occurrence('a', { checkin: { checkin_id: 'check-a', occurrence_id: 'a', status: 'NOT_TAKEN', taken_at: null, revision: 1, corrected: false } })
    mount(services(day([item])))
    await screen.findByText('오늘 약의 기록을 남겼어요')
    expect(screen.getByText('1개 / 총 1개')).toBeTruthy()
    expect(screen.getByText('복용 0개 · 미복용 1개')).toBeTruthy()
    expect(screen.queryByText(/모두 복용/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '오늘 기록 보기' }))
    expect(screen.getByTestId('destination').textContent).toBe('/records?date=2026-11-05')
  })
  it('excludes cancelled occurrences and does not count UNCONFIRMED as recorded', async () => {
    mount(services(day([occurrence('cancel', { status: 'CANCELLED' }), occurrence('a', { checkin: { checkin_id: 'check-a', occurrence_id: 'a', status: 'UNCONFIRMED', taken_at: null, revision: 1, corrected: false } })])))
    await screen.findByText('예시약 A')
    expect(screen.getByText('0개 / 총 1개')).toBeTruthy()
    expect(screen.getByRole('button', { name: '먹었는지 확인하기' })).toBeTruthy()
  })
  it('separates setup-required from an empty day', async () => {
    const view = mount(services(day([], 'SETUP_REQUIRED')))
    expect(await screen.findByRole('button', { name: '복약 시간 설정하기' })).toBeTruthy()
    expect(screen.queryByText(/0개 \/ 총/)).toBeNull()
    view.unmount(); mount(services(day([])))
    expect(await screen.findByText('오늘 예정된 약이 없어요')).toBeTruthy()
    expect(screen.queryByText('오늘 약의 기록을 남겼어요')).toBeNull()
  })
  it('does not turn loading or failure into zero records and supports retry', async () => {
    const api = services(); vi.mocked(api.getMedicationDay).mockRejectedValueOnce(new Error('offline'))
    mount(api)
    expect(screen.getByText('오늘 약을 확인하고 있어요')).toBeTruthy()
    await screen.findByRole('alert')
    expect(screen.queryByText(/0개 \/ 총/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '오늘 일정 다시 불러오기' }))
    expect(await screen.findByText('예시약 A')).toBeTruthy()
    expect(api.getMedicationDay).toHaveBeenCalledTimes(2)
  })
  it('shows due and future groups separately without inferring meal times', async () => {
    mount(services(day([occurrence('a'), occurrence('b', { scheduled_at: '2026-11-05T11:00:00Z' })])))
    await screen.findByText('예시약 A')
    expect(screen.getByText('20:00 · 약 1개')).toBeTruthy()
    expect(screen.getByText('0개 / 총 2개')).toBeTruthy()
    expect(screen.queryByText('아침 약')).toBeNull()
  })
  it('never shows a medication from a mismatched immutable snapshot', async () => {
    const api = services(day([occurrence('a')]))
    vi.mocked(api.getOccurrenceMedication).mockResolvedValueOnce({ data: { occurrence_id: 'other', prescription_version_id: 'other-version', prescription_version_medication_id: 'other-med', medication_name: '잘못 연결된 약', strength_text: null, dose_value: null, dose_unit: null } })
    mount(api)
    await screen.findByText(/약 이름을 불러오지 못했어요/)
    expect(screen.queryByText('잘못 연결된 약')).toBeNull()
    expect(screen.queryByText('약 이름 확인 중')).toBeNull()
  })
  it.each([{ scheduled_local_date: '2026-11-04' }, { scheduled_at: 'invalid' }])('rejects wrong day/time instead of offering recording: %j', async (extra) => {
    mount(services(day([occurrence('a', extra)])))
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: '먹었는지 확인하기' })).toBeNull()
  })
  it('offers reauthentication on expired access without empty data', async () => {
    const api = services(); vi.mocked(api.getMedicationDay).mockRejectedValue(new ApiError(401, 'Expired', 'EXPIRED_TOKEN'))
    mount(api); await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: '로그인하기' })).toBeTruthy()
    expect(screen.queryByText('오늘 예정된 약이 없어요')).toBeNull()
  })
  it('aborts an old day response when the KST date rolls over', async () => {
    const api = services(day([])); let complete: ((value: MedicationDayResponse) => void) | undefined
    vi.mocked(api.getMedicationDay).mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    const view = render(<MemoryRouter><HomeTodaySection now={NOW} services={api} /></MemoryRouter>)
    view.rerender(<MemoryRouter><HomeTodaySection now={Date.parse('2026-11-05T15:01:00Z')} services={api} /></MemoryRouter>)
    await waitFor(() => expect(api.getMedicationDay).toHaveBeenCalledWith('2026-11-06', expect.any(AbortSignal)))
    await act(async () => complete?.(day([occurrence('a')])))
    expect(await screen.findByText('오늘 예정된 약이 없어요')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '먹었는지 확인하기' })).toBeNull()
  })
})
