import { ref, shallowRef, onMounted, onUnmounted, type Ref } from 'vue'
import { ElMessage } from 'element-plus'

export interface UseAsyncDataOptions {
  /** 加载失败时是否弹出全局错误提示（默认 true） */
  showError?: boolean
  defaultErrorMessage?: string
  /** 自动加载时机：'mounted' 表示组件挂载后自动执行一次（默认） */
  immediate?: boolean
}

export interface UseAsyncDataResult<T> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string>
  /** 手动刷新；refresh(silent=true) 用于轮询等场景，失败不弹提示 */
  refresh: (silent?: boolean) => Promise<void>
}

/**
 * 统一封装异步数据加载：loading / 错误提示 / 手动刷新。
 * 轮询场景建议配合 usePolling 使用，失败时静默重试。
 */
export function useAsyncData<T>(
  loader: () => Promise<T>,
  options: UseAsyncDataOptions = {},
): UseAsyncDataResult<T> {
  const { showError = true, defaultErrorMessage = '加载失败', immediate = true } = options
  // shallowRef 避免 Vue 对泛型 T 做 UnwrapRef 展开，保证 Ref<T | null> 类型可控
  const data: Ref<T | null> = shallowRef<T | null>(null)
  const loading = ref(false)
  const error = ref('')

  async function refresh(silent = false): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      data.value = await loader()
    } catch (e: any) {
      const message = e?.message || defaultErrorMessage
      error.value = message
      if (!silent && showError) {
        ElMessage.error(message)
      }
    } finally {
      loading.value = false
    }
  }

  if (immediate) {
    onMounted(() => void refresh(true))
  }

  return { data, loading, error, refresh }
}

/**
 * 轮询封装：按 intervalMs 周期执行 refresh(silent=true)，组件卸载自动停止。
 * 页面不可见时跳过本轮，避免后台无意义请求。
 */
export function usePolling(refresh: (silent?: boolean) => Promise<void>, intervalMs: number): void {
  let timer: ReturnType<typeof setInterval> | undefined

  onMounted(() => {
    timer = setInterval(() => {
      if (document.hidden) return
      void refresh(true)
    }, intervalMs)
  })

  onUnmounted(() => {
    if (timer) clearInterval(timer)
  })
}
