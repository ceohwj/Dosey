import { expect, test } from '@playwright/test'
import {
  installRequirementsApi,
  syntheticToken,
} from './fixtures/requirementsApi'

const mobileWidths = [320, 390, 412] as const

test.beforeEach(async ({ page }) => {
  await page.addInitScript((token) => {
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('access_token', token)
  }, syntheticToken)
})

test('390px Chat 재진입은 읽기 쉬운 질문·전송 버튼과 기존 입력 동작을 유지한다', async ({ page }) => {
  await page.addInitScript(() => {
    document.documentElement.style.setProperty('--ds-safe-bottom', '0px')
  })
  await page.setViewportSize({ width: 390, height: 860 })
  await installRequirementsApi(page, {
    existingPrescription: true,
    existingChat: true,
  })

  const mutations: string[] = []
  page.on('request', (request) => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      mutations.push(`${request.method()} ${request.url()}`)
    }
  })

  await page.goto('/chat')
  await expect(page.getByText('무엇을 도와드릴까요?')).toBeVisible()

  const topbar = page.locator('.app-topbar')
  const headerMascot = topbar.locator('.dosey-mascot--header')
  const topbarTitle = topbar.getByRole('heading', { name: 'Dosey 도지' })
  const intro = page.locator('.chat-page__intro')
  const conversation = page.locator('.chat-page__conversation')
  const conversationSurface = page.locator('.chat-messages')
  const greeting = page.locator('.chat-page__greeting')
  const avatar = greeting.locator('.dosey-mascot--chat')
  const bubble = page.locator('.chat-page__empty-card')
  const preset = page.getByRole('button', { name: '아침 약은 언제 먹나요?' })
  const composer = page.locator('.chat-composer')
  const input = page.getByLabel('복약 질문')
  const send = page.getByRole('button', { name: '질문 전송' })
  const navigation = page.getByRole('navigation', { name: '주요 메뉴' })

  await expect(intro).toBeVisible()
  await expect(greeting).toBeVisible()
  await expect(topbar).toBeVisible()
  await expect(headerMascot).toBeVisible()
  await expect(topbarTitle).toBeVisible()
  await expect(avatar).toBeVisible()
  await expect(bubble).toBeVisible()
  const composerBox = await composer.boundingBox()
  const navigationBox = await navigation.boundingBox()
  const presetBox = await preset.boundingBox()
  expect((presetBox?.height ?? 0)).toBeGreaterThanOrEqual(60)
  expect((await send.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(60)
  expect((await input.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(60)
  expect((navigationBox?.y ?? 0) - ((composerBox?.y ?? 0) + (composerBox?.height ?? 0))).toBeGreaterThanOrEqual(0)
  expect(await conversationSurface.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  expect((await conversation.boundingBox())!.width).toBeLessThanOrEqual(390)
  await expect(page.getByRole('button', { name: '도지' })).toHaveAttribute('aria-current', 'page')

  await preset.click()
  await expect(page.getByLabel('복약 질문')).toHaveValue('아침 약은 언제 먹나요?')
  expect(mutations).toEqual([])
})

test('Chat rediscovery initial은 320/390/412px safe-area에서 clipping과 navigation 겹침이 없다', async ({ page }) => {
  await page.addInitScript(() => {
    document.documentElement.style.setProperty('--ds-safe-bottom', '34px')
  })
  await installRequirementsApi(page, {
    existingPrescription: true,
    existingChat: true,
  })

  for (const width of mobileWidths) {
    await test.step(`${width}px`, async () => {
      await page.setViewportSize({ width, height: 860 })
      await page.goto('/chat')
      await expect(page.getByText('현재 확인된 처방과 제공된 근거 범위에서만 답해요.')).toBeVisible()
      await page.evaluate(() => {
        document.documentElement.style.setProperty('--ds-safe-bottom', '34px')
      })

      const preset = page.getByRole('button', { name: '약을 함께 먹어도 되나요?' })
      const composer = page.locator('.chat-composer')
      const input = page.getByLabel('복약 질문')
      const subtitle = page.getByText('현재 확인된 처방과 제공된 근거 범위에서만 답해요.')
      const navigation = page.getByRole('navigation', { name: '주요 메뉴' })
      const presetBox = await preset.boundingBox()
      const composerBox = await composer.boundingBox()
      const navigationBox = await navigation.boundingBox()

      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0)
      expect(await page.locator('.chat-messages').evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0)
      expect(presetBox).not.toBeNull()
      expect(composerBox).not.toBeNull()
      expect(navigationBox).not.toBeNull()
      expect(presetBox?.x ?? -1).toBeGreaterThanOrEqual(0)
      expect((presetBox?.x ?? 0) + (presetBox?.width ?? 0)).toBeLessThanOrEqual(width)
      expect(composerBox?.x ?? -1).toBeGreaterThanOrEqual(0)
      expect((composerBox?.x ?? 0) + (composerBox?.width ?? 0)).toBeLessThanOrEqual(width)
      expect((navigationBox?.y ?? 0) - ((composerBox?.y ?? 0) + (composerBox?.height ?? 0))).toBeGreaterThanOrEqual(0)
      expect(navigationBox?.height).toBeCloseTo(98, 0)

      expect(await subtitle.evaluate((element) => element.scrollWidth - element.clientWidth)).toBe(0)
      await input.fill('현재 처방과 복용 시간을 함께 확인하고 싶어서 길게 질문을 입력합니다.')
      await input.focus()
      await page.locator('.chat-layout').evaluate((element) => element.classList.add('keyboard-open'))
      const keyboardComposerBox = await composer.boundingBox()
      expect(keyboardComposerBox).not.toBeNull()
      expect(keyboardComposerBox?.x ?? -1).toBeGreaterThanOrEqual(0)
      expect((keyboardComposerBox?.x ?? 0) + (keyboardComposerBox?.width ?? 0)).toBeLessThanOrEqual(width)
      expect(await input.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0)
      expect((navigationBox?.y ?? 0) - ((keyboardComposerBox?.y ?? 0) + (keyboardComposerBox?.height ?? 0))).toBeGreaterThanOrEqual(0)
    })
  }
})
