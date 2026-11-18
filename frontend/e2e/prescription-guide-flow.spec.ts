import { expect, test } from '@playwright/test'
import { ids, installRequirementsApi, syntheticToken } from './fixtures/requirementsApi'

const content = [
  '복약 가이드',
  '[1] 합성 처방약 100mg\n용량: 1 정\n복용 횟수: 하루 3회\n복용 시점: 식후\n복용 기간: 7일\n복용 시 주의해야 할 점: 처방에 안내된 복용 계획을 확인해 주세요.',
  '공통 안내: 불명확한 내용은 의료진 또는 약사에게 확인해 주세요.\n안전 안내: 임의로 복용을 중단하거나 변경하지 마세요.',
].join('\n\n')

for (const width of [320, 390, 412]) {
  test(`${width}px 등록→OCR 누락값 확인→명시적 확정→근거 있는 가이드→일정 흐름을 유지한다`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await page.addInitScript((token) => localStorage.setItem('access_token', token), syntheticToken)
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    const api = await installRequirementsApi(page, { missingPrescribedDate: true, jobStatuses: ['PROCESSING', 'COMPLETED'] })
    let confirms = 0
    let guides = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/prescription')) confirms += 1
    })
    await page.route('**/api/v1/guides**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (request.method() === 'POST' && path === '/api/v1/guides') guides += 1
      else if (!(request.method() === 'GET' && path === `/api/v1/guides/${ids.guide}`)) return route.fallback()
      return route.fulfill({
        status: request.method() === 'POST' ? 201 : 200,
        contentType: 'application/json',
        body: JSON.stringify({ data: {
          guide_id: ids.guide, prescription_id: ids.prescription, prescription_version_id: ids.prescriptionVersion,
          generation_status: 'COMPLETED', content, model_name: 'synthetic-only', prompt_version: 'synthetic-only',
          release_decision: 'PASS', release_is_current: true, fallback_code: null, fallback_text: null,
          citations: [{ source_type: 'LIFESTYLE_GUIDELINE', source_code: 'SYNTHETIC-SOURCE', source_version: '2026.1', locator: 'section-1', display_order: 1 }],
          requested_at: '2026-11-05T00:00:00Z', completed_at: '2026-11-05T00:00:01Z',
        } }),
      })
    })
    await page.goto('/prescriptions/upload')
    await expect(page.getByRole('list', { name: '처방전 등록 순서' })).toBeVisible()
    await expect(page.getByText('지원 파일: JPG · JPEG · PNG · PDF / 최대 30MB')).toBeVisible()
    await page.locator('input[type=file]').nth(1).setInputFiles({
      name: `합성_검증용_긴_처방전_파일이름_${'가나다'.repeat(12)}.png`, mimeType: 'image/png',
      buffer: Buffer.from('synthetic image; OCR API is mocked'),
    })
    await page.getByRole('button', { name: '전체 파일명 보기' }).click()
    await expect(page.getByRole('status').filter({ hasText: '파일을 선택했어요.' })).toBeVisible()
    const read = page.getByRole('button', { name: '처방전 읽기', exact: true })
    await read.scrollIntoViewIfNeeded()
    expect((await read.boundingBox())!.height).toBeGreaterThanOrEqual(60)
    await read.click()
    await expect(page.getByText('누락된 항목을 직접 입력해 주세요')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('button', { name: '처방전 확정 및 가이드 만들기' })).toBeDisabled()
    expect(confirms).toBe(0)
    expect(guides).toBe(0)
    await page.getByLabel('처방일', { exact: true }).fill('2026-09-08')
    await page.getByRole('button', { name: '수정완료', exact: true }).click()
    await expect(page.getByText('약 1/1개 검토 완료')).toBeVisible()
    await page.getByRole('checkbox').check()
    const confirm = page.getByRole('button', { name: '처방전 확정 및 가이드 만들기' })
    await confirm.scrollIntoViewIfNeeded()
    expect((await confirm.boundingBox())!.height).toBeGreaterThanOrEqual(60)
    await confirm.click()
    await expect(page).toHaveURL(new RegExp(`/guides/${ids.guide}$`))
    await expect(page.getByRole('heading', { name: /^확인된 약 목록/ })).toBeVisible()
    const medication = page.getByRole('button', { name: /합성 처방약 100mg/ })
    await expect(medication).toHaveAttribute('aria-expanded', 'false')
    await medication.focus()
    await page.keyboard.press('Enter')
    await expect(medication).toHaveAttribute('aria-expanded', 'true')
    await expect(page.getByRole('heading', { name: '안내 근거' })).toBeVisible()
    await expect(page.getByText('SYNTHETIC-SOURCE', { exact: true })).toBeVisible()
    await medication.focus()
    await page.keyboard.press('Space')
    await expect(medication).toHaveAttribute('aria-expanded', 'false')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    expect(confirms).toBe(1)
    expect(guides).toBe(1)
    expect(api.uploadCount).toBe(1)
    expect(api.confirmedMedicationCount).toBe(1)
    expect(api.idempotencyKeys).toHaveLength(1)
    const schedule = page.getByRole('button', { name: '복용 일정 확인하기', exact: true })
    await schedule.scrollIntoViewIfNeeded()
    const box = await schedule.boundingBox()
    const nav = await page.getByRole('navigation', { name: '주요 메뉴' }).boundingBox()
    expect(box!.height).toBeGreaterThanOrEqual(60)
    expect(box!.y + box!.height).toBeLessThanOrEqual(nav!.y)
    await schedule.click()
    await expect(page).toHaveURL(/\/schedule\?date=\d{4}-\d{2}-\d{2}$/)
    expect(api.unexpectedRequests).toEqual([])
    expect(errors).toEqual([])
  })
}
