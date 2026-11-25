import { expect, test } from '@playwright/test'
import { installRequirementsApi, syntheticToken } from './fixtures/requirementsApi'

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1180 })
  await page.addInitScript((token) => {
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('access_token', token)
  }, syntheticToken)
})

test('390px 미처방 HOME은 등록 진입과 읽기 쉬운 간격을 유지한다', async ({ page }) => {
  await installRequirementsApi(page)
  await page.goto('/')

  await expect(page.getByRole('heading', { name: '오늘 약, 함께 확인해요' })).toBeVisible()
  const registration = page.locator('.mvp-home__hub-card--prescription')
  await expect(registration).toBeInViewport()
  const heroBox = await page.locator('.mvp-home__hero').boundingBox()
  const registrationBox = await registration.boundingBox()
  expect(registrationBox!.y).toBeGreaterThan(heroBox!.y + heroBox!.height)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0)
  await expect(page.getByRole('button', { name: /일반의약품 안내/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /도지에게 질문하기/ })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/requirements/home-empty-390x1180.png' })
})

test('390px 처방 HOME은 오늘 일정을 먼저 보여주고 기존 카드와 마스코트 기능을 유지한다', async ({ page }) => {
  await installRequirementsApi(page, { existingPrescription: true })
  await page.goto('/')

  await expect(page.getByRole('heading', { name: '이번 주 복약 달성도' })).toBeVisible()
  const today = page.getByRole('region', { name: '오늘 복약 확인' })
  await expect(today).toBeVisible()
  const todayBox = await today.boundingBox()
  const adherenceBox = await page.locator('.mvp-home__adherence-card').boundingBox()
  expect(adherenceBox!.y).toBeGreaterThan(todayBox!.y + todayBox!.height)
  await expect(page.locator('.mvp-home__hub-card--report')).toBeVisible()
  await page.locator('.mvp-home__hub-card--new-prescription').scrollIntoViewIfNeeded()
  const nav = page.getByRole('navigation', { name: '주요 메뉴' })
  const newPrescriptionCard = page.locator(
    '.mvp-home__hub-card--new-prescription',
  )
  const navBox = await nav.boundingBox()
  const newPrescriptionBox = await newPrescriptionCard.boundingBox()

  expect(navBox).not.toBeNull()
  expect(newPrescriptionBox).not.toBeNull()
  expect(
    (navBox?.y ?? 0) -
      ((newPrescriptionBox?.y ?? 0) +
        (newPrescriptionBox?.height ?? 0)),
  ).toBeGreaterThanOrEqual(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0)

  await page.evaluate(() => {
    const state = window as typeof window & { __doseyDragStarts?: number }
    state.__doseyDragStarts = 0
    document.addEventListener('dragstart', (event) => {
      if ((event.target as Element | null)?.matches('.dosey-mascot img')) {
        state.__doseyDragStarts = (state.__doseyDragStarts ?? 0) + 1
      }
    })
  })

  const mascotImages = page.locator('.dosey-mascot img')
  expect(await mascotImages.count()).toBeGreaterThanOrEqual(3)
  for (let index = 0; index < await mascotImages.count(); index += 1) {
    const image = mascotImages.nth(index)
    await expect(image).toHaveAttribute('draggable', 'false')
    const box = await image.boundingBox()
    if (!box) continue
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 30, box.y + box.height / 2 + 20, { steps: 4 })
    await page.mouse.up()
  }
  expect(await page.evaluate(() => (
    window as typeof window & { __doseyDragStarts?: number }
  ).__doseyDragStarts)).toBe(0)

  await page.screenshot({ path: 'test-results/requirements/home-active-390x1180.png' })

  await page.getByRole('button', { name: '도지' }).click()
  await expect(page).toHaveURL(/\/chat/)
  await expect(page.getByText(/먼저 처방전을 등록해 주세요|무엇을 도와드릴까요/)).toBeVisible()
})

test('390x844 처방 HOME은 스크롤 끝에서도 마지막 카드와 Bottom Navigation이 겹치지 않는다', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await installRequirementsApi(page, { existingPrescription: true })
  await page.goto('/')

  const content = page.locator('.app-scroll')
  const lastCard = page.locator(
    '.mvp-home__hub-card--new-prescription',
 )
  const nav = page.getByRole('navigation', { name: '주요 메뉴' })

  await expect(lastCard).toBeVisible()
  await content.evaluate((element) =>
    element.scrollTo(0, element.scrollHeight),
  )

  const lastCardBox = await lastCard.boundingBox()
  const navBox = await nav.boundingBox()

  expect(lastCardBox).not.toBeNull()
  expect(navBox).not.toBeNull()
  expect(
    (navBox?.y ?? 0) -
      ((lastCardBox?.y ?? 0) + (lastCardBox?.height ?? 0)),
  ).toBeGreaterThanOrEqual(0)
})

test('360px 미처방 HOME은 등록 카드 아이콘과 문구가 겹치지 않는다', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 844 })
  await installRequirementsApi(page)
  await page.goto('/')

  const iconBox = await page.locator('.mvp-home__hub-icon').boundingBox()
  const copyBox = await page.locator('.mvp-home__hub-copy').boundingBox()
  expect(iconBox).not.toBeNull()
  expect(copyBox).not.toBeNull()
  expect((copyBox?.x ?? 0) - ((iconBox?.x ?? 0) + (iconBox?.width ?? 0))).toBeGreaterThanOrEqual(0)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0)
})
test('HOME 도지 안내는 1.5초 idle 후 표시되고 사용자 활동 시 숨는다', async ({
  page,
}) => {
  await installRequirementsApi(page, {
    existingPrescription: true,
  })
  await page.goto('/')

  const hint = page.getByLabel('도지 기능 안내')

  await expect(hint).toBeHidden()

  await page.waitForTimeout(1600)
  await expect(hint).toBeVisible()
  await expect(hint).toContainText(
    '처방약 이외에 다른 약을 복용해도 괜찮은지 물어볼 수 있어요!',
  )

  await page.mouse.move(40, 40)

  await expect(hint).toBeHidden()

  await page.waitForTimeout(1600)
  await expect(hint).toBeVisible()
})
