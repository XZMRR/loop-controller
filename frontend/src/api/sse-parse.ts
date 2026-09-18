/**
 * SSE 纯函数工具：帧解析与退避计算。
 * 独立成模块以便在无 DOM 的 node 测试环境中直接引用
 *（sse.ts 的 createEventStream 依赖 auth store 与 router，不能进 node 测试）。
 */

export interface ParsedSseFrame {
  id: string | null
  event: string
  data: Record<string, any>
}

/** 解析单个 SSE 帧（不含结尾空行）；无 data 或 JSON 非法时返回 null */
export function parseSseFrame(frame: string): ParsedSseFrame | null {
  const data: string[] = []
  let id: string | null = null
  let event = 'message'
  const normalized = frame.replace(/\r\n/g, '\n').replace(/\r(?!$)/g, '\n')
  for (const line of normalized.split('\n')) {
    if (line.startsWith(':')) continue
    const index = line.indexOf(':')
    const field = index < 0 ? line : line.slice(0, index)
    const value = index < 0 ? '' : line.slice(index + 1).replace(/^ /, '')
    if (field === 'data') data.push(value)
    if (field === 'event') event = value || 'message'
    if (field === 'id' && !value.includes('\0')) id = value
  }
  if (!data.length) return null
  let raw: Record<string, any>
  try {
    raw = JSON.parse(data.join('\n'))
  } catch {
    return null
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  return { id, event, data: raw }
}

/**
 * 从累积缓冲区切出完整帧。返回解析出的帧与剩余缓冲区
 *（剩余部分保留，等待后续数据补齐）。
 */
export function drainSseBuffer(
  buffer: string,
): { frames: ParsedSseFrame[]; rest: string } {
  const frames: ParsedSseFrame[] = []
  let rest = buffer.replace(/\r\n/g, '\n').replace(/\r(?!$)/g, '\n')
  let end: number
  while ((end = rest.indexOf('\n\n')) !== -1) {
    const frameText = rest.slice(0, end)
    rest = rest.slice(end + 2)
    const parsed = parseSseFrame(frameText)
    if (parsed) frames.push(parsed)
  }
  return { frames, rest }
}

/** 指数退避：1s → 2s → … → 10s 封顶 */
export function nextBackoffDelay(currentMs: number): number {
  return Math.min(Math.max(currentMs * 2, 1000), 10000)
}
