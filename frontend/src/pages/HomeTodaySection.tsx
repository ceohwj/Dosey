import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client'
import {
  getMedicationDay,
  getOccurrenceMedication,
  type MedicationDayResponse,
} from '../api/medicationSchedules'
import { Button } from '../design-system/components'
import { clearAuthenticatedSession } from '../features/auth/authSession'
import {
  getGroupProgress,
  groupOccurrencesByScheduledAt,
  isCheckinTarget,
  isUserRecorded,
} from './checkinGroupView'
import './HomeTodaySection.css'

export type HomeTodayServices = {
  getMedicationDay: typeof getMedicationDay
  getOccurrenceMedication: typeof getOccurrenceMedication
}

const defaultServices: HomeTodayServices = { getMedicationDay, getOccurrenceMedication }
const TIME_ZONE = 'Asia/Seoul'

function homeLocalDate(now: number): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE }).format(now)
}

function clock(value: string): string {
  return new Intl.DateTimeFormat('ko-KR', {
    timeZone: TIME_ZONE, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

export default function HomeTodaySection({
  now,
  services = defaultServices,
}: {
  now: number
  services?: HomeTodayServices
}) {
  const navigate = useNavigate()
  const date = homeLocalDate(now)
  const [day, setDay] = useState<MedicationDayResponse['data'] | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadedDate, setLoadedDate] = useState<string | null>(null)
  const [failure, setFailure] = useState<'AUTH' | 'LOAD' | null>(null)
  const [reload, setReload] = useState(0)
  const [names, setNames] = useState<Record<string, string>>({})
  const [namesFailed, setNamesFailed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setFailure(null)
    setDay(null)
    setLoadedDate(null)
    void services.getMedicationDay(date, controller.signal).then(({ data }) => {
      if (controller.signal.aborted) return
      // A different day or invalid timestamp must not become a recordable home card.
      if (data.occurrences.some((item) => isCheckinTarget(item) && (
        item.scheduled_local_date !== date || !Number.isFinite(Date.parse(item.scheduled_at))
      ))) throw new Error('Invalid day schedule')
      setDay(data)
      setLoadedDate(date)
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setFailure(error instanceof ApiError && (error.status === 401 || error.status === 403)
          ? 'AUTH' : 'LOAD')
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [date, reload, services])

  const targets = useMemo(() => day?.occurrences.filter(isCheckinTarget) ?? [], [day])
  const groups = useMemo(() => groupOccurrencesByScheduledAt(targets), [targets])
  const due = groups.find((group) => getGroupProgress(group, now).state === 'ATTENTION')
  const upcoming = groups.find((group) => getGroupProgress(group, now).state === 'UPCOMING')
  const featured = due ?? upcoming

  useEffect(() => {
    const controller = new AbortController()
    setNames({})
    setNamesFailed(false)
    if (!featured) return () => controller.abort()
    void Promise.all(featured.occurrences.map(async (occurrence) => {
      const { data } = await services.getOccurrenceMedication(occurrence.occurrence_id, controller.signal)
      // Use the original occurrence snapshot, never another/current prescription as a fallback.
      if (data.occurrence_id !== occurrence.occurrence_id ||
        data.prescription_version_id !== occurrence.prescription_version_id ||
        data.prescription_version_medication_id !== occurrence.prescription_version_medication_id ||
        !data.medication_name.trim()) throw new Error('Occurrence medication unavailable')
      return [occurrence.occurrence_id, [data.medication_name, data.strength_text].filter(Boolean).join(' ')] as const
    })).then((entries) => {
      if (!controller.signal.aborted) setNames(Object.fromEntries(entries))
    }).catch(() => {
      if (!controller.signal.aborted) setNamesFailed(true)
    })
    return () => controller.abort()
  }, [featured, services])

  const openSchedule = () => navigate(`/schedule?date=${date}`)
  const recorded = targets.filter((item) => isUserRecorded(item.checkin)).length
  const taken = targets.filter((item) => item.checkin?.status === 'TAKEN').length
  const notTaken = targets.filter((item) => item.checkin?.status === 'NOT_TAKEN').length
  const needsSetup = day?.schedule_status === 'SETUP_REQUIRED' || day?.schedule_status === 'PARTIAL'

  if (loading || (!failure && loadedDate !== date)) return <section className="home-today home-today--state" role="status">
    <h2>오늘 약을 확인하고 있어요</h2>
    <p>설정한 복약 일정을 불러오고 있어요.</p>
  </section>

  if (failure) return <section className="home-today home-today--state" aria-labelledby="home-today-error">
    <h2 id="home-today-error">{failure === 'AUTH' ? '다시 로그인해 주세요' : '오늘 일정을 불러오지 못했어요'}</h2>
    <p role="alert">{failure === 'AUTH' ? '로그인 정보를 확인한 뒤 계속할 수 있어요.' : '일정 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.'}</p>
    <Button fullWidth onClick={failure === 'AUTH' ? () => {
      clearAuthenticatedSession()
      navigate('/login', { replace: true })
    } : () => setReload((value) => value + 1)}>{failure === 'AUTH' ? '로그인하기' : '오늘 일정 다시 불러오기'}</Button>
  </section>

  return <section className="home-today-section" aria-label="오늘 복약 확인">
    <section className="home-today" aria-labelledby="home-today-heading">
      <p className="home-today__eyebrow">{due ? '지금 확인할 약' : upcoming ? '다음 확인 시간' : '오늘의 복약 일정'}</p>
      {featured ? <>
        <h2 id="home-today-heading"><time dateTime={featured.scheduledAt}>{clock(featured.scheduledAt)}</time><span>약 {featured.occurrences.length}개</span></h2>
        <ul className="home-today__medications">
          {featured.occurrences.map((item) => <li key={item.occurrence_id}>
            <span>{names[item.occurrence_id] ?? (namesFailed ? '약 이름 확인 필요' : '약 이름 확인 중')}</span>
            {isUserRecorded(item.checkin) && <small>{item.checkin.status === 'TAKEN' ? '먹었어요' : '먹지 않았어요'}</small>}
          </li>)}
        </ul>
        {namesFailed && <p className="home-today__help" role="status">약 이름을 불러오지 못했어요. 복약 확인 화면에서 다시 확인할 수 있어요.</p>}
        {due ? <Button fullWidth onClick={() => navigate(`/schedule/checkin/${due.occurrences[0].occurrence_id}?date=${date}`)}>먹었는지 확인하기</Button>
          : <><p className="home-today__help">내가 설정한 시간이에요. 아직 기록할 시간이 되지 않았어요.</p><Button fullWidth variant="secondary" onClick={openSchedule}>오늘 일정 보기</Button></>}
      </> : <>
        <h2 id="home-today-heading">{needsSetup ? '복약 시간을 정해 주세요' : targets.length ? '오늘 약의 기록을 남겼어요' : '오늘 예정된 약이 없어요'}</h2>
        <p>{needsSetup ? '확인한 처방을 바탕으로 복약 시간을 설정해 주세요.' : targets.length ? '먹었는지 확인한 내용은 기록에서 다시 볼 수 있어요.' : '다른 날짜의 일정도 확인할 수 있어요.'}</p>
        <Button fullWidth variant={needsSetup ? 'primary' : 'secondary'} onClick={targets.length && !needsSetup
          ? () => navigate(`/records?date=${date}`) : openSchedule}>{needsSetup ? '복약 시간 설정하기' : targets.length ? '오늘 기록 보기' : '일정 확인하기'}</Button>
      </>}
    </section>
    {targets.length > 0 && <section className="home-today__progress" aria-label="오늘 기록 현황">
      <h2>오늘 남긴 기록 <strong>{recorded}개 / 총 {targets.length}개</strong></h2>
      <div className="home-today__track" aria-hidden="true"><span style={{ width: `${recorded / targets.length * 100}%` }} /></div>
      <p>복용 {taken}개 · 미복용 {notTaken}개</p>
    </section>}
    {due && upcoming && <section className="home-today__next" aria-label="다음 복약 일정">
      <h2>다음 일정</h2><p>{clock(upcoming.scheduledAt)} · 약 {upcoming.occurrences.length}개</p>
      <span>내가 설정한 복약 시간이에요.</span>
    </section>}
    {featured && needsSetup && <p className="home-today__help">아직 시간을 설정하지 않은 약이 있어요. <button type="button" onClick={openSchedule}>일정에서 확인하기</button></p>}
  </section>
}
