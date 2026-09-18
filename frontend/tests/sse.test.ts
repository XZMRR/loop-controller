import { describe, expect, it } from 'vitest'
import { drainSseBuffer, nextBackoffDelay, parseSseFrame } from '@/api/sse-parse'

describe('parseSseFrame', () => {
  it('解析 data 行与 id，默认事件名为 message', () => {
    const frame = parseSseFrame('id: 42\ndata: {"status":"running"}\n')
    expect(frame).not.toBeNull()
    expect(frame!.id).toBe('42')
    expect(frame!.event).toBe('message')
    expect(frame!.data).toEqual({ status: 'running' })
  })

  it('多行 data 合并后 JSON 解析', () => {
    const frame = parseSseFrame('data: {"a":1,\ndata: "b":2}')
    expect(frame!.data).toEqual({ a: 1, 'b': 2 })
  })

  it('event 字段覆盖默认事件名', () => {
    const frame = parseSseFrame('event: result\ndata: {"status":"approved"}')
    expect(frame!.event).toBe('result')
    expect(frame!.data).toEqual({ status: 'approved' })
  })

  it('跳过注释行（: 开头）', () => {
    const frame = parseSseFrame(': keep-alive\ndata: {"x":1}')
    expect(frame!.data).toEqual({ x: 1 })
  })

  it('含 \\0 的 id 被忽略（SSE 规范：游标不前进）', () => {
    const frame = parseSseFrame('id: a\0b\ndata: {"x":1}')
    expect(frame!.id).toBeNull()
  })

  it('非法 JSON 返回 null', () => {
    expect(parseSseFrame('data: not-json')).toBeNull()
  })

  it('无 data 行返回 null', () => {
    expect(parseSseFrame('event: ping\nid: 1')).toBeNull()
  })

  it('CRLF 行尾同样可解析', () => {
    const frame = parseSseFrame('id: 7\r\ndata: {"ok":true}\r\n')
    expect(frame!.id).toBe('7')
    expect(frame!.data).toEqual({ ok: true })
  })
})

describe('drainSseBuffer', () => {
  it('切出多个完整帧，剩余部分保留', () => {
    const buffer = 'data: {"a":1}\n\ndata: {"b":2}\n\ndata: {"c"'
    const { frames, rest } = drainSseBuffer(buffer)
    expect(frames.length).toBe(2)
    expect(frames[0].data).toEqual({ a: 1 })
    expect(frames[1].data).toEqual({ b: 2 })
    expect(rest).toBe('data: {"c"')
  })

  it('无完整帧时全部保留', () => {
    const { frames, rest } = drainSseBuffer('data: {"a":1}\n')
    expect(frames.length).toBe(0)
    expect(rest).toBe('data: {"a":1}\n')
  })

  it('CRLF 分隔的帧被规范化', () => {
    const { frames, rest } = drainSseBuffer('data: {"a":1}\r\n\r\n')
    expect(frames.length).toBe(1)
    expect(rest).toBe('')
  })
})

describe('nextBackoffDelay', () => {
  it('指数增长且封顶 10s', () => {
    expect(nextBackoffDelay(1000)).toBe(2000)
    expect(nextBackoffDelay(2000)).toBe(4000)
    expect(nextBackoffDelay(4000)).toBe(8000)
    expect(nextBackoffDelay(8000)).toBe(10000)
    expect(nextBackoffDelay(10000)).toBe(10000)
  })
})
