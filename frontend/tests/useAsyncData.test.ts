import { describe, it, expect, vi, beforeEach } from 'vitest'

const elMessageMock = vi.hoisted(() => ({ error: vi.fn() }))
vi.mock('element-plus', () => ({
  ElMessage: elMessageMock,
}))

import { useAsyncData } from '@/composables/useAsyncData'

describe('useAsyncData', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('refresh 成功时写入 data 并清除 loading', async () => {
    const { data, loading, error, refresh } = useAsyncData(
      async () => ({ ok: true }),
      { immediate: false },
    )
    expect(loading.value).toBe(false)
    await refresh()
    expect(data.value).toEqual({ ok: true })
    expect(loading.value).toBe(false)
    expect(error.value).toBe('')
  })

  it('默认失败时弹全局错误提示并记录 error', async () => {
    const { error, refresh } = useAsyncData(
      async () => {
        throw new Error('boom')
      },
      { immediate: false, defaultErrorMessage: '自定义失败' },
    )
    await refresh()
    expect(error.value).toBe('boom')
    expect(elMessageMock.error).toHaveBeenCalledWith('boom')
  })

  it('silent 刷新失败时不弹提示（轮询场景）', async () => {
    const { error, refresh } = useAsyncData(
      async () => {
        throw new Error('poll-fail')
      },
      { immediate: false },
    )
    await refresh(true)
    expect(error.value).toBe('poll-fail')
    expect(elMessageMock.error).not.toHaveBeenCalled()
  })

  it('异常对象无 message 时回退到 defaultErrorMessage', async () => {
    const { error, refresh } = useAsyncData(
      async () => {
        // eslint-disable-next-line no-throw-literal
        throw 'string-error'
      },
      { immediate: false, defaultErrorMessage: '兜底文案' },
    )
    await refresh()
    expect(error.value).toBe('兜底文案')
  })
})
