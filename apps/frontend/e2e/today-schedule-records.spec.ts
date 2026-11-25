import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { MedicationCheckinSnapshot } from '../src/api/medicationSchedules'

const date = '2026-11-05'
const now = '2026-11-05T05:00:00Z'
const occurrenceIds = [1, 2, 3, 4, 5].map((id) => `11111111-1111-4111-8111-11111111111${id}`)
const medicationIds = ['22222222-2222-4222-8222-222222222221', '22222222-2222-4222-8222-222222222222']
const version = '33333333-3333-4333-8333-333333333333'

async function installFlowApi(page: Page) {
  const saved: Record<string, MedicationCheckinSnapshot> = {}
  const writes: { id: string; status: string; revision: number; key: string | undefined }[] = []
  const unexpected: string[] = []
  const medications = medicationIds.map((id, index) => ({
    prescription_version_medication_id: id, medication_name: `합성약 ${index ? 'B' : 'A'}`,
    strength_text: '5mg', dose_value: 1, dose_unit: '정', frequency_per_day: 2,
    timing_text: '처방에 기재된 복용 지시', duration_days: null, display_order: index,
  }))
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const json = (data: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
    if (path === '/api/v1/users/me') return json({ id: version, name: '합성 사용자', email: 'synthetic@example.com' })
    if (path === '/api/v1/prescriptions/latest') return json({ data: {
      prescription_id: version, prescription_version_id: version, revision: 1, current: true,
      document_id: version, prescribed_date: date, confirmed_at: now, medications,
    } })
    if (path === '/api/v1/medication-reports') return json({ data: {
      period_days: 7, start_date: '2026-10-30', end_date: date, timezone: 'Asia/Seoul', as_of: now,
      counts: { taken_count: 0, not_taken_count: 0, unconfirmed_count: 0, pending_count: 4, cancelled_count: 1 },
      overdue_pending_count: 0, adherence_rate: { numerator: 0, denominator: 0, percentage: null },
      confirmation_rate: { numerator: 0, denominator: 0, percentage: null }, records: [],
    } })
    if (path === '/api/v1/medication-occurrences') return json({ data: {
      schedule_status: 'READY',
      schedule_items: medicationIds.map((id) => ({
        prescription_version_medication_id: id, schedule_item_status: 'READY', schedule_id: id, revision: 1, setup_reason: null,
        schedule: { schedule_id: id, prescription_version_medication_id: id, revision: 1, status: 'ACTIVE', start_local_date: date, end_mode: 'OPEN_ENDED', end_local_date: null, local_times: ['13:00', '20:00'] },
      })),
      occurrences: url.searchParams.get('date') === date ? occurrenceIds.map((id, index) => ({
        occurrence_id: id, prescription_version_id: version, prescription_version_medication_id: medicationIds[index % 2],
        scheduled_local_date: date, scheduled_at: index < 2 ? '2026-11-05T04:00:00Z' : '2026-11-05T11:00:00Z',
        confirmation_deadline_at: '2026-11-05T18:00:00Z', status: index === 4 ? 'CANCELLED' : 'PENDING', checkin: saved[id] ?? null,
      })) : [],
    } })
    const detail = path.match(/^\/api\/v1\/medication-occurrences\/([^/]+)\/medication$/)
    if (detail) {
      const index = occurrenceIds.indexOf(detail[1])
      if (index >= 0) return json({ data: { ...medications[index % 2], occurrence_id: detail[1], prescription_version_id: version } })
    }
    const mutation = path.match(/^\/api\/v1\/medication-occurrences\/([^/]+)\/check-in$/)
    if (mutation && request.method() === 'PUT') {
      const id = mutation[1]
      const body = request.postDataJSON()
      expect(body.expected_revision).toBe(saved[id]?.revision ?? 0)
      writes.push({ id, status: body.status, revision: body.expected_revision, key: request.headers()['idempotency-key'] })
      saved[id] = {
        checkin_id: id, occurrence_id: id, status: body.status, taken_at: body.status === 'TAKEN' ? now : null,
        revision: body.expected_revision + 1, corrected: Boolean(saved[id]),
      }
      return json({ data: saved[id] })
    }
    unexpected.push(`${request.method()} ${path}`)
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
  })
  return { writes, unexpected }
}

