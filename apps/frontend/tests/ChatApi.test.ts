import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getChatMessages,
  getChatSessionForPrescription,
  sendChatMessage,
  type ChatCitationItem,
} from '../src/api/chat'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Chat rediscovery API', () => {
  it('확정 처방에 연결된 ACTIVE Chat session을 조회한다', async () => {
    const prescriptionId = '11111111-1111-4111-8111-111111111111'
    const responseBody = {
      data: {
        session_id: '22222222-2222-4222-8222-222222222222',
        prescription_id: prescriptionId,
        session_status: 'ACTIVE',
        created_at: '2026-09-08T00:00:00Z',
      },
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      getChatSessionForPrescription(prescriptionId),
    ).resolves.toEqual(responseBody)
    expect(fetchMock).toHaveBeenCalledWith(
      `http://localhost:8000/api/v1/prescriptions/${prescriptionId}/chat-session`,
      expect.any(Object),
    )
  })

  it('sendChatMessage 응답의 citations 필드를 보존한다', async () => {
    const sessionId = '22222222-2222-4222-8222-222222222222'
    const citations: ChatCitationItem[] = [
      {
        claim_key: 'claim-1',
        claim_text: '식후 30분에 복용하는 것이 좋습니다.',
        verdict: 'SUPPORTED',
        source_code: 'MFDS_DRUG_INFO',
        source_version: '2026-09-01',
        locator: '용법·용량',
        display_order: 1,
      },
      {
        claim_key: 'claim-2',
        claim_text: '충분한 물과 함께 드세요.',
        verdict: 'NEEDS_VERIFICATION',
        source_code: 'MFDS_DRUG_INFO',
        source_version: '2026-09-01',
        locator: '사용상 주의사항',
        display_order: 2,
      },
    ]
    const responseBody = {
      data: {
        user_message_id: 'user-msg-1',
        assistant_message_id: 'assistant-msg-1',
        session_id: sessionId,
        generation_status: 'COMPLETED',
        content: '식후 30분에 복용하세요.',
        model_name: 'chat-model',
        prompt_version: 'chat-v1',
        created_at: '2026-09-08T00:00:00Z',
        completed_at: '2026-09-08T00:00:01Z',
        citations,
      },
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await sendChatMessage(sessionId, '언제 먹나요?')
    expect(result).toEqual(responseBody)
    expect(result.data.citations).toEqual(citations)
    expect(fetchMock).toHaveBeenCalledWith(
      `http://localhost:8000/api/v1/chat-sessions/${sessionId}/messages`,
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ content: '언제 먹나요?' }),
      }),
    )
  })

  it('getChatMessages 응답의 citations 배열 및 null 값을 그대로 반환한다', async () => {
    const sessionId = '22222222-2222-4222-8222-222222222222'
    const responseBody = {
      data: {
        session_id: sessionId,
        messages: [
          {
            message_id: 'user-msg-1',
            role: 'USER',
            content: '언제 먹나요?',
            generation_status: 'NOT_APPLICABLE',
            created_at: '2026-09-08T00:00:00Z',
            citations: null,
          },
          {
            message_id: 'assistant-msg-1',
            role: 'ASSISTANT',
            content: '식후 30분에 복용하세요.',
            generation_status: 'COMPLETED',
            created_at: '2026-09-08T00:00:01Z',
            citations: [
              {
                claim_key: 'claim-1',
                claim_text: '식후 30분에 복용하세요.',
                verdict: 'SUPPORTED',
                source_code: 'MFDS_DRUG_INFO',
                source_version: '2026-09-01',
                locator: '용법·용량',
                display_order: 1,
              },
            ],
          },
        ],
      },
    }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await getChatMessages(sessionId)
    expect(result).toEqual(responseBody)
    expect(result.data.messages[0].citations).toBeNull()
    expect(result.data.messages[1].citations).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledWith(
      `http://localhost:8000/api/v1/chat-sessions/${sessionId}/messages`,
      expect.any(Object),
    )
  })
})
