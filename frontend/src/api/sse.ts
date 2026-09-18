import { useAuthStore } from '@/stores/auth'
import router from '@/router'
import { drainSseBuffer, nextBackoffDelay, parseSseFrame } from './sse-parse'

export { drainSseBuffer, nextBackoffDelay, parseSseFrame } from './sse-parse'
export type { ParsedSseFrame } from './sse-parse'

/**
 * 管理台 SSE 基础模块：hardened 事件流客户端。
 * 语义与 v0.54 A2A 任务流一致（python.ts streamA2ATask 同源抽取）：
 * - fetch + AbortController（需携带 Authorization 头，不能用 EventSource）；
 * - Last-Event-ID 断点续传游标；
 * - 连接正常结束后指数退避重连（1s 起步，10s 封顶）；
 * - 401 → 登出并跳回登录页；400/410 → 游标失效，作为致命错误上抛；
 * - 204 → 流结束（不再重连）；
 * - shouldStop(event) 命中 → 取消读取并结束（如任务终态）。
 */

export interface EventStreamOptions {
  url: string
  headers?: Record<string, string>
  onEvent: (event: Record<string, any>, meta: { id: string | null; event: string }) => void
  onError?: (error: Error) => void
  onEnd?: () => void
  /** 命中后取消读取并结束整个流（不再重连），如任务终态 */
  shouldStop?: (event: Record<string, any>) => boolean
}

/** 建立 hardened SSE 流；返回停止函数。注意 401 会触发登出并跳转登录页。 */
export function createEventStream(options: EventStreamOptions): () => void {
  const abort = new AbortController()
  let cursor: string | null = null
  const run = async () => {
    let delay = 1000
    try {
      while (!abort.signal.aborted) {
        const auth = useAuthStore()
        if (!auth.sessionValid) {
          auth.logout()
          await router.replace('/login')
          throw new Error('登录已失效，请重新登录')
        }
        const headers: Record<string, string> = {
          Authorization: `Bearer ${auth.sessionToken}`,
          ...options.headers,
        }
        if (cursor !== null) headers['Last-Event-ID'] = cursor
        const resp = await fetch(options.url, { headers, signal: abort.signal })
        if (resp.status === 401) {
          auth.logout()
          await router.replace('/login')
          throw new Error('登录已失效，请重新登录')
        }
        if (resp.status === 400 || resp.status === 410) {
          throw new Error(resp.status === 410 ? '续传游标已过期，请重新订阅' : '续传游标无效，请重新订阅')
        }
        if (resp.status === 204) return
        if (!resp.ok || !resp.body) throw new Error(`事件流请求失败：HTTP ${resp.status}`)
        const reader = resp.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!abort.signal.aborted) {
          const { done, value } = await reader.read()
          buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
          const drained = drainSseBuffer(buffer)
          buffer = drained.rest
          for (const frame of drained.frames) {
            if (frame.id !== null) cursor = frame.id
            const event = frame.data.payload && frame.data.event_type ? frame.data.payload : frame.data
            options.onEvent(event, { id: frame.id, event: frame.event })
            if (options.shouldStop?.(event)) {
              await reader.cancel()
              return
            }
          }
          if (done) {
            // 流结束时缓冲区可能残留无空行结尾的最后一帧
            const tail = parseSseFrame(buffer)
            if (tail) {
              if (tail.id !== null) cursor = tail.id
              const event = tail.data.payload && tail.data.event_type ? tail.data.payload : tail.data
              options.onEvent(event, { id: tail.id, event: tail.event })
              if (options.shouldStop?.(event)) return
            }
            break
          }
        }
        if (abort.signal.aborted) return
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, delay)
          abort.signal.addEventListener('abort', () => { clearTimeout(timer); resolve() }, { once: true })
        })
        delay = nextBackoffDelay(delay)
      }
    } catch (error) {
      if (!abort.signal.aborted) options.onError?.(error instanceof Error ? error : new Error('事件流中断'))
    } finally {
      options.onEnd?.()
    }
  }
  void run()
  return () => abort.abort()
}