for (const width of [320, 390, 412]) {
  test(`${width}px 오늘→복약 확인→일정→기록→수정이 날짜와 실제 revision을 유지한다`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await page.clock.setFixedTime(new Date(now))
    await page.addInitScript(() => localStorage.setItem('access_token', 'synthetic-flow-token'))
    const api = await installFlowApi(page)
    await page.goto('/')
    await page.getByRole('button', { name: '먹었는지 확인하기' }).click()
    await page.getByRole('button', { name: /^복용했어요/ }).click()
    await page.getByRole('button', { name: '다음 약 확인하기' }).click()
    await page.getByRole('button', { name: /^복용하지 않았어요/ }).click()
    expect(api.writes).toHaveLength(0)
    await page.getByRole('button', { name: '기록하기', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: '확인', exact: true }).click()
    await expect(page.getByText('2/2 기록 완료', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '전체 일정 보기' }).click()
    await expect(page).toHaveURL(new RegExp(`/schedule\\?date=${date}$`))
    await expect(page.locator('.schedule-occurrence-card')).toHaveCount(4)
    await expect(page.locator('.schedule-occurrence-card').getByText('복용 완료', { exact: true })).toHaveCount(1)
    await expect(page.locator('.schedule-occurrence-card').getByText('복용하지 않음', { exact: true })).toHaveCount(1)
    const history = page.locator('.schedule-page__cancelled-history')
    await expect(history).not.toHaveAttribute('open', '')
    await history.getByText('취소된 일정 1건 보기').click()
    await expect(history.getByText('현재 복용할 일정이 아니에요.', { exact: false })).toBeVisible()
    await page.getByRole('button', { name: '복약 기록 보기', exact: true }).click()
    await expect(page).toHaveURL(new RegExp(`/records\\?date=${date}$`))
    await expect(page.locator('.medication-record-card')).toHaveCount(4)
    await expect(page.locator('.medication-record-card--readonly')).toHaveCount(2)
    // The original occurrence route intentionally saves immediately on choice.
    const cardB = page.locator('.medication-record-card').filter({ hasText: '13:00' }).filter({ hasText: '합성약 B' })
    await cardB.getByRole('button').click()
    await expect(page).toHaveURL(new RegExp(`/schedule/occurrences/${occurrenceIds[1]}\\?date=${date}$`))
    await expect(page.getByText('아래 버튼을 누르면 바로 저장돼요.')).toBeVisible()
    await page.getByRole('button', { name: '복용했어요', exact: true }).click()
    await expect.poll(() => api.writes.length).toBe(3)
    expect(api.writes[2]).toMatchObject({ id: occurrenceIds[1], revision: 1, status: 'TAKEN' })
    expect(api.writes[2].key).toBeTruthy()
    await page.getByRole('button', { name: '이전 화면' }).click()
    await page.getByRole('button', { name: '복약 기록 보기', exact: true }).click()
    await expect(page.locator('.medication-record-card').getByText('복용 완료', { exact: true })).toHaveCount(2)
    const dates = page.getByRole('group', { name: '날짜 선택' }).getByRole('button')
    await expect(dates.nth(4)).toBeDisabled()
    await dates.nth(0).click()
    await expect(page).toHaveURL(/date=2026-11-02$/)
    await expect(page.locator('.medication-record__empty')).toBeVisible()
    expect(api.writes).toHaveLength(3)
    await dates.nth(3).click()
    await expect(page.locator('.medication-record-card')).toHaveCount(4)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(api.unexpected).toEqual([])
    await page.screenshot({ path: `test-results/requirements/records-flow-${width}.png` })
  })
}
