import { expect, test } from '@playwright/test'
import { ids, installRequirementsApi, syntheticToken } from './fixtures/requirementsApi'

const answer = Array.from({ length: 14 }, (_, index) =>
  `${index + 1}. 합성 답변 ${index + 1}: 확인된 처방 내용과 의료진 또는 약사의 안내를 확인해 주세요. 임의로 복용 방법을 바꾸지 마세요.`,
).join('\n\n')

for (const width of [320, 390, 412]) {
  test(`${width}px 가이드→처방 상담→질문 선택→전송 실패 재시도→긴 답변 읽기를 유지한다`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await page.addInitScript((token) => {
      localStorage.setItem('access_token', token)
    }, syntheticToken)
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    const api = await installRequirementsApi(page, { existingPrescription: true, existingGuide: true })
    const requests: string[] = []
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    await page.route(`**/api/v1/chat-sessions/${ids.session}/messages`, async (route) => {
      const request = route.request()
      if (request.method() !== 'POST') return route.fallback()
      requests.push(request.postDataJSON().content)
      if (requests.length === 1) {
        await gate
        return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ code: 'CONSENT_POLICY_UNAVAILABLE', message: 'synthetic failure', details: [], trace_id: 'synthetic-only' }) })
      }
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ data: {
        session_id: ids.session, user_message_id: '88888888-8888-4888-8888-888888888881',
        assistant_message_id: '88888888-8888-4888-8888-888888888882',
        generation_status: 'COMPLETED', content: answer, model_name: 'synthetic-only', prompt_version: 'synthetic-only',
        created_at: '2026-11-05T00:00:00Z', completed_at: '2026-11-05T00:00:01Z',
      } }) })
    })
    await page.goto(`/guides/${ids.guide}`)
    await page.evaluate(() => document.documentElement.style.setProperty('--ds-safe-bottom', '34px'))
    await expect(page.getByRole('heading', { name: '확인된 복약 안내' })).toBeVisible()
    expect(api.chatSessionCreationCount).toBe(0)
    await page.getByRole('button', { name: '복약 챗봇 도지와 이야기하기' }).click()
    await expect(page).toHaveURL(new RegExp(`/chat\\?prescription_id=${ids.prescription}$`))
    await expect(page.getByText('무엇을 도와드릴까요?')).toBeVisible()
    const input = page.getByLabel('복약 질문', { exact: true })
    const send = page.getByRole('button', { name: '질문 전송', exact: true })
    await expect(send).toBeDisabled()
    const preset = page.getByRole('button', { name: '아침 약은 언제 먹나요?', exact: true })
    await preset.click()
    await expect(input).toHaveValue('아침 약은 언제 먹나요?')
    expect(requests).toHaveLength(0)
    await input.focus()
    await input.press('End')
    await input.press('Shift+Enter')
    await page.keyboard.insertText('추가 질문')
    await expect(input).toHaveValue('아침 약은 언제 먹나요?\n추가 질문')
    await expect(page.locator('.chat-message.user')).toHaveCount(0)
    expect(requests).toHaveLength(0)
    await input.fill('확인된 처방 내용을\n어디에서 다시 볼 수 있나요?')
    const draft = await input.inputValue()
    expect(api.chatSessionCreationCount).toBe(1)
    await send.click()
    await expect(page.getByRole('status').filter({ hasText: '답변을 확인하고 있어요' })).toBeVisible()
    await expect(send).toBeDisabled()
    await expect(input).toBeDisabled()
    await expect.poll(() => [...requests]).toEqual([draft])
    release()
    await page.getByRole('button', { name: '다시 시도', exact: true }).click()
    await expect(page.locator('.chat-message.assistant')).toContainText('합성 답변 14:')
    await expect.poll(() => [...requests]).toEqual([draft, draft])
    await expect(page.locator('.chat-message.user')).toHaveCount(1)
    const messages = page.locator('.chat-messages')
    expect(await messages.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true)
    await messages.hover()
    await page.mouse.wheel(0, -100000)
    await expect(page.locator('.chat-message.user')).toBeInViewport()
    await messages.evaluate((element) => { element.scrollTop = element.scrollHeight })
    expect(await messages.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    for (const height of [844, 480]) {
      await page.setViewportSize({ width, height })
      const composer = await page.locator('.chat-composer').boundingBox()
      const navigation = await page.getByRole('navigation', { name: '주요 메뉴' }).boundingBox()
      expect(composer!.y).toBeGreaterThanOrEqual(0)
      expect(composer!.y + composer!.height).toBeLessThanOrEqual(navigation!.y)
      expect((await messages.boundingBox())!.height).toBeGreaterThanOrEqual(96)
      expect((await send.boundingBox())!.height).toBeGreaterThanOrEqual(60)
      await input.fill('입력 중인 합성 질문')
      await input.focus()
      expect(requests).toHaveLength(2)
    }
    expect(api.unexpectedRequests).toEqual([])
    expect(errors).toEqual([])
  })
}
